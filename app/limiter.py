"""Rate limiter со скользящим окном (sliding window log) на Redis.

Для каждого API-ключа в Redis хранится sorted set:
    ключ   = "ratelimit:<sha256(api_key)>"
    member = "<время в мс>-<uuid>"   (uuid — чтобы два запроса в одну мс не склеились)
    score  = время запроса в мс

Вся логика «проверить и записать» выполняется одним Lua-скриптом, поэтому
она атомарна: Redis выполняет скрипт целиком, не перемежая его другими командами.
"""

import hashlib
import math
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from redis.asyncio import Redis

# Тип «часов»: функция без аргументов, возвращающая текущее время в миллисекундах.
# В продакшене — реальное время, в тестах — подменные часы.
Clock = Callable[[], int]

# KEYS[1] — ключ sorted set'а
# ARGV[1] — now (мс), ARGV[2] — window (мс), ARGV[3] — limit, ARGV[4] — member
# Возвращает {allowed (1/0), remaining, retry_after_ms}.
SLIDING_WINDOW_LUA = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local member = ARGV[4]

-- 1. Удаляем записи, вышедшие из окна. Граница включительная:
--    запрос, сделанный ровно window мс назад, уже не считается.
redis.call('ZREMRANGEBYSCORE', key, 0, now - window)

-- 2. Сколько запросов сейчас в окне.
local count = redis.call('ZCARD', key)

-- 3. Есть место: записываем запрос и продлеваем TTL ключа на длину окна
--    (после этого все записи гарантированно устарели, ключ можно удалить).
if count < limit then
    redis.call('ZADD', key, now, member)
    redis.call('PEXPIRE', key, window)
    return {1, limit - count - 1, 0}
end

-- 4. Места нет: отклоняем, НЕ добавляя запрос в окно.
--    Ждать нужно, пока самый старый запрос не выйдет из окна.
local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
local retry_after_ms = tonumber(oldest[2]) + window - now
return {0, 0, retry_after_ms}
"""


def system_clock_ms() -> int:
    """Текущее время Unix в миллисекундах.

    Берём именно «настенное» время, а не time.monotonic(): несколько процессов
    API работают с одним Redis, и их метки времени должны быть сравнимы.
    """
    return time.time_ns() // 1_000_000


def hash_api_key(api_key: str) -> str:
    """SHA-256 от ключа в hex. Сам ключ нигде не сохраняем."""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RateLimitResult:
    """Результат проверки одного запроса."""

    allowed: bool
    limit: int
    remaining: int
    retry_after_ms: int

    @property
    def retry_after_seconds(self) -> int:
        """Значение для заголовка Retry-After: целые секунды, вверх, минимум 1."""
        return max(1, math.ceil(self.retry_after_ms / 1000))


class SlidingWindowRateLimiter:
    """Ограничивает число запросов: не больше `limit` за `window_seconds` на ключ."""

    def __init__(
        self,
        redis: Redis,
        limit: int,
        window_seconds: int,
        clock: Clock = system_clock_ms,
    ) -> None:
        if limit <= 0:
            raise ValueError("limit must be positive")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")

        self._redis = redis
        self.limit = limit
        self.window_ms = window_seconds * 1000
        self._clock = clock
        # register_script вызывает скрипт через EVALSHA (по хэшу) и сам
        # делает EVAL, если Redis этот скрипт ещё не видел.
        self._script = redis.register_script(SLIDING_WINDOW_LUA)

    @staticmethod
    def redis_key(api_key: str) -> str:
        """Имя sorted set'а в Redis для данного API-ключа."""
        return f"ratelimit:{hash_api_key(api_key)}"

    async def hit(self, api_key: str) -> RateLimitResult:
        """Учесть один запрос по ключу и сказать, разрешён ли он."""
        now_ms = self._clock()
        member = f"{now_ms}-{uuid.uuid4().hex}"

        allowed, remaining, retry_after_ms = await self._script(
            keys=[self.redis_key(api_key)],
            args=[now_ms, self.window_ms, self.limit, member],
        )
        return RateLimitResult(
            allowed=bool(allowed),
            limit=self.limit,
            remaining=int(remaining),
            retry_after_ms=int(retry_after_ms),
        )
