FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Непривилегированный пользователь, от которого работает сервис.
RUN useradd --create-home --uid 1000 app

# Сначала только описание зависимостей — этот слой кэшируется,
# пока pyproject.toml не меняется.
COPY pyproject.toml README.md ./
COPY app/__init__.py app/__init__.py
# Dev-зависимости тоже ставим, чтобы работал `docker compose run --rm api pytest`.
RUN pip install ".[dev]"

COPY app ./app
COPY tests ./tests
# Пересобираем пакет с реальным кодом (зависимости уже установлены).
RUN pip install --no-deps . && chown -R app:app /app

USER app

EXPOSE 8000

CMD ["uvicorn", "--factory", "app.main:create_app", "--host", "0.0.0.0", "--port", "8000"]
