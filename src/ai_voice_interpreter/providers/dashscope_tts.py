from __future__ import annotations

import logging
import shutil
import tempfile
import time
from pathlib import Path

from ..config import AppConfig
from ..exceptions import ConfigurationError, TTSProviderError
from ..models import TTSResult
from .common import configure_dashscope, friendly_service_message

logger = logging.getLogger(__name__)


class DashScopeTextToSpeech:
    def __init__(self, config: AppConfig, output_dir: Path | None = None) -> None:
        self.config = config
        self._owned_dir = output_dir is None
        self.output_dir = output_dir or Path(tempfile.mkdtemp(prefix="aivi-tts-"))
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def synthesize(self, text: str, voice: str | None = None) -> TTSResult:
        if not text.strip():
            raise TTSProviderError("翻译文本为空，无法生成语音。")
        selected_voice = voice or self.config.effective_tts_voice
        self._validate_voice(selected_voice)
        started = time.perf_counter()
        voice_mode = "cloned" if self.config.cloned_voice_id else "system"
        logger.info(
            "TTS provider started model=%s voice_mode=%s text_length=%d",
            self.config.tts_model,
            voice_mode,
            len(text),
        )
        try:
            configure_dashscope(self.config)
            from dashscope.audio.tts_v2 import AudioFormat, SpeechSynthesizer

            synthesizer_kwargs = {}
            if self.config.dashscope_workspace_id:
                synthesizer_kwargs["workspace"] = self.config.dashscope_workspace_id
            synthesizer = SpeechSynthesizer(
                model=self.config.tts_model,
                voice=selected_voice,
                format=AudioFormat.WAV_24000HZ_MONO_16BIT,
                language_hints=[self.config.target_language],
                **synthesizer_kwargs,
            )
            audio = synthesizer.call(
                text.strip(),
                timeout_millis=int(self.config.network_timeout_seconds * 1000),
            )
            request_id = synthesizer.get_last_request_id()
            if not audio:
                detail = synthesizer.get_response() or "服务未返回音频"
                raise TTSProviderError(friendly_service_message(detail))
            output_path = self.output_dir / f"tts-{time.time_ns()}.wav"
            output_path.write_bytes(audio)
            duration_ms = (time.perf_counter() - started) * 1000
            logger.info(
                "TTS completed elapsed_ms=%.1f request_id=%s voice_mode=%s",
                duration_ms,
                request_id,
                voice_mode,
            )
            logger.info(
                "TTS audio written path=%s bytes=%d",
                output_path,
                output_path.stat().st_size,
            )
            return TTSResult(
                audio_path=output_path,
                audio_format="wav",
                duration_ms=duration_ms,
                provider="dashscope",
                model=self.config.tts_model,
                voice=selected_voice,
                request_id=request_id,
            )
        except (ConfigurationError, TTSProviderError):
            raise
        except Exception as exc:
            logger.exception("TTS failed type=%s", type(exc).__name__)
            raise TTSProviderError(friendly_service_message(exc)) from exc

    def cleanup(self) -> None:
        if self._owned_dir and not self.config.keep_temp_audio:
            shutil.rmtree(self.output_dir, ignore_errors=True)

    def _validate_voice(self, voice: str) -> None:
        if self.config.cloned_voice_id:
            if voice != self.config.cloned_voice_id:
                raise ConfigurationError("已配置克隆音色，但合成请求未使用该音色。")
            if not voice.startswith(f"{self.config.tts_model}-"):
                raise ConfigurationError("克隆音色与 TTS_MODEL 不匹配，不能静默降级到系统音色。")
