import copy
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parents[1] / 'helpers' / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


q = load('qa_test_module', 'dialogue_qa.py')
d = load('delivery_qa_test_module', 'dialogue_delivery.py')


class QaFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.source = self.folder / 'source.mp4'
        self.source.write_bytes(b'immutable source fixture')
        self.plan_path = self.folder / 'plan.json'
        self.info = dict(fps='25', frames=100, width=640, height=360, channels=2, samplerate=48000)
        self.plan = dict(source='source.mp4', ranges=[dict(start=0, end=1), dict(start=2, end=4)])
        self.write(self.plan_path, self.plan)
        self.edit = d.canonical_plan(self.plan, self.info)
        self.audio = self.folder / 'timeline.wav'
        with wave.open(str(self.audio), 'wb') as wav:
            wav.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            wav.writeframes(bytes(48000 * 2))
        self.map_path = self.folder / 'source_map.json'
        self.mapping = dict(source_sha256=q.digest(self.source), plan_sha256=q.digest(self.plan_path),
            source_fps='25', source_frames=100, output_frames=75, sr=16000, output_samples=48000,
            ranges=[dict(clip=1, source_start_frame=0, source_end_frame=25, output_start_frame=0,
                output_end_frame=25, source_start_sample=0, source_end_sample=16000,
                source_start=0, source_end=1, output_start=0, output_end=1,
                output_start_sample=0, output_end_sample=16000),
                dict(clip=2, source_start_frame=50, source_end_frame=100, output_start_frame=25,
                output_end_frame=75, source_start_sample=32000, source_end_sample=64000,
                source_start=2, source_end=4, output_start=1, output_end=3,
                output_start_sample=16000, output_end_sample=48000)])
        self.write(self.map_path, self.mapping)
        self.manifest_path = self.folder / 'audit_manifest.json'
        self.manifest = dict(time_basis='output', source_sha256=q.digest(self.source),
            plan_sha256=q.digest(self.plan_path), settings=dict(source=self.ref(self.source), plan=self.ref(self.plan_path)),
            source_map=self.ref(self.map_path), audio=str(self.audio), audio_sha256=q.digest(self.audio),
            windows=[[0, 1.5], [1, 2.5], [2, 3]], window_ids=['w0', 'w1', 'w2'],
            seams=[dict(id='seam_00001', output_frame=25, left_source_end_frame=25, right_source_start_frame=50)])
        self.write(self.manifest_path, self.manifest)
        self.evidence = self.folder / 'review.txt'
        self.evidence.write_text('Independent reviewer evidence fixture', encoding='utf-8')
        self.report_path = self.folder / 'qa.json'
        self.report = dict(version=1, source_sha256=q.digest(self.source), plan_sha256=q.digest(self.plan_path),
            audit_manifest=self.ref(self.manifest_path), evidence=[dict(id='ev', **self.ref(self.evidence))],
            reviewed_windows=[dict(id=x, evidence_ids=['ev']) for x in self.manifest['window_ids']],
            reviewed_seams=[dict(**self.manifest['seams'][0], evidence_ids=['ev'])],
            candidates=[], findings=[])

    def write(self, path, value):
        Path(path).write_text(json.dumps(value), encoding='utf-8')

    def ref(self, path):
        return dict(path=str(path), sha256=q.digest(path))

    def refresh_manifest(self):
        self.write(self.manifest_path, self.manifest)
        self.report['audit_manifest'] = self.ref(self.manifest_path)

    def validate(self, mp4=None):
        self.write(self.report_path, self.report)
        return q.validate_qa(self.report_path, self.plan_path, self.source, self.info, self.edit, mp4)

    def candidate(self, disposition, interval=(25, 50)):
        self.report['candidates'] = [dict(id='f1', source_frames=list(interval), kind='unfinished_sentence')]
        self.report['findings'] = [dict(id='f1', disposition=disposition, reason='Source evidence reviewed', evidence_ids=['ev'])]


