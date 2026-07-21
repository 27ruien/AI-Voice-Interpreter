import logging
import subprocess
import wave
from pathlib import Path

import numpy as np
import pytest

from ai_voice_interpreter.audio.player import MacAudioPlayer
from ai_voice_interpreter.audio.recorder import MicrophoneRecorder
from ai_voice_interpreter.exceptions import PlaybackError


class FakeStream:
    def stop(self) -> None:
        pass

    def close(self) -> None:
        pass


def test_recorder_writes_16khz_mono_pcm_wav_and_cleans_temp(tmp_path: Path) -> None:
    recorder = MicrophoneRecorder(minimum_duration_seconds=0.01)
    temp_dir = recorder._temp_dir
    recorder._stream = FakeStream()
    recorder._frames = [np.zeros((1600, 1), dtype=np.int16)]
    path = recorder.stop()
    with wave.open(str(path), "rb") as wav_file:
        assert wav_file.getframerate() == 16000
        assert wav_file.getnchannels() == 1
        assert wav_file.getsampwidth() == 2
    recorder.cleanup()
    assert not temp_dir.exists()


def test_recorder_rejects_too_short_audio() -> None:
    recorder = MicrophoneRecorder(minimum_duration_seconds=0.5)
    recorder._stream = FakeStream()
    recorder._frames = [np.zeros((100, 1), dtype=np.int16)]
    with pytest.raises(Exception, match="录音太短"):
        recorder.stop()
    recorder.cleanup()


def test_player_reports_missing_file(tmp_path: Path) -> None:
    with pytest.raises(PlaybackError, match="不存在"):
        MacAudioPlayer().play(tmp_path / "missing.wav")


def test_player_logs_afplay_launch_and_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"audio")
    executable = tmp_path / "afplay"
    executable.write_text("", encoding="utf-8")

    class FakeProcess:
        pid = 123
        returncode = 0

        def communicate(self) -> tuple[bytes, bytes]:
            return b"", b""

        def poll(self) -> int:
            return 0

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    caplog.set_level(logging.INFO)
    MacAudioPlayer(executable).play(audio)
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "afplay started pid=123" in messages
    assert "afplay finished returncode=0" in messages
