from fastapi.testclient import TestClient

from ai_voice_interpreter.streaming.mock_harness import build_mock_app, run_mock_turn


def test_mock_harness_is_explicitly_offline_modular(tmp_path) -> None:  # type: ignore[no-untyped-def]
    app = build_mock_app(tmp_path)
    assert app.state.config.stream_pipeline_provider == "modular"
    with TestClient(app) as client:
        result = run_mock_turn(client)
    assert result["success"] is True
    assert result["fallback"] is False
