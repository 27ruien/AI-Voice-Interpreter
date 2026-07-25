.PHONY: setup run test lint mock doctor server-test server-lint remote-smoke

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

doctor:
	$(VENV_PYTHON) -m ai_voice_interpreter.doctor

server-test:
	$(VENV_PYTHON) -m pytest server/tests

server-lint:
	$(VENV_PYTHON) -m ruff check server

REMOTE_SMOKE_AUDIO ?= /tmp/ai-interpreter-test.wav
remote-smoke:
	$(VENV_PYTHON) -m ai_voice_interpreter.remote_smoke \
		--audio "$(REMOTE_SMOKE_AUDIO)" --verify-output
