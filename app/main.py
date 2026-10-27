"""FastAPI-приложение: /health и /v1/echo с ограничением частоты запросов.

Запуск: uvicorn --factory app.main:create_app
"""

import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from http import HTTPStatus
from typing import Annotated, Any, NoReturn

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.auth import get_api_key
from app.config import Settings
from app.limiter import RateLimitResult, SlidingWindowRateLimiter, hash_api_key

# Сколько символов хэша показываем клиенту как key_id: достаточно, чтобы
# различать ключи в логах, и ничего не говорит о самом ключе.
KEY_ID_LENGTH = 12

# Тело /v1/echo — любой JSON. Описываем это для OpenAPI вручную, потому что
# FastAPI не видит тела среди параметров роута (мы разбираем его сами, см. echo).
ANY_JSON_BODY: dict[str, Any] = {
    "requestBody": {"required": True, "content": {"application/json": {"schema": {}}}}
}


def get_limiter(request: Request) -> SlidingWindowRateLimiter:
    """Dependency: лимитер, созданный в lifespan. В тестах подменяется."""
    limiter: SlidingWindowRateLimiter = request.app.state.limiter
    return limiter


def rate_limit_headers(result: RateLimitResult) -> dict[str, str]:
    """Заголовки X-RateLimit-* (и Retry-After, если запрос отклонён)."""
    headers = {
        "X-RateLimit-Limit": str(result.limit),
        "X-RateLimit-Remaining": str(result.remaining),
    }
    if not result.allowed:
        headers["Retry-After"] = str(result.retry_after_seconds)
    return headers


def _reject_constant(name: str) -> NoReturn:
    # json.loads по умолчанию принимает NaN и Infinity, но это не JSON:
    # такой ответ потом нельзя было бы сериализовать обратно.
    raise ValueError(f"{name} is not valid JSON")


def parse_json(raw: bytes) -> Any:
    """Разобрать тело как JSON любого типа: объект, массив, число, строка, true/false/null.

    Бросает ValueError (сюда входят JSONDecodeError и ошибки кодировки)
    или RecursionError (слишком глубокая вложенность).
    """
    return json.loads(raw, parse_constant=_reject_constant)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Собрать приложение. Настройки по умолчанию читаются из окружения."""
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        # Один клиент Redis (с пулом соединений) на всё время жизни приложения.
        redis = Redis.from_url(settings.redis_url)
        app.state.limiter = SlidingWindowRateLimiter(
            redis,
            limit=settings.rate_limit_requests,
            window_seconds=settings.rate_limit_window_seconds,
        )
        try:
            yield
        finally:
            await redis.aclose()

    app = FastAPI(title="Sliding-window rate limiter", lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/echo", response_model=None, openapi_extra=ANY_JSON_BODY)
    async def echo(
        request: Request,
        response: Response,
        api_key: Annotated[str, Depends(get_api_key)],
        limiter: Annotated[SlidingWindowRateLimiter, Depends(get_limiter)],
    ) -> dict[str, Any] | JSONResponse:
        # Порядок проверок: ключ -> лимит -> тело.
        # 1. Ключ проверяет dependency get_api_key: без него сразу 401, тело не читаем.
        # 2. Лимит: каждый авторизованный запрос (даже с битым телом) тратит место в окне,
        #    иначе клиент мог бы бесплатно нагружать сервис заведомо невалидными запросами.
        try:
            result = await limiter.hit(api_key)
        except RedisError:
            # Без Redis мы не можем проверить лимит. Выбираем fail-closed:
            # лучше временно отказать, чем пропустить клиента сверх квоты.
            return JSONResponse(
                status_code=HTTPStatus.SERVICE_UNAVAILABLE,
                content={"detail": "Rate limiter backend unavailable"},
            )

        headers = rate_limit_headers(result)
        if not result.allowed:
            return JSONResponse(
                status_code=HTTPStatus.TOO_MANY_REQUESTS,
                content={"detail": "Rate limit exceeded"},
                headers=headers,
            )

        # 3. Только теперь читаем и разбираем тело.
        try:
            body = parse_json(await request.body())
        except (ValueError, RecursionError):
            return JSONResponse(
                status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
                content={"detail": "Request body must be valid JSON"},
                headers=headers,
            )

        response.headers.update(headers)
        return {"echo": body, "key_id": hash_api_key(api_key)[:KEY_ID_LENGTH]}

    return app
