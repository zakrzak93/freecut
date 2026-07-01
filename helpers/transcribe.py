"""Transcribe a video with a pluggable ASR backend.

Extracts mono 16kHz audio via ffmpeg, runs the selected backend, then writes
a normalized JSON to <edit_dir>/transcripts/<video_stem>.json.

The output shape is fixed and consumed downstream by pack_transcripts.py:

    {
      "words": [
        {"type": "word"|"spacing"|"audio_event",
         "text": str,
         "start": float (sec),
         "end":   float (sec),
         "speaker_id": "speaker_0" | "speaker_1" | ... }
      ]
    }

Backends:
    whisper     — DEFAULT. Local, free. Uses mlx-whisper on Apple Silicon if
                  available, otherwise faster-whisper. Single speaker (no
                  diarization) — every word gets speaker_id "speaker_0".
    vibevoice   — Calls an HTTP endpoint (env VIBEVOICE_ASR_URL) that serves
                  the microsoft/VibeVoice-ASR model. Multi-speaker diarization.
    elevenlabs  — Original ElevenLabs Scribe path. Needs ELEVENLABS_API_KEY.

Cached: if the output file already exists, transcription is skipped.

Usage:
    python helpers/transcribe.py <video_path>
    python helpers/transcribe.py <video_path> --backend whisper --model small
    python helpers/transcribe.py <video_path> --backend vibevoice
    python helpers/transcribe.py <video_path> --backend elevenlabs --num-speakers 2
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests


SCRIBE_URL = "https://api.elevenlabs.io/v1/speech-to-text"
DEFAULT_BACKEND = "whisper"
DEFAULT_WHISPER_MODEL = "small"


# ---------------------------------------------------------------------------
# .env loading
# ---------------------------------------------------------------------------

def _read_env_file() -> dict[str, str]:
    out: dict[str, str] = {}
    for candidate in [Path(__file__).resolve().parent.parent / ".env", Path(".env")]:
        if not candidate.exists():
            continue
        for line in candidate.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _env(name: str) -> str:
    return os.environ.get(name, "") or _read_env_file().get(name, "")


def load_api_key() -> str:
    """Return the ElevenLabs API key. Kept for import back-compat with
    transcribe_batch.py; only used by the elevenlabs backend."""
    v = _env("ELEVENLABS_API_KEY")
    if not v:
        sys.exit("ELEVENLABS_API_KEY not found in .env or environment")
    return v


# ---------------------------------------------------------------------------
# ffmpeg audio extraction
# ---------------------------------------------------------------------------

def extract_audio(video_path: Path, dest: Path) -> None:
    cmd = [
        "ffmpeg", "-y", "-i", str(video_path),
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
        str(dest),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ---------------------------------------------------------------------------
# Shared helpers for the normalized shape
# ---------------------------------------------------------------------------

def _interleave_spacing(word_entries: list[dict]) -> list[dict]:
    """Insert 'spacing' entries between consecutive words so that
    pack_transcripts.py can detect silence gaps. Words are assumed sorted."""
    out: list[dict] = []
    prev_end: float | None = None
    for w in word_entries:
        start = float(w["start"])
        if prev_end is not None and start > prev_end:
            out.append({
                "type": "spacing",
                "text": " ",
                "start": prev_end,
                "end": start,
            })
        out.append(w)
        prev_end = float(w["end"])
    return out


# ---------------------------------------------------------------------------
# Backend: whisper (local, free, single-speaker)
# ---------------------------------------------------------------------------

def _mlx_whisper_available() -> bool:
    try:
        import mlx_whisper  # noqa: F401
        return True
    except Exception:
        return False


def _faster_whisper_available() -> bool:
    try:
        import faster_whisper  # noqa: F401
        return True
    except Exception:
        return False


def transcribe_whisper(
    audio_path: Path,
    model: str = DEFAULT_WHISPER_MODEL,
    language: str | None = None,
) -> dict:
    """Local Whisper transcription with word-level timestamps.

    Prefers mlx-whisper on Apple Silicon; falls back to faster-whisper.
    Returns the normalized {"words": [...]} shape with speaker_id="speaker_0".
    """
    word_entries: list[dict] = []

    if _mlx_whisper_available():
        import mlx_whisper
        # mlx-whisper uses HuggingFace repo IDs. Map common short names.
        repo_map = {
            "tiny": "mlx-community/whisper-tiny-mlx",
            "base": "mlx-community/whisper-base-mlx",
            "small": "mlx-community/whisper-small-mlx",
            "medium": "mlx-community/whisper-medium-mlx",
            "large": "mlx-community/whisper-large-v3-mlx",
            "large-v3": "mlx-community/whisper-large-v3-mlx",
            "large-v3-turbo": "mlx-community/whisper-large-v3-turbo",
        }
        path_or_repo = repo_map.get(model, model)
        result = mlx_whisper.transcribe(
            str(audio_path),
            path_or_hf_repo=path_or_repo,
            language=language,
            word_timestamps=True,
        )
        for seg in result.get("segments", []):
            for w in seg.get("words", []) or []:
                text = (w.get("word") or "").strip()
                if not text:
                    continue
                word_entries.append({
                    "type": "word",
                    "text": text,
                    "start": float(w["start"]),
                    "end": float(w["end"]),
                    "speaker_id": "speaker_0",
                })

    elif _faster_whisper_available():
        from faster_whisper import WhisperModel
        wm = WhisperModel(model, device="auto", compute_type="auto")
        segments, _info = wm.transcribe(
            str(audio_path),
            language=language,
            word_timestamps=True,
            vad_filter=False,
        )
        for seg in segments:
            for w in (seg.words or []):
                text = (w.word or "").strip()
                if not text:
                    continue
                word_entries.append({
                    "type": "word",
                    "text": text,
                    "start": float(w.start),
                    "end": float(w.end),
                    "speaker_id": "speaker_0",
                })

    else:
        sys.exit(
            "whisper backend needs one of:\n"
            "  pip install mlx-whisper       # Apple Silicon (recommended)\n"
            "  pip install faster-whisper    # NVIDIA / CPU fallback"
        )

    return {"words": _interleave_spacing(word_entries)}


# ---------------------------------------------------------------------------
# Backend: vibevoice (HTTP endpoint, multi-speaker diarization)
# ---------------------------------------------------------------------------

def transcribe_vibevoice(
    audio_path: Path,
    url: str | None = None,
    language: str | None = None,
) -> dict:
    """Call a VibeVoice-ASR HTTP endpoint. The endpoint is expected to accept
    a multipart upload of a wav file and return either

        {"words": [...]}                                 # already normalized
    or  {"segments": [{"speaker": "SPEAKER_0",           # Who/When/What
                       "start": 0.12, "end": 3.4,
                       "text": "hello world",
                       "words": [{"text":"hello","start":0.12,"end":0.42}, ...]}]}

    If per-word timestamps are absent, we linearly interpolate across the
    segment text so pack_transcripts still gets word-level entries.
    """
    endpoint = url or _env("VIBEVOICE_ASR_URL")
    if not endpoint:
        sys.exit(
            "vibevoice backend requires VIBEVOICE_ASR_URL (env or .env).\n"
            "VibeVoice-ASR is CUDA-only; point this at a rented GPU box, "
            "Modal/RunPod deploy, or an Azure AI Foundry endpoint."
        )

    data: dict[str, str] = {}
    if language:
        data["language"] = language

    with open(audio_path, "rb") as f:
        resp = requests.post(
            endpoint,
            files={"file": (audio_path.name, f, "audio/wav")},
            data=data,
            timeout=1800,
        )
    if resp.status_code != 200:
        raise RuntimeError(f"vibevoice endpoint returned {resp.status_code}: {resp.text[:500]}")
    payload = resp.json()

    # Already normalized?
    if isinstance(payload, dict) and isinstance(payload.get("words"), list) and payload["words"]:
        first = payload["words"][0]
        if all(k in first for k in ("type", "text", "start", "end", "speaker_id")):
            return payload

    # Convert VibeVoice segments → words[] with speaker_N IDs.
    segments = []
    if isinstance(payload, dict):
        segments = payload.get("segments") or payload.get("results") or []

    # Stable mapping from arbitrary speaker labels → speaker_0, speaker_1, ...
    speaker_ids: dict[str, str] = {}

    def sid(label: object) -> str:
        key = str(label) if label is not None else "0"
        if key not in speaker_ids:
            speaker_ids[key] = f"speaker_{len(speaker_ids)}"
        return speaker_ids[key]

    word_entries: list[dict] = []
    for seg in segments:
        spk_label = seg.get("speaker") or seg.get("speaker_id") or seg.get("who") or "0"
        speaker_id = sid(spk_label)
        seg_start = float(seg.get("start", 0.0))
        seg_end = float(seg.get("end", seg_start))
        seg_words = seg.get("words") or []

        if seg_words:
            for w in seg_words:
                text = (w.get("text") or w.get("word") or "").strip()
                if not text:
                    continue
                word_entries.append({
                    "type": "word",
                    "text": text,
                    "start": float(w.get("start", seg_start)),
                    "end": float(w.get("end", seg_end)),
                    "speaker_id": speaker_id,
                })
        else:
            # Fall back to linear interpolation across the segment text.
            tokens = (seg.get("text") or "").split()
            if not tokens:
                continue
            span = max(seg_end - seg_start, 1e-3)
            per = span / len(tokens)
            for i, tok in enumerate(tokens):
                word_entries.append({
                    "type": "word",
                    "text": tok,
                    "start": seg_start + i * per,
                    "end": seg_start + (i + 1) * per,
                    "speaker_id": speaker_id,
                })

    word_entries.sort(key=lambda w: w["start"])
    return {"words": _interleave_spacing(word_entries)}


# ---------------------------------------------------------------------------
# Backend: elevenlabs (Scribe — original path, still supported)
# ---------------------------------------------------------------------------

def call_scribe(
    audio_path: Path,
    api_key: str,
    language: str | None = None,
    num_speakers: int | None = None,
) -> dict:
    data: dict[str, str] = {
        "model_id": "scribe_v1",
        "diarize": "true",
        "tag_audio_events": "true",
        "timestamps_granularity": "word",
    }
    if language:
        data["language_code"] = language
    if num_speakers:
        data["num_speakers"] = str(num_speakers)

    with open(audio_path, "rb") as f:
        resp = requests.post(
            SCRIBE_URL,
            headers={"xi-api-key": api_key},
            files={"file": (audio_path.name, f, "audio/wav")},
            data=data,
            timeout=1800,
        )
    if resp.status_code != 200:
        raise RuntimeError(f"Scribe returned {resp.status_code}: {resp.text[:500]}")
    return resp.json()


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def _run_backend(
    backend: str,
    audio: Path,
    language: str | None,
    num_speakers: int | None,
    whisper_model: str,
    vibevoice_url: str | None,
    api_key: str | None,
) -> dict:
    if backend == "whisper":
        return transcribe_whisper(audio, model=whisper_model, language=language)
    if backend == "vibevoice":
        return transcribe_vibevoice(audio, url=vibevoice_url, language=language)
    if backend == "elevenlabs":
        key = api_key or load_api_key()
        return call_scribe(audio, key, language=language, num_speakers=num_speakers)
    sys.exit(f"unknown backend: {backend}")


def transcribe_one(
    video: Path,
    edit_dir: Path,
    api_key: str | None = None,
    language: str | None = None,
    num_speakers: int | None = None,
    verbose: bool = True,
    backend: str = DEFAULT_BACKEND,
    whisper_model: str = DEFAULT_WHISPER_MODEL,
    vibevoice_url: str | None = None,
) -> Path:
    """Transcribe a single video. Returns path to transcript JSON.

    Cached: returns existing path immediately if the transcript already exists.
    `api_key` is only consulted by the elevenlabs backend; other backends
    ignore it. Kept in the signature for import back-compat with
    transcribe_batch.py.
    """
    transcripts_dir = edit_dir / "transcripts"
    transcripts_dir.mkdir(parents=True, exist_ok=True)
    out_path = transcripts_dir / f"{video.stem}.json"

    if out_path.exists():
        if verbose:
            print(f"cached: {out_path.name}")
        return out_path

    if verbose:
        print(f"  extracting audio from {video.name}", flush=True)

    t0 = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        audio = Path(tmp) / f"{video.stem}.wav"
        extract_audio(video, audio)
        size_mb = audio.stat().st_size / (1024 * 1024)
        if verbose:
            print(f"  {backend}: {video.stem}.wav ({size_mb:.1f} MB)", flush=True)
        payload = _run_backend(
            backend=backend,
            audio=audio,
            language=language,
            num_speakers=num_speakers,
            whisper_model=whisper_model,
            vibevoice_url=vibevoice_url,
            api_key=api_key,
        )

    out_path.write_text(json.dumps(payload, indent=2))
    dt = time.time() - t0

    if verbose:
        kb = out_path.stat().st_size / 1024
        print(f"  saved: {out_path.name} ({kb:.1f} KB) in {dt:.1f}s")
        if isinstance(payload, dict) and "words" in payload:
            print(f"    words: {len(payload['words'])}")

    return out_path


def main() -> None:
    ap = argparse.ArgumentParser(description="Transcribe a video (pluggable backend)")
    ap.add_argument("video", type=Path, help="Path to video file")
    ap.add_argument(
        "--edit-dir",
        type=Path,
        default=None,
        help="Edit output directory (default: <video_parent>/edit)",
    )
    ap.add_argument(
        "--backend",
        choices=["whisper", "vibevoice", "elevenlabs"],
        default=DEFAULT_BACKEND,
        help=f"Transcription backend (default: {DEFAULT_BACKEND}).",
    )
    ap.add_argument(
        "--model",
        default=DEFAULT_WHISPER_MODEL,
        help=f"Whisper model size or HF repo (default: {DEFAULT_WHISPER_MODEL}). "
             "whisper backend only.",
    )
    ap.add_argument(
        "--vibevoice-url",
        default=None,
        help="Override VIBEVOICE_ASR_URL. vibevoice backend only.",
    )
    ap.add_argument(
        "--language",
        type=str,
        default=None,
        help="Optional ISO language code (e.g., 'en'). Omit to auto-detect.",
    )
    ap.add_argument(
        "--num-speakers",
        type=int,
        default=None,
        help="Optional number of speakers. elevenlabs backend only.",
    )
    args = ap.parse_args()

    video = args.video.resolve()
    if not video.exists():
        sys.exit(f"video not found: {video}")

    edit_dir = (args.edit_dir or (video.parent / "edit")).resolve()

    transcribe_one(
        video=video,
        edit_dir=edit_dir,
        api_key=None,
        language=args.language,
        num_speakers=args.num_speakers,
        backend=args.backend,
        whisper_model=args.model,
        vibevoice_url=args.vibevoice_url,
    )


if __name__ == "__main__":
    main()
