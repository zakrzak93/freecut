import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import wave

import numpy as np

spec = importlib.util.spec_from_file_location('dialogue_risks', Path(__file__).parents[1] / 'helpers/dialogue_risks.py')
risks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(risks)


def window(words, ident='window_00000', start=0, end=2.4):
    return dict(window_id=ident, start=start, end=end,
                segments=[dict(words=[dict(text=t, start=a, end=b) for t, a, b in words])])


class RiskTests(unittest.TestCase):
    def fixture(self, root):
        audit = risks.audit_module()
        source, pcm, output = root / 'original.wav', root / 'source_decoded_16k.wav', root / 'timeline.wav'
        samples = np.zeros(48000, dtype='<i2')
        for a, b in ((.60, .66), (1.22, 1.45), (1.58, 1.81), (2.1, 2.6)):
            n = round((b-a)*16000)
            samples[round(a*16000):round(a*16000)+n] = (5000*np.sin(np.arange(n)*.2)).astype('<i2')
        def save(path, values):
            with wave.open(str(path), 'wb') as w:
                w.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                w.writeframes(values.tobytes())
        save(source, samples); save(pcm, samples)
        save(output, np.r_[samples[:12800], samples[19200:44800]])
        plan = root / 'plan.json'
        audit.write_json(plan, {'source': str(source), 'ranges': [{'start': 0, 'end': .8}, {'start': 1.2, 'end': 2.8}]})
        rows = []
        for i, (a, b, c, d) in enumerate(((0, .8, 0, .8), (1.2, 2.8, .8, 2.4)), 1):
            rows.append(dict(clip=i, source_start=a, source_end=b, output_start=c, output_end=d,
                             source_start_frame=round(a*25), source_end_frame=round(b*25),
                             output_start_frame=round(c*25), output_end_frame=round(d*25),
                             source_start_sample=round(a*16000), source_end_sample=round(b*16000),
                             output_start_sample=round(c*16000), output_end_sample=round(d*16000)))
        mapping = dict(source_sha256=audit.fingerprint(source)['sha256'], plan_sha256=audit.fingerprint(plan)['sha256'],
                       source_fps='25', source_frames=75, output_frames=60, output_samples=38400, sr=16000, ranges=rows)
        mp = root / 'source_map.json'; audit.write_json(mp, mapping)
        data = dict(settings=dict(source=audit.fingerprint(source), plan=audit.fingerprint(plan)),
                    time_basis='output', source_sha256=mapping['source_sha256'], plan_sha256=mapping['plan_sha256'],
                    source_map=audit.fingerprint(mp), source_map_path=str(mp), source_map_sha256=audit.fingerprint(mp)['sha256'],
                    source_audio=audit.fingerprint(pcm),
                    audio=str(output), audio_sha256=audit.fingerprint(output)['sha256'], source_fps='25', output_frames=60,
                    duration=2.4, windows=[[0, 2.4]], window_ids=['window_00000'], quiet_candidates=[[0, .6]],
                    kept_ranges=[[0, 2.4]], acoustic_islands=[[.6, .66]], uncovered_kept_ranges=[])
        manifest = root / 'manifest.json'; audit.write_json(manifest, data)
        transcript = root / 'transcript.json'
        audit.write_json(transcript, dict(settings=dict(manifest=audit.fingerprint(manifest)), time_basis='output',
                         completed_windows=1, expected_windows=1,
                         windows=[window([('ścis', .85, 1.02), ('ścis', 1.18, 1.40)])]))
        return manifest, transcript

    def test_required_edges_context_micro_quiet_short_clip_and_repeat(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); manifest, transcript = self.fixture(root)
            report = risks.build_inventory(manifest, transcript)
            checks = report['checks']
            edges = [c for c in checks if c['kind'] == 'source_edge']
            self.assertEqual([c['id'] for c in edges], ['edge_00001_start', 'edge_00001_end', 'edge_00002_start', 'edge_00002_end'])
            self.assertEqual(edges[0]['evidence_windows'][0]['role'], 'kept')
            self.assertEqual(edges[0]['evidence_windows'][1]['start'], 0)
            self.assertEqual(edges[1]['evidence_windows'][1]['end'], 1.15)
            kinds = {c['kind'] for c in checks}
            self.assertTrue({'short_kept_clip', 'long_output_quiet_gap', 'micro_island', 'adjacent_repeat'} <= kinds)
            self.assertGreaterEqual(sum(c['kind'] == 'micro_island' for c in checks), 3)
            self.assertTrue(report['asr_complete'])
            self.assertEqual(risks.build_inventory(manifest, transcript), report)
            for c in checks:
                for a, b in c['source_ranges']:
                    self.assertTrue(0 <= a < b <= 3)

    def test_overlap_observations_are_not_repeats_but_real_repeated_phrase_is(self):
        a = [('dziecko', 0, .3), ('widziało', .3, .6), ('dziecko', .75, 1), ('widziało', 1, 1.25), ('figurkę', 1.25, 1.6)]
        b = [('dziecko', .76, 1.01), ('widziało', 1.01, 1.26), ('figurkę', 1.26, 1.61)]
        tokens = risks.tokens({'windows': [window(a), window(b, 'window_00001')]})
        self.assertEqual(len(tokens), 5)
        repeats = risks.repeats(tokens)
        self.assertEqual(len(repeats), 1)
        self.assertEqual(repeats[0]['ngram'], 2)
        self.assertEqual(risks.repeats(risks.tokens({'windows': [window(b), window(b)]})), [])

    def test_three_word_repeat_and_same_window_overlap_survive_dedup(self):
        words = [(t, i*.2, i*.2+.15) for i, t in enumerate(['nie', 'ma', 'nic']*2)]
        self.assertTrue(any(r['ngram'] == 3 for r in risks.repeats(risks.tokens({'windows': [window(words)]}))))
        self.assertEqual(len(risks.tokens({'windows': [window([('a', 0, .2), ('a', .15, .3)])]})), 2)

    def test_source_map_cross_clip_interval(self):
        rows = [dict(source_start=10, source_end=11, output_start=0, output_end=1),
                dict(source_start=20, source_end=21, output_start=1, output_end=2)]
        self.assertEqual(risks.mapped([.8, 1.2], rows), [[10.8, 11], [20, 20.2]])

    def test_850ms_failed_attempt_flagged_even_when_asr_smooths_it(self):
        samples = np.zeros(32000, dtype=np.float32)
        samples[6400:20000] = .2*np.sin(np.arange(13600)*.2)
        mapping = dict(source_frames=50, source_fps='25', ranges=[dict(
            source_start=0, source_end=2, output_start=0, output_end=2,
            source_start_frame=0, source_end_frame=50)])
        data = dict(duration=2, quiet_candidates=[])
        # ASR has one fluent covering token, not a reported repetition.
        transcript = dict(windows=[window([('smooth', .4, 1.25)])])
        checks = risks.discover(data, mapping, samples, samples, transcript)
        candidates = [c for c in checks if c['kind'] == 'micro_island']
        self.assertEqual(len(candidates), 1)
        self.assertAlmostEqual(candidates[0]['source_ranges'][0][0], .4)
        self.assertAlmostEqual(candidates[0]['source_ranges'][0][1], 1.25)
        self.assertFalse(any(c['kind'] == 'adjacent_repeat' for c in checks))

    def test_immutable_report_missing_asr_and_stale_inputs(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); manifest, transcript = self.fixture(root)
            out = root / 'risks.json'
            report = risks.generate(manifest, None, out)
            self.assertFalse(report['asr_complete'])
            self.assertEqual(risks.generate(manifest, None, out), report)
            with self.assertRaisesRegex(ValueError, 'differs'):
                risks.generate(manifest, transcript, out)
            self.assertFalse(json.loads(out.read_text())['asr_complete'])
            pcm = root / 'source_decoded_16k.wav'
            with wave.open(str(pcm), 'rb') as w:
                values = np.frombuffer(w.readframes(w.getnframes()), dtype='<i2').copy()
            values[100] = 123
            with wave.open(str(pcm), 'wb') as w:
                w.setparams((1, 2, 16000, 0, 'NONE', 'not compressed')); w.writeframes(values.tobytes())
            with self.assertRaisesRegex(ValueError, 'Decoded source audio changed'):
                risks.build_inventory(manifest, transcript)

    def test_quiet_inventory_cannot_be_suppressed_by_filtered_manifest_gaps(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); manifest, _ = self.fixture(root)
            before = risks.build_inventory(manifest)
            data = json.loads(manifest.read_text()); data['quiet_candidates'] = []
            manifest.write_text(json.dumps(data))
            after = risks.build_inventory(manifest)
            self.assertEqual([c for c in before['checks'] if c['kind'] == 'long_output_quiet_gap'],
                             [c for c in after['checks'] if c['kind'] == 'long_output_quiet_gap'])
            self.assertTrue(any(c['kind'] == 'long_output_quiet_gap' for c in after['checks']))

    def test_discarded_source_pcm_mutation_and_missing_decode_binding_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); manifest, _ = self.fixture(root)
            pcm = root / 'source_decoded_16k.wav'
            original = pcm.read_bytes()
            changed = bytearray(original)
            changed[44 + 16000*2] ^= 1  # source 1s is discarded, outside both retained spans
            pcm.write_bytes(changed)
            with self.assertRaisesRegex(ValueError, 'Decoded source audio changed'):
                risks.build_inventory(manifest)
            pcm.write_bytes(original)
            data = json.loads(manifest.read_text()); del data['source_audio']
            manifest.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, 'regenerate'):
                risks.build_inventory(manifest)

    def test_actual_asr_window_identity_required_not_just_completed_count(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); manifest, transcript = self.fixture(root)
            d = json.loads(transcript.read_text()); d['windows'] = []
            transcript.write_text(json.dumps(d))
            with self.assertRaisesRegex(ValueError, 'actual windows'):
                risks.build_inventory(manifest, transcript)


if __name__ == '__main__':
    unittest.main()
