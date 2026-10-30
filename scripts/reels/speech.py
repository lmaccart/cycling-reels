from __future__ import annotations

from pathlib import Path

from .media import load_audio_16k


def detect_speech(path: Path, duration: float) -> dict | None:
    """Finds stretches of talking with the Silero VAD bundled in faster-whisper.

    Cheap (no Whisper model download) and good enough to decide which clips
    are worth transcribing. Wind noise occasionally trips it; the transcript
    review step catches those.
    """
    if duration > 3 * 3600:
        return None
    try:
        from faster_whisper.vad import VadOptions, get_speech_timestamps
    except Exception:  # noqa: BLE001 - optional dependency
        return None
    try:
        audio = load_audio_16k(path)
    except Exception as e:  # noqa: BLE001
        return {"speechError": str(e)[:200]}
    if len(audio) == 0:
        return {"speechSeconds": 0.0, "speechRanges": []}
    stamps = get_speech_timestamps(audio, VadOptions(min_silence_duration_ms=600, speech_pad_ms=200))
    ranges = [[round(t["start"] / 16000, 2), round(t["end"] / 16000, 2)] for t in stamps]
    total = round(sum(e - s for s, e in ranges), 2)
    return {"speechSeconds": total, "speechRanges": ranges[:60]}
