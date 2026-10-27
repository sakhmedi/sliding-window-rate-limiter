"""Юнит-тесты SlidingWindowRateLimiter (fakeredis + подменные часы)."""

import asyncio
import os

import fakeredis
import pytest
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.limiter import RateLimitResult, SlidingWindowRateLimiter, hash_api_key
from tests.conftest import FakeClock, LimiterFactory

KEY = "test-key"


async def hit_n(limiter: SlidingWindowRateLimiter, n: int, key: str = KEY) -> list[RateLimitResult]:
    return [await limiter.hit(key) for _ in range(n)]


async def test_under_limit_all_allowed_and_remaining_decreases(
    make_limiter: LimiterFactory,
) -> None:
    limiter = make_limiter(5, 60)

    results = await hit_n(limiter, 5)

    assert all(r.allowed for r in results)
    assert [r.remaining for r in results] == [4, 3, 2, 1, 0]


async def test_over_limit_rejected_with_retry_after(make_limiter: LimiterFactory) -> None:
    limiter = make_limiter(3, 60)
    await hit_n(limiter, 3)

    result = await limiter.hit(KEY)

    assert not result.allowed
    assert result.remaining == 0
    assert result.retry_after_ms > 0
    assert result.retry_after_seconds > 0


async def test_window_slides_after_full_window(
    make_limiter: LimiterFactory, clock: FakeClock
) -> None:
    limiter = make_limiter(3, 10)
    await hit_n(limiter, 3)
    assert not (await limiter.hit(KEY)).allowed

    clock.advance(10)

    assert (await limiter.hit(KEY)).allowed


async def test_partial_slide_frees_exactly_expired_slots(
    make_limiter: LimiterFactory, clock: FakeClock
) -> None:
    """Главное отличие от фиксированного окна: места освобождаются по одному."""
    limiter = make_limiter(3, 10)
    assert (await limiter.hit(KEY)).allowed  # t=0
    clock.advance(4)
    assert (await limiter.hit(KEY)).allowed  # t=4
    clock.advance(2)
    assert (await limiter.hit(KEY)).allowed  # t=6
    clock.advance(1)
    assert not (await limiter.hit(KEY)).allowed  # t=7, окно полно

    clock.advance(3)  # t=10: вышел только запрос из t=0
    assert (await limiter.hit(KEY)).allowed
    blocked = await limiter.hit(KEY)
    assert not blocked.allowed
    assert blocked.retry_after_ms == 4_000  # самый старый теперь t=4 -> выйдет в t=14

    clock.advance(4)  # t=14: вышел запрос из t=4
    assert (await limiter.hit(KEY)).allowed
    assert not (await limiter.hit(KEY)).allowed


async def test_boundary_request_exactly_window_ago_not_counted(
    make_limiter: LimiterFactory, clock: FakeClock
) -> None:
    limiter = make_limiter(1, 10)
    assert (await limiter.hit(KEY)).allowed  # t=0

    clock.advance(9.999)  # за 1 мс до границы запрос ещё в окне
    almost = await limiter.hit(KEY)
    assert not almost.allowed
    assert almost.retry_after_ms == 1
    assert almost.retry_after_seconds == 1  # округление вверх, минимум 1

    clock.advance(0.001)  # ровно t = 0 + T: старый запрос уже не учитывается
    assert (await limiter.hit(KEY)).allowed


async def test_different_keys_are_independent(make_limiter: LimiterFactory) -> None:
    limiter = make_limiter(2, 60)
    await hit_n(limiter, 2, key="alice")
    assert not (await limiter.hit("alice")).allowed

    bob = await limiter.hit("bob")

    assert bob.allowed
    assert bob.remaining == 1


async def test_retry_after_exact_value(make_limiter: LimiterFactory, clock: FakeClock) -> None:
    limiter = make_limiter(2, 10)
    await limiter.hit(KEY)  # t=0
    clock.advance(1.5)
    await limiter.hit(KEY)  # t=1.5

    clock.advance(1.5)  # t=3.0
    first = await limiter.hit(KEY)
    assert first.retry_after_ms == 7_000
    assert first.retry_after_seconds == 7

    clock.advance(0.2)  # t=3.2 -> ждать 6.8 с, округляем вверх до 7
    second = await limiter.hit(KEY)
    assert second.retry_after_ms == 6_800
    assert second.retry_after_seconds == 7


