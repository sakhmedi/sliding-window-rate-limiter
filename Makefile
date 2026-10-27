# Make сам управляет виртуальным окружением ./.venv:
# `make test` или `make lint` на чистом клоне создают .venv и ставят зависимости.
# Повторная установка происходит, только если изменился pyproject.toml.

# Системный Python, которым создаётся .venv (можно переопределить: make test BOOTSTRAP_PYTHON=python3.12).
# На Windows и пути внутри venv другие.
ifeq ($(OS),Windows_NT)
  BOOTSTRAP_PYTHON ?= python
  PYTHON := .venv/Scripts/python.exe
else
  BOOTSTRAP_PYTHON ?= python3
  PYTHON := .venv/bin/python
endif

# Файл-метка: «зависимости установлены». Make сравнивает его время с pyproject.toml.
DEPS_STAMP := .venv/.deps-installed

.PHONY: install lint format test up down

$(DEPS_STAMP): pyproject.toml
	$(BOOTSTRAP_PYTHON) -m venv .venv
	$(PYTHON) -m pip install --upgrade pip
	$(PYTHON) -m pip install -e ".[dev]"
	$(PYTHON) -c "import pathlib; pathlib.Path('$(DEPS_STAMP)').touch()"

install: $(DEPS_STAMP)

lint: $(DEPS_STAMP)
	$(PYTHON) -m ruff check .
	$(PYTHON) -m ruff format --check .
	$(PYTHON) -m mypy --strict app tests

format: $(DEPS_STAMP)
	$(PYTHON) -m ruff format .
	$(PYTHON) -m ruff check --fix .

test: $(DEPS_STAMP)
	$(PYTHON) -m pytest

up:
	docker compose up --build -d

down:
	docker compose down
