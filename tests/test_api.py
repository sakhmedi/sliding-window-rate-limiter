"""HTTP-тесты: аутентификация, разбор тела, лимиты, заголовки, конкурентность."""

import asyncio
import json
from typing import Any

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

# Тело по умолчанию для echo(); не изменяется тестами.
DEFAULT_BODY = {"hi": 1}

# Тела, которые не являются валидным JSON.
MALFORMED_BODIES = [
    pytest.param(b"{not json", id="broken-object"),
    pytest.param(b"", id="empty"),
    pytest.param(b"hello", id="bare-word"),
    pytest.param(b'{"a": 1,}', id="trailing-comma"),
    pytest.param(b"NaN", id="nan"),
    pytest.param(b'{"x": Infinity}', id="infinity"),
    pytest.param(b"\xff\xfe\xfa", id="bad-encoding"),
    pytest.param(b"[" * 100_000 + b"]" * 100_000, id="too-deep"),
]

# Заголовки Authorization, с которыми запрос не авторизован.
BAD_AUTH_HEADERS = [
    pytest.param({}, id="missing"),
    pytest.param({"Authorization": ""}, id="empty"),
    pytest.param({"Authorization": "test123"}, id="no-scheme"),
    pytest.param({"Authorization": "Basic dXNlcjpwYXNz"}, id="basic"),
    pytest.param({"Authorization": "Bearer"}, id="no-key"),
    pytest.param({"Authorization": "Bearer "}, id="blank-key"),
    pytest.param({"Authorization": "Bearer two tokens"}, id="extra-part"),
]


def auth(key: str = "test123") -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


async def echo(client: AsyncClient, key: str = "test123", body: Any = DEFAULT_BODY) -> Response:
    return await client.post("/v1/echo", headers=auth(key), json=body)


async def post_raw(client: AsyncClient, content: bytes, headers: dict[str, str]) -> Response:
    """Отправить тело как есть, без сериализации в JSON."""
    return await client.post(
        "/v1/echo", content=content, headers={"Content-Type": "application/json", **headers}
    )


async def exhaust(client: AsyncClient, key: str = "test123") -> None:
    for _ in range(LIMIT):
        assert (await echo(client, key)).status_code == 200


# --- /health ---------------------------------------------------------------


async def test_health_ok_without_auth(client: AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_health_is_not_rate_limited(client: AsyncClient) -> None:
    for _ in range(LIMIT * 2):
        assert (await client.get("/health")).status_code == 200


# --- тело запроса и формат ответа ------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"model": "x", "messages": [1, 2]}, id="object"),
        pytest.param([1, "two", {"three": 3}], id="array"),
        pytest.param(42, id="int"),
        pytest.param(-1.5, id="float"),
        pytest.param("hello", id="string"),
        pytest.param(True, id="bool"),
        pytest.param(None, id="null"),
        pytest.param({}, id="empty-object"),
        pytest.param({"key_id": "client-value"}, id="object-with-key_id"),
    ],
)
async def test_any_valid_json_is_echoed(client: AsyncClient, body: Any) -> None:
    # Сериализуем сами: httpx с json=None вообще не отправил бы тело.
    response = await post_raw(client, json.dumps(body).encode(), auth())

    assert response.status_code == 200
    assert response.json() == {"echo": body, "key_id": hash_api_key("test123")[:12]}


async def test_key_itself_is_not_leaked(client: AsyncClient) -> None:
    response = await echo(client, key="super-secret-key")

    assert "super-secret-key" not in response.text


async def test_echo_ignores_content_type(client: AsyncClient) -> None:
    """Тело разбирается как JSON независимо от Content-Type."""
    response = await client.post(
        "/v1/echo", content=b"[1, 2]", headers={**auth(), "Content-Type": "text/plain"}
    )

    assert response.status_code == 200
    assert response.json()["echo"] == [1, 2]


@pytest.mark.parametrize("content", MALFORMED_BODIES)
async def test_malformed_json_with_valid_key_returns_422(
    client: AsyncClient, content: bytes
) -> None:
    response = await post_raw(client, content, auth())

    assert response.status_code == 422
    assert response.json() == {"detail": "Request body must be valid JSON"}
    # Лимит уже проверен, поэтому заголовки лимита есть и в 422.
    assert response.headers["X-RateLimit-Limit"] == str(LIMIT)
    assert response.headers["X-RateLimit-Remaining"] == str(LIMIT - 1)


async def test_malformed_json_counts_against_limit(client: AsyncClient) -> None:
    """Лимит проверяется до разбора тела: битый запрос тоже занимает место в окне."""
    for _ in range(LIMIT):
        assert (await post_raw(client, b"{not json", auth())).status_code == 422

    response = await echo(client)  # валидный запрос, но окно уже заполнено
    assert response.status_code == 429


async def test_over_limit_with_malformed_json_returns_429(client: AsyncClient) -> None:
    """При исчерпанном лимите тело даже не разбирается: 429, а не 422."""
    await exhaust(client)

    response = await post_raw(client, b"{not json", auth())

    assert response.status_code == 429
    assert "Retry-After" in response.headers


# --- авторизация -----------------------------------------------------------


@pytest.mark.parametrize("headers", BAD_AUTH_HEADERS)
async def test_missing_or_malformed_authorization_returns_401(
    client: AsyncClient, headers: dict[str, str]
) -> None:
    response = await client.post("/v1/echo", headers=headers, json={"hi": 1})

    assert response.status_code == 401
    assert "detail" in response.json()
    assert response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize("headers", BAD_AUTH_HEADERS)
