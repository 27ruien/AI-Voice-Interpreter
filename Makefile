.PHONY: setup run test lint mock

PYTHON ?= python3
VENV := .venv
VENV_PYTHON := $(VENV)/bin/python

setup:
	$(PYTHON) -m venv $(VENV)
	$(VENV_PYTHON) -m pip install --upgrade pip
	$(VENV_PYTHON) -m pip install -e '.[dev]'

run:
	./run_mvp.sh

mock:
	APP_MODE=mock ./run_mvp.sh

test:
	$(VENV_PYTHON) -m pytest

lint:
	$(VENV_PYTHON) -m ruff check .

