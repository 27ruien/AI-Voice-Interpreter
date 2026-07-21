from __future__ import annotations

import logging
import subprocess
import threading
from pathlib import Path

from ..exceptions import PlaybackError

logger = logging.getLogger(__name__)


class MacAudioPlayer:
    def __init__(self, executable: Path = Path("/usr/bin/afplay")) -> None:
        self.executable = executable
        self._process: subprocess.Popen[bytes] | None = None
        self._lock = threading.RLock()

    @property
    def is_playing(self) -> bool:
        with self._lock:
            return self._process is not None and self._process.poll() is None

    def play(self, audio_path: Path) -> None:
        path = Path(audio_path)
        if not path.is_file():
            raise PlaybackError(f"音频文件不存在：{path}")
        if not self.executable.is_file():
            raise PlaybackError("未找到 macOS 系统播放器 /usr/bin/afplay。")
        self.stop()
        logger.info("Playback started file=%s", path.name)
        process: subprocess.Popen[bytes] | None = None
        try:
            process = subprocess.Popen(
                [str(self.executable), str(path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            with self._lock:
                self._process = process
            _, stderr = process.communicate()
            if process.returncode not in {0, -15}:
                detail = stderr.decode("utf-8", errors="replace").strip()
                raise PlaybackError(f"播放失败：{detail or f'afplay 退出码 {process.returncode}'}")
            logger.info("Playback finished")
        except PlaybackError:
            raise
        except Exception as exc:
            logger.exception("Playback failed type=%s", type(exc).__name__)
            raise PlaybackError(f"无法播放音频：{exc}") from exc
        finally:
            with self._lock:
                if process is not None and self._process is process:
                    self._process = None

    def stop(self) -> None:
        with self._lock:
            process = self._process
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
        with self._lock:
            if self._process is process:
                self._process = None
