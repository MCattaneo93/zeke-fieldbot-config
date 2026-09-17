"""Local voice-note transcription with faster-whisper.

No audio is uploaded to an external transcription API. The model is loaded on
first use and cached by faster-whisper for later runs.
"""

from __future__ import annotations

import logging


log = logging.getLogger("fieldbot.voice")
_model = None


def transcribe(path: str) -> str:
    global _model
    if _model is None:
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise RuntimeError(
                "Local voice transcription is not installed. Run: "
                "python3 -m pip install -r requirements-voice.txt"
            ) from exc
        log.info("loading local faster-whisper base model")
        _model = WhisperModel("base", device="cpu", compute_type="int8")
    segments, info = _model.transcribe(path, vad_filter=True)
    text = " ".join(segment.text.strip() for segment in segments).strip()
    log.info("transcribed audio duration_seconds=%.1f chars=%d", info.duration, len(text))
    return text