class QaTests(QaFixture, unittest.TestCase):
    def test_complete_review_ready_without_trusting_passed_boolean(self):
        self.report['passed'] = False
        self.assertEqual(self.validate()['editorial_status'], 'READY')
        self.report.update(passed=True, reviewed_windows=[], reviewed_seams=[])
        self.assertEqual(self.validate()['editorial_status'], 'REVIEW_REQUIRED')

    def test_missing_window_or_seam_is_draft(self):
        original = copy.deepcopy(self.report)
        self.report['reviewed_windows'].pop(1)
        self.assertEqual(self.validate()['editorial_status'], 'REVIEW_REQUIRED')
        self.report = original
        self.report['reviewed_seams'] = []
        self.assertEqual(self.validate()['editorial_status'], 'REVIEW_REQUIRED')

    def test_subframe_coverage_gap_is_not_rounded_away(self):
        self.manifest['windows'] = [[0, 1.499], [1.501, 2.5], [2, 3]]
        self.refresh_manifest()
        with self.assertRaisesRegex(ValueError, 'uncovered'):
            self.validate()

    def test_targeted_join_windows_cannot_replace_full_timeline(self):
        self.manifest['windows'] = [[.9, 1.1]]
        self.manifest['window_ids'] = ['w0']
        self.refresh_manifest()
        with self.assertRaisesRegex(ValueError, 'uncovered'):
            self.validate()

    def test_plan_audio_map_and_evidence_hashes_are_checked(self):
        for path in (self.plan_path, self.audio, self.map_path, self.evidence):
            with self.subTest(path=path.name):
                original = path.read_bytes()
                path.write_bytes(original + b'changed')
                with self.assertRaises(ValueError):
                    self.validate()
                path.write_bytes(original)

    def test_current_hash_does_not_hide_wrong_map_or_stale_seam(self):
        self.mapping['ranges'][1]['source_start_frame'] += 1
        self.write(self.map_path, self.mapping)
        self.manifest['source_map'] = self.ref(self.map_path)
        self.refresh_manifest()
        with self.assertRaisesRegex(ValueError, 'map source_start_frame'):
            self.validate()
        self.mapping['ranges'][1]['source_start_frame'] -= 1
        self.write(self.map_path, self.mapping)
        self.manifest['source_map'] = self.ref(self.map_path)
        self.refresh_manifest()
        self.report['reviewed_seams'][0]['right_source_start_frame'] = 51
        with self.assertRaisesRegex(ValueError, 'seam identity'):
            self.validate()

    def test_map_seconds_are_checked_even_if_frames_are_correct(self):
        self.mapping['ranges'][1]['source_start'] = 2.1
        self.write(self.map_path, self.mapping)
        self.manifest['source_map'] = self.ref(self.map_path)
        self.refresh_manifest()
        with self.assertRaisesRegex(ValueError, 'seconds mismatch'):
            self.validate()

    def test_report_and_evidence_changes_during_validation_are_rejected(self):
        original_validate = q.validate_timeline
        for target in ('report', 'evidence'):
            with self.subTest(target=target):
                original_evidence = self.evidence.read_bytes()
                def change_after_read(*args, **kwargs):
                    result = original_validate(*args, **kwargs)
                    if target == 'report':
                        changed = copy.deepcopy(self.report)
                        changed['reviewed_windows'] = []
                        self.write(self.report_path, changed)
                    else:
                        self.evidence.write_bytes(b'changed evidence')
                    return result
                with patch.object(q, 'validate_timeline', side_effect=change_after_read), self.assertRaisesRegex(ValueError, 'stale hash'):
                    self.validate()
                self.evidence.write_bytes(original_evidence)

    def test_candidate_decisions_are_complete_unique_and_geometrically_true(self):
        self.candidate('removed')
        self.assertEqual(self.validate()['editorial_status'], 'READY')
        self.report['findings'] = []
        self.assertEqual(self.validate()['editorial_status'], 'REVIEW_REQUIRED')
        self.candidate('unresolved')
        self.assertEqual(self.validate()['editorial_status'], 'REVIEW_REQUIRED')
        self.candidate('removed', (0, 10))
        with self.assertRaisesRegex(ValueError, 'still intersects'):
            self.validate()
        self.candidate('retained_with_reason', (0, 10))
        self.assertEqual(self.validate()['editorial_status'], 'READY')
        self.candidate('retained_with_reason', (20, 30))
        with self.assertRaisesRegex(ValueError, 'not fully retained'):
            self.validate()
        self.candidate('removed')
        self.report['candidates'].append(copy.deepcopy(self.report['candidates'][0]))
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            self.validate()

    def test_reasons_and_references_cannot_be_empty(self):
        self.candidate('removed')
        self.report['findings'][0]['reason'] = ' '
        with self.assertRaisesRegex(ValueError, 'reason'):
            self.validate()
        self.candidate('removed')
        self.report['findings'][0]['evidence_ids'] = ['unknown']
        with self.assertRaisesRegex(ValueError, 'evidence_ids'):
            self.validate()

    def test_render_requires_actual_hash_start_end_and_all_seams(self):
        mp4 = self.folder / 'render.mp4'
        mp4.write_bytes(b'actual render fixture')
        self.assertEqual(self.validate(mp4)['editorial_status'], 'REVIEW_REQUIRED')
        self.report['render_review'] = dict(mp4_sha256=q.digest(mp4),
            reviewed_windows=[dict(id=x, target='render', evidence_ids=['ev']) for x in ('w0', 'w2')],
            reviewed_seams=[dict(**self.manifest['seams'][0], target='render', evidence_ids=['ev'])])
        self.assertEqual(self.validate(mp4)['editorial_status'], 'READY')
        self.report['render_review']['reviewed_seams'] = []
        self.assertEqual(self.validate(mp4)['editorial_status'], 'REVIEW_REQUIRED')
        self.report['render_review']['mp4_sha256'] = 'wrong'
        with self.assertRaisesRegex(ValueError, 'stale MP4'):
            self.validate(mp4)


