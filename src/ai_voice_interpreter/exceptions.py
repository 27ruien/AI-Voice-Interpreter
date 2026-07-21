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