async def test_rejected_requests_do_not_extend_block(
    make_limiter: LimiterFactory, clock: FakeClock
) -> None:
    limiter = make_limiter(2, 10)
    await hit_n(limiter, 2)  # t=0

    clock.advance(5)
    assert (await limiter.hit(KEY)).retry_after_ms == 5_000  # t=5, отклонён
    clock.advance(4)
    assert (await limiter.hit(KEY)).retry_after_ms == 1_000  # t=9, отклонён

    clock.advance(1)  # t=10: если бы отклонённые писались в окно, тут был бы 429
    assert (await limiter.hit(KEY)).allowed


async def test_same_millisecond_requests_are_not_merged(
    make_limiter: LimiterFactory, redis: fakeredis.FakeAsyncRedis
) -> None:
    """Часы стоят на месте — у всех запросов одинаковый score, но member разный."""
    limiter = make_limiter(5, 60)
    await hit_n(limiter, 3)

    assert await redis.zcard(SlidingWindowRateLimiter.redis_key(KEY)) == 3


async def test_redis_key_is_hashed_and_has_ttl(
    make_limiter: LimiterFactory, redis: fakeredis.FakeAsyncRedis
) -> None:
    limiter = make_limiter(5, 60)
    await limiter.hit("secret-api-key")

    keys = await redis.keys("*")
    assert keys == [f"ratelimit:{hash_api_key('secret-api-key')}".encode()]
    assert b"secret-api-key" not in keys[0]
    ttl_ms = await redis.pttl(keys[0])
    assert 0 < ttl_ms <= 60_000


async def test_concurrent_hits_allow_exactly_limit(make_limiter: LimiterFactory) -> None:
    """50 одновременных hit() через asyncio.gather -> разрешено ровно 10.

    ВАЖНО: на fakeredis этот тест НЕ доказывает атомарность. fakeredis выполняет
    команды строго по очереди, поэтому даже неатомарная реализация (отдельные
    ZCARD и ZADD) здесь тоже даст 10. Атомарность Lua-скрипта проверяет
    test_concurrency_against_real_redis ниже — на настоящем Redis неатомарная
    реализация пропускает все 50.
    """
    limiter = make_limiter(10, 60)

    results = await asyncio.gather(*(limiter.hit(KEY) for _ in range(50)))

    assert sum(r.allowed for r in results) == 10


@pytest.mark.parametrize(("limit", "window"), [(0, 60), (10, 0), (-1, 60)])
def test_invalid_parameters_rejected(
    redis: fakeredis.FakeAsyncRedis, limit: int, window: int
) -> None:
    with pytest.raises(ValueError):
        SlidingWindowRateLimiter(redis, limit, window)


async def test_concurrency_against_real_redis() -> None:
    """Проверка атомарности на настоящем Redis: 50 одновременных запросов, лимит 10.

    Здесь запросы реально конкурируют за соединения из пула, и без Lua-скрипта
    (отдельные ZCARD и ZADD) проходят все 50.

    Если Redis недоступен: при REQUIRE_REDIS=1 (так в docker-compose) тест падает,
    иначе (локальный `make test` без Redis) — пропускается.
    """
    redis = Redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
    try:
        await redis.ping()
    except (RedisError, OSError) as error:
        await redis.aclose()
        if os.environ.get("REQUIRE_REDIS") == "1":
            pytest.fail(f"REQUIRE_REDIS=1, but real Redis is not available: {error}")
        pytest.skip("real Redis is not available (set REQUIRE_REDIS=1 to make this a failure)")

    limiter = SlidingWindowRateLimiter(redis, limit=10, window_seconds=60)
    key = f"integration-test-{os.getpid()}"
    try:
        results = await asyncio.gather(*(limiter.hit(key) for _ in range(50)))
        assert sum(r.allowed for r in results) == 10
    finally:
        await redis.delete(SlidingWindowRateLimiter.redis_key(key))
        await redis.aclose()