class DeliveryQaTests(QaFixture, unittest.TestCase):
    def export(self, name, qa=False):
        folder = self.folder / name
        args = [str(self.plan_path), '-o', str(folder), '--xml-only']
        if qa:
            self.write(self.report_path, self.report)
            args += ['--qa', str(self.report_path)]
        with patch.object(d, 'media_info', return_value=self.info), contextlib.redirect_stdout(io.StringIO()):
            d.main(args)
        return folder, json.loads((folder / 'manifest.json').read_text())

    def test_draft_then_finalize_preserves_raw_plan_and_relative_source(self):
        folder, manifest = self.export('draft')
        self.assertEqual(manifest['export_status'], 'COMPLETE')
        self.assertEqual(manifest['editorial_status'], 'REVIEW_REQUIRED')
        self.assertFalse((self.folder / 'latest-xml.json').exists())
        self.assertEqual((folder / 'plan.json').read_bytes(), self.plan_path.read_bytes())
        self.write(self.report_path, self.report)
        with patch.object(d, 'media_info', return_value=self.info):
            result = d.finalize(folder, self.report_path)
        self.assertEqual(result['editorial_status'], 'READY')
        pointer = json.loads((self.folder / 'latest-xml.json').read_text())
        self.assertEqual(pointer['directory'], str(folder.resolve()))
        self.assertEqual(pointer['manifest_sha256'], q.digest(folder / 'manifest.json'))

    def test_incomplete_supplied_qa_creates_draft_without_replacing_latest(self):
        ready, _ = self.export('ready', qa=True)
        old_pointer = (self.folder / 'latest-xml.json').read_bytes()
        self.report['reviewed_windows'] = []
        draft, manifest = self.export('draft', qa=True)
        self.assertTrue((draft / 'Dialogue_Premiere.xml').exists())
        self.assertEqual(manifest['editorial_status'], 'REVIEW_REQUIRED')
        self.assertEqual((self.folder / 'latest-xml.json').read_bytes(), old_pointer)

    def test_finalize_detects_corrupt_xml_and_invalidates_its_ready_pointer(self):
        folder, _ = self.export('ready', qa=True)
        xml = folder / 'Dialogue_Premiere.xml'
        xml.write_bytes(xml.read_bytes() + b'corrupt')
        with patch.object(d, 'media_info', return_value=self.info), self.assertRaisesRegex(ValueError, 'XML hash'):
            d.finalize(folder, self.report_path)
        self.assertFalse((self.folder / 'latest-xml.json').exists())
        manifest = json.loads((folder / 'manifest.json').read_text())
        self.assertEqual(manifest['editorial_status'], 'REVIEW_REQUIRED')

    def test_finalize_requires_qa_and_cannot_take_export_arguments(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            d.main(['--finalize', str(self.folder)])

    def test_plan_change_during_probe_does_not_pair_new_bytes_with_old_edit(self):
        def probe(_):
            self.plan_path.write_bytes(self.plan_path.read_bytes() + b' ')
            return self.info
        output = self.folder / 'raced'
        with patch.object(d, 'media_info', side_effect=probe), self.assertRaisesRegex(ValueError, 'changed during media validation'):
            d.main([str(self.plan_path), '-o', str(output), '--xml-only'])
        self.assertFalse(output.exists())

    def test_finalize_rejects_raw_plan_mutation_after_qa(self):
        folder, _ = self.export('draft')
        self.write(self.report_path, self.report)
        def review_then_mutate(*args):
            result = q.validate_qa(*args)
            plan = folder / 'plan.json'
            plan.write_bytes(plan.read_bytes() + b' ')
            return result
        module = SimpleNamespace(validate_qa=review_then_mutate, recheck_bindings=q.recheck_bindings)
        with patch.object(d, 'media_info', return_value=self.info), patch.object(d, 'qa_module', return_value=module), self.assertRaisesRegex(ValueError, 'changed during finalization'):
            d.finalize(folder, self.report_path)
        self.assertFalse((self.folder / 'latest-xml.json').exists())

    def test_finalize_rejects_mp4_mutation_after_qa(self):
        folder, manifest = self.export('draft')
        mp4 = folder / 'Dialogue.mp4'
        mp4.write_bytes(b'original render fixture')
        manifest.update(mode='render', mp4=dict(file=mp4.name, sha256=q.digest(mp4)), mp4_ready=True)
        self.write(folder / 'manifest.json', manifest)
        self.report['render_review'] = dict(mp4_sha256=q.digest(mp4),
            reviewed_windows=[dict(id=x, target='render', evidence_ids=['ev']) for x in ('w0', 'w2')],
            reviewed_seams=[dict(**self.manifest['seams'][0], target='render', evidence_ids=['ev'])])
        self.write(self.report_path, self.report)
        def review_then_mutate(*args):
            result = q.validate_qa(*args)
            mp4.write_bytes(b'changed render')
            return result
        module = SimpleNamespace(validate_qa=review_then_mutate, recheck_bindings=q.recheck_bindings)
        with patch.object(d, 'media_info', return_value=self.info), patch.object(d, 'validate_render', return_value={}), patch.object(d, 'qa_module', return_value=module), self.assertRaisesRegex(ValueError, 'changed during finalization'):
            d.finalize(folder, self.report_path)
        self.assertFalse((self.folder / 'latest-render.json').exists())


if __name__ == '__main__':
    unittest.main()
