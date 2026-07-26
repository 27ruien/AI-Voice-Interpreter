.PHONY: setup run test lint mock doctor server-test server-lint remote-smoke stream-test stream-smoke stream-benchmark stream-soak livetranslate-test provider-permission-smoke livetranslate-smoke pipeline-benchmark meeting-test meeting-doctor meeting-audio-doctor meeting-loopback-smoke meeting-bridge-smoke meeting-bridge-soak

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

livetranslate-test:
	$(VENV_PYTHON) -m pytest \
		server/tests/streaming/test_livetranslate_provider.py \
		server/tests/streaming/test_livetranslate_gateway.py \
		server/tests/streaming/test_mock_livetranslate.py

provider-permission-smoke:
	./scripts/provider_permission_smoke.sh

LIVETRANSLATE_SMOKE_FLAGS ?=
livetranslate-smoke:
	$(VENV_PYTHON) -m ai_voice_interpreter.livetranslate_smoke \
		--json-report livetranslate-smoke-output/report.json \
		$(LIVETRANSLATE_SMOKE_FLAGS)

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

pipeline-benchmark:
	$(VENV_PYTHON) -m ai_voice_interpreter.pipeline_benchmark

SOAK_MINUTES ?= 30
stream-soak:
	$(VENV_PYTHON) -m ai_voice_interpreter.stream_soak --minutes "$(SOAK_MINUTES)"

meeting-test:
	$(VENV_PYTHON) -m pytest \
		tests/meeting \
		server/tests/streaming/test_bridge_registry.py

meeting-doctor:
	$(VENV_PYTHON) -m ai_voice_interpreter.meeting.doctor

MEETING_AUDIO_DOCTOR_FLAGS ?=
meeting-audio-doctor:
	$(VENV_PYTHON) -m ai_voice_interpreter.meeting.audio_doctor \
		--json-report meeting-audio-doctor-output/report.json \
		$(MEETING_AUDIO_DOCTOR_FLAGS)

meeting-loopback-smoke:
	$(VENV_PYTHON) -m ai_voice_interpreter.meeting.audio_doctor \
		--json-report meeting-loopback-output/report.json --duration 1

MEETING_BRIDGE_SMOKE_FLAGS ?= --no-real-api
meeting-bridge-smoke:
	$(VENV_PYTHON) -m ai_voice_interpreter.meeting_bridge_smoke \
		--json-report meeting-bridge-smoke-output/report.json \
		$(MEETING_BRIDGE_SMOKE_FLAGS)

MEETING_SOAK_MINUTES ?= 30
meeting-bridge-soak:
	$(VENV_PYTHON) -m ai_voice_interpreter.meeting.soak \
		--minutes "$(MEETING_SOAK_MINUTES)" \
		--json-report meeting-bridge-soak-output/report.json
