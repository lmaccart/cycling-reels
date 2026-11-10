from __future__ import annotations

from .config import Project
from .library import all_assets, load_asset, update_asset
from .media import load_audio_16k
from .util import ReelsError, log, now_iso, write_json


def load_model(project: Project, model_name: str | None = None):
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise ReelsError("faster-whisper isn't installed — run ./setup.sh") from e
    tc = project.config["transcription"]
    name = model_name or tc["model"]
    log(f"loading Whisper model '{name}' (first run downloads it)…")
    return WhisperModel(name, device=tc["device"], compute_type=tc["computeType"])


def transcribe(
    project: Project,
    ids: list[str] | None = None,
    *,
    force: bool = False,
    model_name: str | None = None,
) -> int:
    """Word-level transcripts with faster-whisper. Without ids: every clip with detected speech."""
    if ids:
        targets = [load_asset(project, i) for i in ids]
    else:
        targets = [
            a
            for a in all_assets(project)
            if a["kind"] == "video"
            and (a.get("probe") or {}).get("hasAudio")
            and ((a.get("audio") or {}).get("speechSeconds") or 0) >= 1.5
            and a.get("status") != "skip"
        ]
    targets = [a for a in targets if not a.get("deleted")]  # the media file is gone
    if not force:
        targets = [a for a in targets if not a.get("transcript")]
    targets = [a for a in targets if a["kind"] == "video" and (a.get("probe") or {}).get("hasAudio")]
    if not targets:
        log("nothing to transcribe")
        return 0

    tc = project.config["transcription"]
    model = load_model(project, model_name)
    hotwords = " ".join(tc.get("keyterms") or []) or None
    done = 0
    for asset in targets:
        audio = load_audio_16k(project.abs(asset["media"]))
        segments, info = model.transcribe(
            audio,
            language=tc.get("language") or None,
            word_timestamps=True,
            vad_filter=True,
            condition_on_previous_text=False,
            beam_size=5,
            hotwords=hotwords,
        )
        segs, words = [], []
        for s in segments:
            segs.append({"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()})
            for w in s.words or []:
                text = w.word.strip()
                if text:
                    words.append({"w": text, "s": round(w.start, 3), "e": round(w.end, 3), "p": round(w.probability, 3)})
        out = project.transcripts_dir / f"{asset['id']}.json"
        write_json(
            out,
            {
                "asset": asset["id"],
                "model": model_name or tc["model"],
                "language": info.language,
                "createdAt": now_iso(),
                "segments": segs,
                "words": words,
            },
        )
        update_asset(project, asset, transcript=project.rel(out))
        text = " ".join(s["text"] for s in segs)
        log(f"transcribed {asset['id']}: {len(words)} words — \"{text[:140]}{'…' if len(text) > 140 else ''}\"")
        done += 1
    return done
