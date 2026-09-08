import importlib.util
from pathlib import Path
import tempfile
import unittest
import contextlib
import io
import shutil
import wave
from types import SimpleNamespace
from unittest import mock
from fractions import Fraction
import subprocess

spec = importlib.util.spec_from_file_location("dialogue_audit", Path(__file__).parents[1] / "helpers/dialogue_audit.py")
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


class AuditTests(unittest.TestCase):
    def test_cache_changes_for_source_bytes_and_config(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "a.wav"
            path.write_bytes(b"one")
            old = audit.fingerprint(path)
            path.write_bytes(b"two")
            self.assertNotEqual(old, audit.fingerprint(path))
            self.assertNotEqual(audit.key({"source": old, "model": "a"}), audit.key({"source": old, "model": "b"}))

    def test_short_island_preserved_and_context_includes_neighbors(self):
        windows, islands = audit.make_windows([[0, 4]], [[0, 1], [1.04, 1.5], [3, 4]], 4, context=.5)
        self.assertIn([1, 1.04], islands)
        self.assertIn([.5, 1.27], windows)
        self.assertEqual(audit.subtract(islands, windows), [])
        self.assertLessEqual(windows[0][1], windows[1][0])

    def test_adjacent_retry_windows_never_include_other_attempt(self):
        windows, _ = audit.make_windows([[0, 3]], [[.1, .45], [.55, .9]], 3, context=.5)
        self.assertEqual(windows[0][1], .275)
        self.assertEqual(windows[1][0], .275)
        self.assertLess(windows[1][1], .9)

    def test_plan_source_validation(self):
        audit.validate_plan_source({"source": "a.mp4", "ranges": []}, "a.mp4")
        for plan in ({"source": "b.mp4"}, {"sources": []}, {"ranges": [{"source": "b.mp4"}]}):
            with self.assertRaises(ValueError):
                audit.validate_plan_source(plan, "a.mp4")

    def test_events_include_narrow_candidates_and_targets(self):
        data = {"kept_ranges": [[0, 3]], "acoustic_islands": [[.3, 1]],
                "windows": [[.2, .6]], "settings": {"target_windows": [[.2, .6]]}}
        windows = audit.event_windows(data, [[.5, 1]], True, 3, 2)
        self.assertIn((.3, .5, "isolated_speech_uncovered"), windows)
        self.assertIn((.2, .6, "explicit_target"), windows)
        self.assertIn((0, 3, "broad"), windows)

    def test_events_classify_short_islands_even_when_asr_claims_speech(self):
        data = {"kept_ranges": [[0, 5]], "acoustic_islands": [[.4, .47], [1, 1.7], [2, 5]],
                "windows": [[0, 5]], "settings": {}}
        windows = audit.event_windows(data, [[0, 5]], True, 3, 2)
        self.assertIn((.4, .47, "isolated_acoustic_island"), windows)
        self.assertIn((1, 1.7, "isolated_acoustic_island"), windows)
        self.assertFalse(any(kind == "isolated_speech_uncovered" for _, _, kind in windows))

    def test_classifier_preserves_speech_and_top10_competing_classes(self):
        labels = {i: f"class_{i}" for i in range(12)}
        labels.update({12: "Speech", 13: "Throat clearing", 14: "Cough"})
        values = [i / 100 for i in range(12)] + [.001, .0002, .0003]
        scores = audit.classifier_scores(labels, values)
        self.assertEqual(len(scores["top10"]), 10)
        self.assertEqual(scores["top10"][0], {"label": "class_11", "score": .11})
        self.assertEqual(scores["speech_score"], .001)
        self.assertEqual(scores["events"]["Speech"], .001)
        self.assertEqual(scores["events"]["Cough"], .0003)

    def test_bounded_windows_cover_end_and_overlap(self):
        windows = audit.bounded([[0, 40]], 18, 3)
        self.assertEqual(windows, [[0, 18], [15, 33], [30, 40]])
        self.assertEqual(audit.subtract([[0, 40]], windows), [])
        with self.assertRaises(ValueError):
            audit.bounded([[0, 40]], 3, 3)

    def test_uncovered_is_not_silence(self):
        self.assertEqual(audit.subtract([[1, 4]], [[2, 3]]), [[1, 2], [3, 4]])
        self.assertEqual(audit.subtract([[1, 4]], []), [[1, 4]])

    def test_dc_offset_does_not_hide_gap(self):
        import numpy as np
        sr = 16000
        audio = np.full(sr, .2)
        audio[3200:6400] += .1 * np.sin(np.arange(3200) * .2)
        self.assertEqual(audit.quiet_gaps(audio, minimum=.15), [[0, .2], [.4, 1]])

    def test_ranges_reject_outside_or_nonfinite_source(self):
        for invalid in ([[0, 11]], [[-1, 1]], [[0, float("nan")]]):
            with self.assertRaises(ValueError):
                audit.ranges(invalid, 10)

    def timeline_args(self, plan, folder):
        return SimpleNamespace(plan=plan, out_dir=folder, max_window=4, overlap=1.25,
                               threshold=-42, minimum_gap=.35)

    def run_timeline(self, args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            audit.timeline(args)
        return Path(output.getvalue().strip())

    def test_timeline_rejects_stale_source_hash_alias_before_probe(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "source.wav").write_bytes(b"source bytes")
            plan = root / "plan.json"
            audit.write_json(plan, {"source": "source.wav", "source_hash": "stale",
                                    "ranges": [{"start": 0, "end": 1}]})
            with mock.patch.object(audit, "delivery_module", side_effect=AssertionError("must not probe")):
                with self.assertRaisesRegex(ValueError, "source_hash"):
                    self.run_timeline(self.timeline_args(plan, root / "audit"))

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg required")
    def test_timeline_does_not_publish_manifest_after_input_changes_during_generation(self):
        import numpy as np
        for mutate in ("source", "plan"):
            with self.subTest(mutate=mutate), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                source = root / "source.wav"
                with wave.open(str(source), "wb") as wav:
                    wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                    wav.writeframes(np.zeros(32000, dtype="<i2").tobytes())
                plan = root / "plan.json"
                audit.write_json(plan, {"source": "source.wav", "source_hash": audit.fingerprint(source)["sha256"],
                                        "ranges": [{"start": 0, "end": 1}]})
                delivery = audit.delivery_module()
                original_quiet = audit.quiet_gaps

                def mutate_after_decode(*args, **kwargs):
                    changed = source if mutate == "source" else plan
                    changed.write_bytes(changed.read_bytes() + b" ")
                    return original_quiet(*args, **kwargs)

                with mock.patch.object(delivery, "media_info", return_value={"fps": "25", "frames": 50}), \
                        mock.patch.object(audit, "delivery_module", return_value=delivery), \
                        mock.patch.object(audit, "quiet_gaps", side_effect=mutate_after_decode):
                    with self.assertRaisesRegex(ValueError, "changed during"):
                        self.run_timeline(self.timeline_args(plan, root / "audit"))
                self.assertEqual(list((root / "audit").rglob("manifest.json")), [])

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe required")
    def test_timeline_exact_pcm_frame_map_full_coverage_and_cache_binding(self):
        import numpy as np
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            audio, source = root / "input.wav", root / "input.mkv"
            samples = ((np.arange(16000 * 8) % 20001) - 10000).astype("<i2")
            with wave.open(str(audio), "wb") as wav:
                wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                wav.writeframes(samples.tobytes())
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=s=16x16:r=25:d=8",
                            "-i", str(audio), "-map", "0:v:0", "-map", "1:a:0", "-c:v", "ffv1",
                            "-c:a", "pcm_s16le", str(source)], check=True)
            plan = root / "plan.json"
            audit.write_json(plan, {"source": "input.mkv", "source_fps": 25,
                                    "ranges": [{"start": .2, "end": 3.4}, {"start": 4, "end": 8}]})
            args = self.timeline_args(plan, root / "audit")
            manifest = self.run_timeline(args)
            data = audit.checked_manifest(manifest)
            source_map = audit.read_json(data["source_map"]["path"])
            self.assertEqual(data["time_basis"], "output")
            self.assertEqual(data["windows"], [[0, 4], [2.75, 6.75], [5.5, 7.2]])
            self.assertEqual(data["window_ids"], ["window_00000", "window_00001", "window_00002"])
            self.assertEqual(data["uncovered_kept_ranges"], [])
            self.assertEqual(data["seams"], [{"id": "seam_00001", "output_frame": 80,
                                            "left_source_end_frame": 85, "right_source_start_frame": 100,
                                            "window": [2.2, 4.2]}])
            self.assertEqual(source_map["output_frames"], 180)
            self.assertEqual(source_map["ranges"][1]["output_start_sample"], 51200)
            with wave.open(data["audio"], "rb") as wav:
                actual = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2")
            np.testing.assert_array_equal(actual, np.r_[samples[3200:54400], samples[64000:128000]])
            with mock.patch.object(audit.subprocess, "run", side_effect=AssertionError("cache decoded again")):
                self.assertEqual(self.run_timeline(args), manifest)
            map_path = Path(data["source_map"]["path"])
            original_map = map_path.read_bytes()
            map_path.write_bytes(original_map + b" ")
            with self.assertRaisesRegex(ValueError, "source map changed"):
                audit.checked_manifest(manifest)
            map_path.write_bytes(original_map)
            original_plan = plan.read_bytes()
            plan.write_bytes(original_plan + b" ")
            with self.assertRaisesRegex(ValueError, "plan changed"):
                audit.checked_manifest(manifest)
            # Changed plan bytes select a fresh evidence cache, even for equal JSON.
            new_manifest = self.run_timeline(args)
            self.assertNotEqual(new_manifest, manifest)
            new_data = audit.checked_manifest(new_manifest)
            self.assertNotEqual(new_data["plan_sha256"], data["plan_sha256"])

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg required")
    def test_timeline_fractional_rate_uses_cumulative_sample_rounding(self):
        import numpy as np
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.wav"
            samples = (np.arange(64000) % 10000).astype("<i2")
            with wave.open(str(source), "wb") as wav:
                wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                wav.writeframes(samples.tobytes())
            fps = Fraction(30000, 1001)
            plan = root / "plan.json"
            audit.write_json(plan, {"source": "source.wav", "ranges": [
                {"start": float(Fraction(a) / fps), "end": float(Fraction(a + 1) / fps)}
                for a in (0, 2, 4, 6, 8, 10)]})
            delivery = audit.delivery_module()
            with mock.patch.object(delivery, "media_info", return_value={"fps": str(fps), "frames": 100}), \
                    mock.patch.object(audit, "delivery_module", return_value=delivery):
                manifest = self.run_timeline(self.timeline_args(plan, root / "audit"))
            data = audit.checked_manifest(manifest)
            mapping = audit.read_json(data["source_map"]["path"])
            self.assertEqual(mapping["output_samples"], round(Fraction(6 * 16000) / fps))
            with wave.open(data["audio"], "rb") as wav:
                actual = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2")
            expected = np.concatenate([samples[r["source_start_sample"]:r["source_end_sample"]]
                                       for r in mapping["ranges"]])
            np.testing.assert_array_equal(actual, expected)
            self.assertEqual(len(actual), mapping["output_samples"])

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is required for decode smoke test")
    def test_prepare_and_events_preserve_source_and_track_uncovered_sound(self):
        import numpy as np
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.wav"
            samples = np.zeros(32000, dtype="<i2")
            samples[8000:16000] = (6000 * np.sin(np.arange(8000) * .2)).astype("<i2")
            with wave.open(str(source), "wb") as wav:
                wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                wav.writeframes(samples.tobytes())
            before = audit.fingerprint(source)
            args = SimpleNamespace(source=source, out_dir=Path(folder) / "audit", plan=None,
                                   windows=None, threshold=-42, minimum_gap=.35, context=.35,
                                   max_window=18, overlap=3)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                audit.prepare(args)
            manifest = Path(output.getvalue().strip())
            data = audit.checked_manifest(manifest)
            self.assertEqual(audit.fingerprint(source), before)
            self.assertEqual(data["acoustic_islands"], [[.5, 1]])
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                audit.events(SimpleNamespace(manifest=manifest, transcript=None, classify=False,
                                             model="unused", window=3, overlap=2, device="cpu"))
            report = audit.read_json(output.getvalue().strip())
            self.assertFalse(report["classified"])
            self.assertEqual(report["speech_uncovered_acoustic_candidates"], [[.5, 1]])
            Path(data["audio"]).write_bytes(b"changed")
            with self.assertRaises(ValueError), contextlib.redirect_stdout(io.StringIO()):
                audit.prepare(args)


if __name__ == "__main__":
    unittest.main()
