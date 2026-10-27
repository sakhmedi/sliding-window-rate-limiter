# Какой Python использовать (по порядку):
#   1. явно переданный: make test PYTHON=/path/to/python
#   2. из активированного venv ($VIRTUAL_ENV);
#   3. из ./.venv, даже если он не активирован (Linux/macOS или Windows);
#   4. python3 из PATH.
ifdef VIRTUAL_ENV
  PYTHON ?= python
else ifneq ($(wildcard .venv/bin/python),)
  PYTHON ?= .venv/bin/python
else ifneq ($(wildcard .venv/Scripts/python.exe),)
  PYTHON ?= .venv/Scripts/python.exe
else
  PYTHON ?= python3
endif

.PHONY: install lint format test up down

install:
	$(PYTHON) -m pip install -e ".[dev]"

lint:
	$(PYTHON) -m ruff check .
	$(PYTHON) -m ruff format --check .

format:
	$(PYTHON) -m ruff check --fix .
	$(PYTHON) -m ruff format .

test:
	$(PYTHON) -m pytest

up:
	docker compose up --build -d

down:
	docker compose down
