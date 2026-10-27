"""Общие фикстуры: подменные часы, fakeredis, лимитер и HTTP-клиент."""

from collections.abc import AsyncIterator, Callable

import fakeredis
import pytest
from httpx import ASGITransport, AsyncClient

from app.config import Settings
from app.limiter import SlidingWindowRateLimiter
from app.main import create_app, get_limiter

# Стартовое время подменных часов: произвольный «реалистичный» момент в мс.
START_MS = 1_700_000_000_000


class FakeClock:
    """Часы, которые идут только когда тест вызывает advance().

    Позволяют проверять сдвиг окна мгновенно, без time.sleep().
    """

    def __init__(self, start_ms: int = START_MS) -> None:
        self.now_ms = start_ms

    def __call__(self) -> int:
        return self.now_ms

    def advance(self, seconds: float) -> None:
        self.now_ms += round(seconds * 1000)


# Фабрика лимитеров: тест сам выбирает лимит и окно.
LimiterFactory = Callable[[int, int], SlidingWindowRateLimiter]


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
async def redis() -> AsyncIterator[fakeredis.FakeAsyncRedis]:
    # Каждый тест получает свой «сервер», чтобы состояние не протекало между тестами.
    client = fakeredis.FakeAsyncRedis(server=fakeredis.FakeServer())
    yield client
    await client.aclose()


@pytest.fixture
def make_limiter(redis: fakeredis.FakeAsyncRedis, clock: FakeClock) -> LimiterFactory:
    def factory(limit: int, window_seconds: int) -> SlidingWindowRateLimiter:
        return SlidingWindowRateLimiter(redis, limit, window_seconds, clock=clock)

    return factory


@pytest.fixture
def limiter(make_limiter: LimiterFactory) -> SlidingWindowRateLimiter:
    """Лимитер «по умолчанию» для HTTP-тестов: 10 запросов за 60 секунд."""
    return make_limiter(10, 60)


@pytest.fixture
async def client(limiter: SlidingWindowRateLimiter) -> AsyncIterator[AsyncClient]:
    """HTTP-клиент к приложению с подменённым лимитером (fakeredis + FakeClock).

    ASGITransport не запускает lifespan, поэтому настоящий Redis не нужен.
    """
    app = create_app(Settings())
    app.dependency_overrides[get_limiter] = lambda: limiter
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client
