"""FastAPI-приложение: /health и /v1/echo с ограничением частоты запросов.

Запуск: uvicorn --factory app.main:create_app
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Body, Depends, FastAPI, Request, Response, status
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.auth import get_api_key
from app.config import Settings
from app.limiter import RateLimitResult, SlidingWindowRateLimiter, hash_api_key

# Сколько символов хэша показываем клиенту как key_id: достаточно, чтобы
# различать ключи в логах, и ничего не говорит о самом ключе.
KEY_ID_LENGTH = 12


def get_limiter(request: Request) -> SlidingWindowRateLimiter:
    """Dependency: лимитер, созданный в lifespan. В тестах подменяется."""
    return request.app.state.limiter


def rate_limit_headers(result: RateLimitResult) -> dict[str, str]:
    """Заголовки X-RateLimit-* (и Retry-After, если запрос отклонён)."""
    headers = {
        "X-RateLimit-Limit": str(result.limit),
        "X-RateLimit-Remaining": str(result.remaining),
    }
    if not result.allowed:
        headers["Retry-After"] = str(result.retry_after_seconds)
    return headers


def create_app(settings: Settings | None = None) -> FastAPI:
    """Собрать приложение. Настройки по умолчанию читаются из окружения."""
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
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

    @app.post("/v1/echo", response_model=None)
    async def echo(
        body: Annotated[dict[str, Any], Body()],
        response: Response,
        api_key: Annotated[str, Depends(get_api_key)],
        limiter: Annotated[SlidingWindowRateLimiter, Depends(get_limiter)],
    ) -> dict[str, Any] | JSONResponse:
        try:
            result = await limiter.hit(api_key)
        except RedisError:
            # Без Redis мы не можем проверить лимит. Выбираем fail-closed:
            # лучше временно отказать, чем пропустить клиента сверх квоты.
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"detail": "Rate limiter backend unavailable"},
            )

        headers = rate_limit_headers(result)
        if not result.allowed:
            return JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={"detail": "Rate limit exceeded"},
                headers=headers,
            )

        response.headers.update(headers)
        return {**body, "key_id": hash_api_key(api_key)[:KEY_ID_LENGTH]}

    return app
