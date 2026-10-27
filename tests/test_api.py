"""HTTP-тесты: аутентификация, лимиты, заголовки, конкурентность."""

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient, Response
from redis.exceptions import ConnectionError as RedisConnectionError

from app.config import Settings
from app.limiter import RateLimitResult, SlidingWindowRateLimiter, hash_api_key
from app.main import create_app, get_limiter
from tests.conftest import FakeClock

# Лимитер из conftest: 10 запросов за 60 секунд.
LIMIT = 10
WINDOW = 60


def auth(key: str = "test123") -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


async def echo(client: AsyncClient, key: str = "test123", body: object = None) -> Response:
    return await client.post("/v1/echo", headers=auth(key), json=body or {"hi": 1})


async def exhaust(client: AsyncClient, key: str = "test123") -> None:
    for _ in range(LIMIT):
        assert (await echo(client, key)).status_code == 200


async def test_health_ok_without_auth(client: AsyncClient):
    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_health_is_not_rate_limited(client: AsyncClient):
    for _ in range(LIMIT * 2):
        assert (await client.get("/health")).status_code == 200


async def test_echo_returns_body_with_key_id(client: AsyncClient):
    response = await echo(client, body={"model": "x", "messages": [1, 2]})

    assert response.status_code == 200
    assert response.json() == {
        "model": "x",
        "messages": [1, 2],
        "key_id": hash_api_key("test123")[:12],
    }
    assert "test123" not in response.text  # сам ключ не утекает в ответ


async def test_under_limit_remaining_decreases_to_zero(client: AsyncClient):
    remaining = []
    for _ in range(LIMIT):
        response = await echo(client)
        assert response.status_code == 200
        assert response.headers["X-RateLimit-Limit"] == str(LIMIT)
        remaining.append(int(response.headers["X-RateLimit-Remaining"]))

    assert remaining == list(range(LIMIT - 1, -1, -1))


async def test_over_limit_returns_429_with_headers(client: AsyncClient):
    await exhaust(client)

    response = await echo(client)

    assert response.status_code == 429
    assert response.json() == {"detail": "Rate limit exceeded"}
    assert int(response.headers["Retry-After"]) > 0
    assert response.headers["X-RateLimit-Limit"] == str(LIMIT)
    assert response.headers["X-RateLimit-Remaining"] == "0"


async def test_window_slides_back_to_200(client: AsyncClient, clock: FakeClock):
    await exhaust(client)
    assert (await echo(client)).status_code == 429

    clock.advance(WINDOW)

    response = await echo(client)
    assert response.status_code == 200
    assert response.headers["X-RateLimit-Remaining"] == str(LIMIT - 1)


async def test_partial_slide_frees_only_expired_slots(client: AsyncClient, clock: FakeClock):
    # 4 запроса в t=0, 6 запросов в t=30 -> окно полно.
    for _ in range(4):
        await echo(client)
    clock.advance(30)
    for _ in range(6):
        await echo(client)
    assert (await echo(client)).status_code == 429

    clock.advance(30)  # t=60: вышли только 4 запроса из t=0
    for _ in range(4):
        assert (await echo(client)).status_code == 200
    blocked = await echo(client)
    assert blocked.status_code == 429
    assert blocked.headers["Retry-After"] == "30"  # самый старый теперь t=30


async def test_boundary_request_exactly_window_ago_not_counted(
    client: AsyncClient, clock: FakeClock
):
    await exhaust(client)  # все 10 в t=0

    clock.advance(WINDOW - 0.001)
    almost = await echo(client)
    assert almost.status_code == 429
    assert almost.headers["Retry-After"] == "1"  # 1 мс -> округляем вверх до 1 с

    clock.advance(0.001)  # ровно t = 0 + T
    assert (await echo(client)).status_code == 200


async def test_different_keys_do_not_affect_each_other(client: AsyncClient):
    await exhaust(client, key="alice")
    assert (await echo(client, key="alice")).status_code == 429

    response = await echo(client, key="bob")

    assert response.status_code == 200
    assert response.headers["X-RateLimit-Remaining"] == str(LIMIT - 1)
    assert response.json()["key_id"] != hash_api_key("alice")[:12]


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": ""},
        {"Authorization": "test123"},
        {"Authorization": "Basic dXNlcjpwYXNz"},
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer "},
        {"Authorization": "Bearer two tokens"},
    ],
    ids=["missing", "empty", "no-scheme", "basic", "no-key", "blank-key", "extra-part"],
)
async def test_missing_or_malformed_authorization_returns_401(
    client: AsyncClient, headers: dict[str, str]
):
    response = await client.post("/v1/echo", headers=headers, json={"hi": 1})

    assert response.status_code == 401
    assert "detail" in response.json()
    assert response.headers["WWW-Authenticate"] == "Bearer"


async def test_bearer_scheme_is_case_insensitive(client: AsyncClient):
    response = await client.post("/v1/echo", headers={"Authorization": "bearer k"}, json={})

    assert response.status_code == 200


async def test_retry_after_exact_value(client: AsyncClient, clock: FakeClock):
    await echo(client)  # t=0 — самый старый
    clock.advance(1.5)
    for _ in range(LIMIT - 1):
        await echo(client)  # t=1.5

    clock.advance(10.5)  # t=12 -> ждать 0 + 60 - 12 = 48 с
    assert (await echo(client)).headers["Retry-After"] == "48"

    clock.advance(0.2)  # t=12.2 -> ждать 47.8 с -> округляем вверх до 48
    assert (await echo(client)).headers["Retry-After"] == "48"


async def test_rejected_requests_do_not_extend_block(client: AsyncClient, clock: FakeClock):
    await exhaust(client)  # t=0

    for _ in range(5):  # долбим сервис отклонёнными запросами
        clock.advance(10)
        assert (await echo(client)).status_code == 429  # t=10..50

    clock.advance(10)  # t=60: блокировка снята ровно по расписанию
    assert (await echo(client)).status_code == 200


async def test_concurrent_requests_allow_exactly_limit(client: AsyncClient):
    responses = await asyncio.gather(*(echo(client) for _ in range(50)))

    codes = [r.status_code for r in responses]
    assert codes.count(200) == LIMIT
    assert codes.count(429) == 50 - LIMIT


async def test_non_object_body_returns_422(client: AsyncClient):
    response = await client.post("/v1/echo", headers=auth(), json=[1, 2, 3])

    assert response.status_code == 422


class BrokenLimiter(SlidingWindowRateLimiter):
    """Лимитер, у которого «упал» Redis."""

    def __init__(self) -> None:  # родительский конструктор не нужен
        pass

    async def hit(self, api_key: str) -> RateLimitResult:
        raise RedisConnectionError("redis is down")


async def test_redis_unavailable_returns_503():
    app = create_app(Settings())
    app.dependency_overrides[get_limiter] = BrokenLimiter

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await echo(client)

    assert response.status_code == 503
