from ai_voice_interpreter.pipeline_benchmark import build_report


def test_pipeline_benchmark_never_invents_missing_sample_or_percentile() -> None:
    report = build_report(
        {
            "pipeline_provider": "livetranslate",
            "first_translation_ms": 1200,
            "first_audio_ms": 1800,
            "client_first_playback_ms": 1850,
            "tts_audio_chunks": 4,
            "tts_audio_bytes": 1000,
        },
        None,
    )
    assert report["livetranslate"]["sample_count"] == 1
    assert report["modular"] == {"sample_count": 0, "status": "unavailable"}
    assert report["percentiles"] == "not_calculated_single_or_missing_sample"
