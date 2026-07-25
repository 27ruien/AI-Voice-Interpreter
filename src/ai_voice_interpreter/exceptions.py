"""User-facing application exceptions."""


class InterpreterError(Exception):
    """Base class for errors safe to summarize in the UI."""


class ConfigurationError(InterpreterError):
    pass


class MicrophonePermissionError(InterpreterError):
    pass


class AudioCaptureError(InterpreterError):
    pass


class ASRProviderError(InterpreterError):
    pass


class TranslationProviderError(InterpreterError):
    pass


class TTSProviderError(InterpreterError):
    pass


class PlaybackError(InterpreterError):
    pass


class VoiceEnrollmentError(InterpreterError):
    pass


class GatewayError(InterpreterError):
    def __init__(self, message: str, request_id: str | None = None) -> None:
        super().__init__(message)
        self.request_id = request_id
