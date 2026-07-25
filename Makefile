.PHONY: setup run test lint mock doctor server-test server-lint remote-smoke stream-test stream-smoke stream-benchmark stream-soak

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
REMOTE_SMOKE_FLAGS ?=
remote-smoke:
	$(VENV_PYTHON) -m ai_voice_interpreter.remote_smoke \
		--audio "$(REMOTE_SMOKE_AUDIO)" --verify-output $(REMOTE_SMOKE_FLAGS)

stream-test:
	$(VENV_PYTHON) -m pytest tests/streaming server/tests/streaming

STREAM_SMOKE_AUDIO ?= /tmp/ai-interpreter-stream-test.wav
STREAM_SMOKE_FLAGS ?=
stream-smoke:
	$(VENV_PYTHON) -m ai_voice_interpreter.stream_smoke \
		--audio "$(STREAM_SMOKE_AUDIO)" --json-report stream-smoke-output/report.json \
		$(STREAM_SMOKE_FLAGS)

STREAM_BENCHMARK_RUNS ?= 20
stream-benchmark:
	$(VENV_PYTHON) -m ai_voice_interpreter.stream_benchmark \
		--runs "$(STREAM_BENCHMARK_RUNS)"

SOAK_MINUTES ?= 30
stream-soak:
	$(VENV_PYTHON) -m ai_voice_interpreter.stream_soak --minutes "$(SOAK_MINUTES)"
