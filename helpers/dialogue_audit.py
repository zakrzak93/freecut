"""Local source-time dialogue evidence, never an automatic deletion decision.

prepare --source clip.mp4 --out-dir edit/audit [--plan plan.json] [--windows windows.json]
timeline --plan plan.json --out-dir edit/timeline-audit
transcribe --manifest edit/audit/<key>/manifest.json [--language pl]
events --manifest ... [--transcript .../transcript.json] [--classify]

JSON windows are [[start, end], ...] or objects with start/end; a plan can
contain ranges or kept_ranges. Prepare times are ORIGINAL source seconds;
timeline times are edited OUTPUT seconds and carry a fingerprinted source map.
ASR-empty audio is explicitly unresolved, never classified as silence.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
import math
import os
from pathlib import Path
import platform
import subprocess
import wave
from fractions import Fraction

VERSION = 4
SR = 16000


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def key(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:24]


def fingerprint(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            h.update(block)
    return {"path": str(Path(path).resolve()), "sha256": h.hexdigest()}


def ranges(value, duration):
    if isinstance(value, dict):
        value = value.get("ranges", value.get("kept_ranges", value.get("windows")))
    if not isinstance(value, list):
        raise ValueError("Expected a list of source-time ranges")
    result = []
    for row in value:
        a, b = (row["start"], row["end"]) if isinstance(row, dict) else row
        a, b = float(a), float(b)
        if not (math.isfinite(a) and math.isfinite(b) and 0 <= a < b <= duration + 1 / SR):
            raise ValueError(f"Invalid source range: {a}, {b}; duration={duration}")
        result.append([a, min(b, duration)])
    return sorted(result)


def merge(intervals):
    out = []
    for a, b in sorted(intervals):
        if out and a <= out[-1][1]:
            out[-1][1] = max(b, out[-1][1])
        else:
            out.append([a, b])
    return out


def subtract(intervals, exclusions):
    result = []
    for a, b in merge(intervals):
        cursor = a
        for lo, hi in merge(exclusions):
            if hi <= cursor or lo >= b:
                continue
            if lo > cursor:
                result.append([cursor, lo])
            cursor = max(cursor, hi)
            if cursor >= b:
                break
        if cursor < b:
            result.append([cursor, b])
    return result


def bounded(intervals, max_seconds, overlap):
    if not math.isfinite(max_seconds) or not math.isfinite(overlap) or max_seconds <= 0 or not 0 <= overlap < max_seconds:
        raise ValueError("Require max-window > overlap >= 0")
    out = []
    for a, b in intervals:
        while a < b:
            end = min(a + max_seconds, b)
            out.append([a, end])
            if end >= b:
                break
            a += max_seconds - overlap
    return out


def quiet_gaps(samples, sr=SR, threshold=-42, frame_seconds=.01, minimum=.35):
    import numpy as np
    n = max(1, round(sr * frame_seconds))
    result, start = [], None
    for i in range(0, len(samples), n):
        frame = samples[i:i + n].astype("float64")
        rms = float(np.sqrt(np.mean((frame - frame.mean()) ** 2)))
        quiet = 20 * math.log10(max(rms, 1e-12)) < threshold
        if quiet and start is None:
            start = i / sr
        if not quiet and start is not None:
            if i / sr - start >= minimum:
                result.append([start, i / sr])
            start = None
    if start is not None and len(samples) / sr - start >= minimum:
        result.append([start, len(samples) / sr])
    return result


def make_windows(kept, gaps, duration, context=.08, max_seconds=18, overlap=3):
    islands = subtract(kept, gaps)
    # Every acoustic island gets its own contextual crop, including tiny syllables.
    # Never let context cross the midpoint to an adjacent acoustic island.
    contextual = []
    for i, (a, b) in enumerate(islands):
        left = (islands[i - 1][1] + a) / 2 if i else 0
        right = (b + islands[i + 1][0]) / 2 if i + 1 < len(islands) else duration
        contextual.append([max(left, a - context), min(right, b + context)])
    return bounded(contextual, max_seconds, overlap), islands


def validate_plan_source(plan, source):
    if plan is None:
        return
    if isinstance(plan, dict):
        if "sources" in plan:
            raise ValueError("Multi-source plans are unsupported; supply one source-time range list")
        declared = plan.get("source")
        if declared is not None:
            if not isinstance(declared, str) or Path(declared).resolve() != Path(source).resolve():
                raise ValueError("Plan source must match --source")
        rows = plan.get("ranges", plan.get("kept_ranges", []))
    elif isinstance(plan, list):
        rows = plan
    else:
        raise ValueError("Expected a range list or single-source plan object")
    for row in rows:
        if isinstance(row, dict):
            for field in ("source", "source_path", "file", "file_path"):
                if field in row and Path(row[field]).resolve() != Path(source).resolve():
                    raise ValueError("Mixed-source range plans are unsupported")


def event_windows(data, words, has_transcript, window, overlap):
    result = [(a, b, "broad") for a, b in bounded(data["kept_ranges"], window, overlap)]
    # ASR can hallucinate words over a cough or smooth a tiny failed start.
    # Classify each short island independently even when ASR claims speech there.
    result.extend((a, b, "isolated_acoustic_island") for a, b in data["acoustic_islands"]
                  if 0 < b - a <= window)
    if has_transcript:
        isolated = [[a, b] for a, b in subtract(data["acoustic_islands"], words) if b - a >= .08]
        result.extend((a, b, "isolated_speech_uncovered") for a, b in bounded(isolated, window, overlap))
    if data["settings"].get("target_windows") is not None:
        result.extend((a, b, "explicit_target") for a, b in bounded(data["windows"], window, overlap))
    return result


def load_audio(path):
    import numpy as np
    with wave.open(str(path), "rb") as wav:
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) != (1, 2, SR):
            raise ValueError("Audit WAV must be mono PCM16 at 16000 Hz")
        return np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2").astype("float32") / 32768


def delivery_module():
    spec = importlib.util.spec_from_file_location("_audit_delivery", Path(__file__).with_name("dialogue_delivery.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def timeline(args):
    """Review the exact canonical edit, including XML-only deliveries, without fades."""
    plan_path = Path(args.plan).resolve()
    plan_bytes = plan_path.read_bytes()
    plan_fp = {"path": str(plan_path), "sha256": hashlib.sha256(plan_bytes).hexdigest()}
    plan = json.loads(plan_bytes.decode("utf-8-sig"))
    if fingerprint(plan_path) != plan_fp:
        raise ValueError("Timeline plan changed while reading; run timeline again")
    if not isinstance(plan, dict) or not isinstance(plan.get("source"), str):
        raise ValueError("Timeline requires a canonical single-source plan with source")
    source = Path(plan["source"])
    if not source.is_absolute():
        source = plan_path.parent / source
    source = source.resolve()
    validate_plan_source(dict(plan, source=str(source)), source)
    source_fp = fingerprint(source)
    for field in ("source_sha256", "source_hash"):
        if field in plan and plan[field] != source_fp["sha256"]:
            raise ValueError(f"Stale plan {field}")
    settings = {"version": VERSION, "mode": "timeline", "source": source_fp,
                "plan": plan_fp, "sr": SR, "max_window": args.max_window,
                "overlap": args.overlap, "threshold_dbfs": args.threshold,
                "minimum_gap": args.minimum_gap, "fades": False}
    # Validate configuration before creating any artifacts.
    bounded([[0, 1]], args.max_window, args.overlap)
    if not math.isfinite(args.threshold) or not math.isfinite(args.minimum_gap) or args.minimum_gap <= 0:
        raise ValueError("Require finite threshold and positive minimum-gap")
    folder = Path(args.out_dir).resolve() / key(settings)
    manifest = folder / "manifest.json"
    if manifest.exists():
        checked_manifest(manifest)
        print(manifest)
        return
    delivery = delivery_module()
    info = delivery.media_info(source)
    edit = delivery.canonical_plan(plan, info)
    fps = Fraction(info["fps"])
    to_sample = lambda frame: round(Fraction(frame) * SR / fps)
    folder.mkdir(parents=True, exist_ok=True)
    decoded, audio = folder / "source_decoded_16k.wav", folder / "timeline_16k.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(source), "-map", "0:a:0",
                    "-vn", "-ac", "1", "-ar", str(SR), "-c:a", "pcm_s16le", str(decoded)], check=True)
    decoded_fp = fingerprint(decoded)
    mapping = []
    with wave.open(str(decoded), "rb") as original, wave.open(str(audio), "wb") as output:
        if (original.getnchannels(), original.getsampwidth(), original.getframerate()) != (1, 2, SR):
            raise ValueError("Decoded source is not mono PCM16/16kHz")
        output.setparams((1, 2, SR, 0, "NONE", "not compressed"))
        for i, (a, b, c, d) in enumerate(edit["ranges"], 1):
            sa, oc, od = to_sample(a), to_sample(c), to_sample(d)
            # Round cumulative output frame positions: fractional rates cannot
            # accumulate a sample of drift per clip. Map actual sampled endpoints.
            sb = sa + od - oc
            original.setpos(sa)
            pcm = original.readframes(sb - sa)
            if len(pcm) != (sb - sa) * 2:
                raise ValueError("Source audio ends before the canonical retained range")
            output.writeframes(pcm)
            mapping.append({"clip": i, "source_start_frame": a, "source_end_frame": b,
                            "output_start_frame": c, "output_end_frame": d,
                            "source_start": float(a / fps), "source_end": float(b / fps),
                            "output_start": float(c / fps), "output_end": float(d / fps),
                            "source_start_sample": sa, "source_end_sample": sb,
                            "output_start_sample": oc, "output_end_sample": od})
    duration = float(edit["output_frames"] / fps)
    map_path = folder / "source_map.json"
    map_data = {"source_sha256": source_fp["sha256"], "plan_sha256": plan_fp["sha256"],
                "source_fps": str(fps), "source_frames": info["frames"],
                "output_frames": edit["output_frames"], "sr": SR,
                "output_samples": to_sample(edit["output_frames"]), "ranges": mapping,
                "sample_quantization": "nearest source start; cumulative output frame rounding, at most one sample endpoint offset"}
    write_json(map_path, map_data)
    samples = load_audio(audio)
    gaps = quiet_gaps(samples, threshold=args.threshold, minimum=args.minimum_gap)
    kept = [[0, duration]]
    windows = bounded(kept, args.max_window, args.overlap)
    seams = [{"id": f"seam_{i:05d}", "output_frame": left["output_end_frame"],
              "left_source_end_frame": left["source_end_frame"],
              "right_source_start_frame": right["source_start_frame"],
              "window": [max(0, left["output_end"] - 1), min(duration, left["output_end"] + 1)]}
             for i, (left, right) in enumerate(zip(mapping, mapping[1:]), 1)]
    # A full CFR probe/decode can take minutes. Never publish evidence bound to
    # the old fingerprints if an editor changed either input while it ran.
    if fingerprint(source) != source_fp:
        raise ValueError("Original source changed during timeline generation; run timeline again")
    if fingerprint(plan_path) != plan_fp:
        raise ValueError("Timeline plan changed during generation; run timeline again")
    if fingerprint(decoded) != decoded_fp:
        raise ValueError("Decoded source audio changed during generation; run timeline again")
    write_json(manifest, {"settings": settings, "time_basis": "output",
                         "source_sha256": source_fp["sha256"], "plan_sha256": plan_fp["sha256"],
                         "source_audio": decoded_fp,
                         "source_map": fingerprint(map_path), "source_map_path": str(map_path),
                         "source_map_sha256": fingerprint(map_path)["sha256"],
                         "audio": str(audio), "audio_sha256": fingerprint(audio)["sha256"],
                         "duration": duration, "output_frames": edit["output_frames"],
                         "source_fps": str(fps), "kept_ranges": kept, "quiet_candidates": gaps,
                         "acoustic_islands": subtract(kept, gaps), "windows": windows,
                         "window_ids": [f"window_{i:05d}" for i in range(len(windows))],
                         "seams": seams, "uncovered_kept_ranges": subtract(kept, windows),
                         "warning": "Output-time evidence only; map findings to source before editing. No fades applied."})
    print(manifest)


def prepare(args):
    plan = read_json(args.plan) if args.plan else None
    validate_plan_source(plan, args.source)
    settings = {"version": VERSION, "source": fingerprint(args.source), "sr": SR,
                "threshold_dbfs": args.threshold, "minimum_gap": args.minimum_gap,
                "context": args.context, "max_window": args.max_window, "overlap": args.overlap,
                "plan": plan,
                "target_windows": read_json(args.windows) if args.windows else None}
    folder = Path(args.out_dir).resolve() / key(settings)
    manifest = folder / "manifest.json"
    if manifest.exists():
        checked_manifest(manifest)
        print(manifest)
        return
    folder.mkdir(parents=True, exist_ok=True)
    audio = folder / "source_16k.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(Path(args.source).resolve()),
                    "-vn", "-ac", "1", "-ar", str(SR), "-c:a", "pcm_s16le", str(audio)], check=True)
    samples = load_audio(audio)
    duration = len(samples) / SR
    kept = merge(ranges(settings["plan"], duration)) if settings["plan"] is not None else [[0, duration]]
    gaps = quiet_gaps(samples, threshold=args.threshold, minimum=args.minimum_gap)
    windows, islands = make_windows(kept, gaps, duration, args.context, args.max_window, args.overlap)
    if settings["target_windows"] is not None:
        windows = bounded(ranges(settings["target_windows"], duration), args.max_window, args.overlap)
    data = {"settings": settings, "audio": str(audio), "audio_sha256": fingerprint(audio)["sha256"],
            "time_basis": "source", "source_sha256": settings["source"]["sha256"],
            "duration": duration, "kept_ranges": kept, "quiet_candidates": gaps,
            "acoustic_islands": islands, "windows": windows,
            "window_ids": [f"window_{i:05d}" for i in range(len(windows))],
            "uncovered_kept_ranges": subtract(kept, windows),
            "warning": "RMS gaps and ASR output are evidence only. No removal is authorized by this helper."}
    write_json(manifest, data)
    print(manifest)


def checked_manifest(path):
    data = read_json(path)
    if fingerprint(data["audio"])["sha256"] != data["audio_sha256"]:
        raise ValueError("Prepared audio changed; run prepare again in a fresh output directory")
    if fingerprint(data["settings"]["source"]["path"]) != data["settings"]["source"]:
        raise ValueError("Original source changed; run prepare again")
    if data.get("time_basis") == "output":
        decoded_fp = data.get("source_audio")
        if not isinstance(decoded_fp, dict) or not decoded_fp.get("path") or not decoded_fp.get("sha256"):
            raise ValueError("Timeline lacks decoded source audio binding; regenerate with current timeline command")
        if fingerprint(decoded_fp["path"]) != decoded_fp:
            raise ValueError("Decoded source audio changed; regenerate timeline")
        plan_fp, map_fp = data["settings"]["plan"], data["source_map"]
        if fingerprint(plan_fp["path"]) != plan_fp or plan_fp["sha256"] != data["plan_sha256"]:
            raise ValueError("Timeline plan changed; run timeline again")
        if fingerprint(map_fp["path"]) != map_fp or map_fp["sha256"] != data["source_map_sha256"]:
            raise ValueError("Timeline source map changed; run timeline again")
        source_map = read_json(map_fp["path"])
        if (source_map["plan_sha256"] != data["plan_sha256"] or
                source_map["source_sha256"] != data["settings"]["source"]["sha256"] or
                data["source_sha256"] != source_map["source_sha256"] or
                source_map["output_frames"] != data["output_frames"] or
                source_map["source_fps"] != data["source_fps"] or
                data["source_map_path"] != map_fp["path"]):
            raise ValueError("Timeline plan/source/map binding mismatch")
        with wave.open(data["audio"], "rb") as wav:
            if (wav.getnframes(), wav.getframerate()) != (source_map["output_samples"], SR):
                raise ValueError("Timeline PCM length differs from source map")
    return data


def transcribe(args):
    data = checked_manifest(args.manifest)
    backend = args.backend
    if backend == "auto":
        backend = "mlx" if platform.system() == "Darwin" and platform.machine() == "arm64" else "faster"
    package = "mlx-whisper" if backend == "mlx" else "faster-whisper"
    try:
        version = importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError as exc:
        raise RuntimeError(f"Install local backend: pip install {package}") from exc
    model_id = args.model or ("mlx-community/whisper-large-v3-mlx" if backend == "mlx" else "large-v3")
    settings = {"version": VERSION, "manifest": fingerprint(args.manifest), "backend": backend,
                "package_version": version, "model": model_id, "language": args.language,
                "device": args.device, "compute_type": args.compute_type, "word_timestamps": True,
                "vad_filter": False, "condition_on_previous_text": False, "initial_prompt": None}
    folder = Path(args.manifest).resolve().parent / ("asr_" + key(settings))
    folder.mkdir(exist_ok=True)
    audio = load_audio(data["audio"])
    model = None
    rows = []
    for i, (a, b) in enumerate(data["windows"]):
        dest = folder / f"window_{i:05d}.json"
        if dest.exists():
            rows.append(read_json(dest))
            continue
        crop = audio[round(a * SR):round(b * SR)]
        if backend == "faster":
            if model is None:
                from faster_whisper import WhisperModel
                model = WhisperModel(model_id, device=args.device, compute_type=args.compute_type)
            segments, _ = model.transcribe(crop, language=args.language, word_timestamps=True,
                                           vad_filter=False, condition_on_previous_text=False,
                                           initial_prompt=None, beam_size=5, temperature=0)
            segments = [{"start": s.start, "end": s.end, "text": s.text,
                         "words": [{"start": w.start, "end": w.end, "word": w.word,
                                    "probability": w.probability} for w in s.words or []]} for s in segments]
        else:
            import mlx_whisper
            segments = mlx_whisper.transcribe(crop, path_or_hf_repo=model_id, language=args.language,
                                              word_timestamps=True, condition_on_previous_text=False,
                                              initial_prompt=None, temperature=0)["segments"]
        normalized = []
        for segment in segments:
            normalized.append({"start": a + segment["start"], "end": a + segment["end"],
                               "text": segment["text"], "words": [
                                   {"start": a + w["start"], "end": a + w["end"],
                                    "text": w["word"], "probability": w.get("probability")}
                                   for w in segment.get("words", [])]})
        row = {"window_id": data.get("window_ids", [f"window_{j:05d}" for j in range(len(data["windows"]))])[i],
               "time_basis": data.get("time_basis", "source"), "start": a, "end": b, "segments": normalized,
               "empty_asr_requires_review": not any(s["text"].strip() for s in normalized)}
        write_json(dest, row)
        rows.append(row)
        print(f"{i+1}/{len(data['windows'])}: {a:.3f}-{b:.3f}", flush=True)
    result = {"settings": settings, "time_basis": data.get("time_basis", "source"),
              "source_sha256": data["settings"]["source"]["sha256"],
              "plan_sha256": data.get("plan_sha256"), "source_map": data.get("source_map"),
              "windows": rows, "completed_windows": len(rows),
              "expected_windows": len(data["windows"]), "uncovered_kept_ranges": data["uncovered_kept_ranges"],
              "empty_asr_windows": [[r["start"], r["end"]] for r in rows if r["empty_asr_requires_review"]],
              "warning": "No silence/deletion inference from empty ASR; inspect original audio and neighboring words."}
    write_json(folder / "transcript.json", result)
    print(folder / "transcript.json")


def classifier_scores(labels, values):
    """Keep competing Speech evidence and global top classes, not just cough hits."""
    all_scores = {label: values[i] for i, label in labels.items()}
    relevant = {label: value for label, value in all_scores.items()
                if label.lower() == "speech" or any(term in label.lower() for term in
                  ("cough", "throat", "sneeze", "sniff", "breath", "gasp", "snort"))}
    return {"events": relevant, "speech_score": all_scores.get("Speech"),
            "top10": [{"label": label, "score": value} for label, value in
                      sorted(all_scores.items(), key=lambda row: row[1], reverse=True)[:10]]}


def events(args):
    data = checked_manifest(args.manifest)
    transcript = read_json(args.transcript) if args.transcript else None
    words = []
    if transcript:
        if transcript["settings"]["manifest"] != fingerprint(args.manifest):
            raise ValueError("Transcript belongs to a different audit manifest")
        words = [[w["start"], w["end"]] for row in transcript["windows"]
                 for s in row["segments"] for w in s["words"] if w["end"] > w["start"]]
    settings = {"version": VERSION, "manifest": fingerprint(args.manifest),
                "transcript": fingerprint(args.transcript) if args.transcript else None,
                "classify": args.classify, "model": args.model, "window": args.window,
                "overlap": args.overlap, "device": args.device}
    scores = []
    if args.classify:
        try:
            os.environ.setdefault("USE_TF", "0")
            os.environ.setdefault("USE_FLAX", "0")
            import torch
            from transformers import ASTFeatureExtractor, ASTForAudioClassification
        except ImportError as exc:
            raise RuntimeError("Optional sound classifier requires: pip install torch transformers") from exc
        settings["transformers_version"] = importlib.metadata.version("transformers")
        settings["torch_version"] = importlib.metadata.version("torch")
    dest = Path(args.manifest).resolve().parent / ("events_" + key(settings) + ".json")
    if dest.exists():
        print(dest)
        return
    if args.classify:
        extractor = ASTFeatureExtractor.from_pretrained(args.model)
        model = ASTForAudioClassification.from_pretrained(args.model).to(args.device).eval()
        audio = load_audio(data["audio"])
        scan_windows = event_windows(data, words, transcript is not None, args.window, args.overlap)
        for a, b, kind in scan_windows:
            inputs = extractor(audio[round(a * SR):round(b * SR)], sampling_rate=SR, return_tensors="pt")
            with torch.inference_mode():
                values = model(**{k: v.to(args.device) for k, v in inputs.items()}).logits.sigmoid()[0].cpu().tolist()
            labels = {int(k): v for k, v in model.config.id2label.items()}
            scores.append({"start": a, "end": b, "kind": kind, **classifier_scores(labels, values)})
    result = {"settings": settings, "time_basis": data.get("time_basis", "source"),
              "classified": args.classify, "scores": scores,
              "independent_short_acoustic_islands": [[a, b] for a, b in data["acoustic_islands"] if b - a <= args.window],
              "speech_uncovered_acoustic_candidates": subtract(data["acoustic_islands"], words),
              "warning": "Scores are uncalibrated candidates, not proof. Uncovered audio may be speech. Review source; never auto-cut."}
    write_json(dest, result)
    print(dest)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subs = parser.add_subparsers(dest="command", required=True)
    p = subs.add_parser("prepare")
    p.add_argument("--source", required=True)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--plan")
    p.add_argument("--windows")
    p.add_argument("--threshold", type=float, default=-42)
    p.add_argument("--minimum-gap", type=float, default=.35)
    p.add_argument("--context", type=float, default=.08)
    p.add_argument("--max-window", type=float, default=18)
    p.add_argument("--overlap", type=float, default=3)
    p.set_defaults(func=prepare)
    p = subs.add_parser("timeline")
    p.add_argument("--plan", required=True)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--max-window", type=float, default=4)
    p.add_argument("--overlap", type=float, default=1.25)
    p.add_argument("--threshold", type=float, default=-42)
    p.add_argument("--minimum-gap", type=float, default=.35)
    p.set_defaults(func=timeline)
    p = subs.add_parser("transcribe")
    p.add_argument("--manifest", required=True)
    p.add_argument("--backend", choices=["auto", "faster", "mlx"], default="auto")
    p.add_argument("--model")
    p.add_argument("--language")
    p.add_argument("--device", default="auto")
    p.add_argument("--compute-type", default="default")
    p.set_defaults(func=transcribe)
    p = subs.add_parser("events")
    p.add_argument("--manifest", required=True)
    p.add_argument("--transcript")
    p.add_argument("--classify", action="store_true")
    p.add_argument("--model", default="MIT/ast-finetuned-audioset-10-10-0.4593")
    p.add_argument("--window", type=float, default=3)
    p.add_argument("--overlap", type=float, default=2)
    p.add_argument("--device", default="cpu")
    p.set_defaults(func=events)
    args = parser.parse_args()
    try:
        if args.command == "prepare" and (not all(math.isfinite(v) for v in (args.context, args.minimum_gap, args.threshold)) or args.context < 0 or args.minimum_gap <= 0):
            raise ValueError("Require context >= 0, minimum-gap > 0 and finite threshold")
        args.func(args)
    except (ValueError, RuntimeError, FileNotFoundError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"Audit failed: {exc}\n")


if __name__ == "__main__":
    main()
