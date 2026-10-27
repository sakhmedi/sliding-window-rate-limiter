"""Извлечение API-ключа из заголовка `Authorization: Bearer <key>`."""

from typing import Annotated

from fastapi import Header, HTTPException, status


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        # Стандартная подсказка клиенту, какую схему аутентификации ждём.
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_api_key(authorization: Annotated[str | None, Header()] = None) -> str:
    """FastAPI dependency: вернуть ключ или ответить 401.

    Корректный формат — ровно два слова: "Bearer" (регистр не важен) и сам ключ.
    """
    if authorization is None:
        raise _unauthorized("Missing Authorization header")

    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise _unauthorized("Authorization header must be 'Bearer <key>'")

    return parts[1]