@pytest.mark.parametrize("content", MALFORMED_BODIES)
async def test_no_valid_key_returns_401_even_with_malformed_json(
    client: AsyncClient, headers: dict[str, str], content: bytes
) -> None:
    """Авторизация проверяется до разбора тела: без ключа всегда 401, а не 422."""
    response = await post_raw(client, content, headers)

    assert response.status_code == 401


async def test_unauthorized_requests_do_not_consume_limit(client: AsyncClient) -> None:
    for _ in range(LIMIT * 2):
        assert (await client.post("/v1/echo", json={"hi": 1})).status_code == 401

    response = await echo(client)
    assert response.status_code == 200
    assert response.headers["X-RateLimit-Remaining"] == str(LIMIT - 1)


async def test_bearer_scheme_is_case_insensitive(client: AsyncClient) -> None:
    response = await client.post("/v1/echo", headers={"Authorization": "bearer k"}, json={})

    assert response.status_code == 200


# --- лимиты и заголовки ----------------------------------------------------


async def test_under_limit_remaining_decreases_to_zero(client: AsyncClient) -> None:
    remaining = []
    for _ in range(LIMIT):
        response = await echo(client)
        assert response.status_code == 200
        assert response.headers["X-RateLimit-Limit"] == str(LIMIT)
        remaining.append(int(response.headers["X-RateLimit-Remaining"]))

    assert remaining == list(range(LIMIT - 1, -1, -1))


async def test_over_limit_returns_429_with_headers(client: AsyncClient) -> None:
    await exhaust(client)

    response = await echo(client)

    assert response.status_code == 429
    assert response.json() == {"detail": "Rate limit exceeded"}
    assert int(response.headers["Retry-After"]) > 0
    assert response.headers["X-RateLimit-Limit"] == str(LIMIT)
    assert response.headers["X-RateLimit-Remaining"] == "0"


async def test_window_slides_back_to_200(client: AsyncClient, clock: FakeClock) -> None:
    await exhaust(client)
    assert (await echo(client)).status_code == 429

    clock.advance(WINDOW)

    response = await echo(client)
    assert response.status_code == 200
    assert response.headers["X-RateLimit-Remaining"] == str(LIMIT - 1)


async def test_partial_slide_frees_only_expired_slots(
    client: AsyncClient, clock: FakeClock
) -> None:
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
) -> None:
    await exhaust(client)  # все 10 в t=0

    clock.advance(WINDOW - 0.001)
    almost = await echo(client)
    assert almost.status_code == 429
    assert almost.headers["Retry-After"] == "1"  # 1 мс -> округляем вверх до 1 с

    clock.advance(0.001)  # ровно t = 0 + T
    assert (await echo(client)).status_code == 200


async def test_different_keys_do_not_affect_each_other(client: AsyncClient) -> None:
    await exhaust(client, key="alice")
    assert (await echo(client, key="alice")).status_code == 429

    response = await echo(client, key="bob")

    assert response.status_code == 200
    assert response.headers["X-RateLimit-Remaining"] == str(LIMIT - 1)
    assert response.json()["key_id"] != hash_api_key("alice")[:12]


async def test_retry_after_exact_value(client: AsyncClient, clock: FakeClock) -> None:
    await echo(client)  # t=0 — самый старый
    clock.advance(1.5)
    for _ in range(LIMIT - 1):
        await echo(client)  # t=1.5

    clock.advance(10.5)  # t=12 -> ждать 0 + 60 - 12 = 48 с
    assert (await echo(client)).headers["Retry-After"] == "48"

    clock.advance(0.2)  # t=12.2 -> ждать 47.8 с -> округляем вверх до 48
    assert (await echo(client)).headers["Retry-After"] == "48"


async def test_rejected_requests_do_not_extend_block(client: AsyncClient, clock: FakeClock) -> None:
    await exhaust(client)  # t=0

    for _ in range(5):  # долбим сервис отклонёнными запросами
        clock.advance(10)
        assert (await echo(client)).status_code == 429  # t=10..50

    clock.advance(10)  # t=60: блокировка снята ровно по расписанию
    assert (await echo(client)).status_code == 200


async def test_concurrent_requests_allow_exactly_limit(client: AsyncClient) -> None:
    """Через HTTP-слой 50 одновременных запросов -> ровно 10 разрешено.

    На fakeredis это проверяет правильность подсчёта, но не атомарность —
    см. test_concurrency_against_real_redis в test_limiter.py.
    """
    responses = await asyncio.gather(*(echo(client) for _ in range(50)))

    codes = [r.status_code for r in responses]
    assert codes.count(200) == LIMIT
    assert codes.count(429) == 50 - LIMIT


# --- отказ Redis -----------------------------------------------------------


class BrokenLimiter(SlidingWindowRateLimiter):
    """Лимитер, у которого «упал» Redis."""

    def __init__(self) -> None:  # родительский конструктор не нужен
        pass

    async def hit(self, api_key: str) -> RateLimitResult:
        raise RedisConnectionError("redis is down")


async def test_redis_unavailable_returns_503() -> None:
    app = create_app(Settings())
    app.dependency_overrides[get_limiter] = BrokenLimiter

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await echo(client)

    assert response.status_code == 503
    assert response.json() == {"detail": "Rate limiter backend unavailable"}
