# Команды запускаются интерпретатором из активированного venv
# (или передайте свой: make test PYTHON=.venv/bin/python).
PYTHON ?= python

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
