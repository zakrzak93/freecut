"""Validate evidence-backed editorial QA for one immutable dialogue plan.

This validates provenance and reviewer declarations, not the truth of an ASR
transcript or a claim that a human listened. Incomplete reviews are drafts;
malformed or stale supplied evidence is an error.
"""
from __future__ import annotations

import hashlib
import json
import math
import wave
from fractions import Fraction
from pathlib import Path


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def reference(value, base, label, bindings=None):
    if not isinstance(value, dict) or not isinstance(value.get('path'), str) or not value.get('sha256'):
        raise ValueError(f'{label} requires path and sha256')
    path = Path(value['path'])
    if not path.is_absolute():
        path = Path(base) / path
    path = path.resolve()
    if not path.is_file() or digest(path) != value['sha256']:
        raise ValueError(f'{label} missing or stale hash')
    if bindings is not None:
        bindings.append(dict(path=str(path), sha256=value['sha256']))
    return path


def recheck_bindings(bindings):
    for value in bindings:
        reference(value, Path.cwd(), 'QA bound file')


def integer(value, label):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f'{label} must be an integer')
    return value


def rows_by_id(rows, label):
    if not isinstance(rows, list):
        raise ValueError(f'{label} must be a list')
    result = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('id'), str) or not row['id'].strip():
            raise ValueError(f'{label} requires nonempty ids')
        if row['id'] in result:
            raise ValueError(f'Duplicate {label} id: {row["id"]}')
        result[row['id']] = row
    return result


def covers(intervals, end):
    edge = 0
    for a, b in sorted(intervals):
        if a > edge:
            return False
        edge = max(edge, b)
    return edge >= end


def expected_seams(edit):
    result = {}
    for i, (left, right) in enumerate(zip(edit['ranges'], edit['ranges'][1:]), 1):
        result[f'seam_{i:05d}'] = dict(output_frame=left[3],
            left_source_end_frame=left[1], right_source_start_frame=right[0])
    return result


def validate_timeline(manifest_path, plan_path, source, info, edit, bindings=None):
    """Check the current audit's hashes, canonical frame map and PCM geometry."""
    manifest_path = Path(manifest_path)
    data = read(manifest_path)
    base = manifest_path.parent
    source_hash, plan_hash = digest(source), digest(plan_path)
    if data.get('time_basis') != 'output':
        raise ValueError('QA requires an output timeline audit')
    if data.get('source_sha256') != source_hash or data.get('plan_sha256') != plan_hash:
        raise ValueError('Timeline audit belongs to a stale source or plan')
    settings = data.get('settings', {})
    bound_plan = reference(settings.get('plan'), base, 'Timeline plan', bindings)
    if digest(bound_plan) != plan_hash:
        raise ValueError('Timeline bound plan differs from current plan')
    bound_source = reference(settings.get('source'), base, 'Timeline source', bindings)
    if bound_source != Path(source).resolve():
        raise ValueError('Timeline source path differs from current source')
    map_path = reference(data.get('source_map'), base, 'Timeline source map', bindings)
    mapping = read(map_path)
    if mapping.get('source_sha256') != source_hash or mapping.get('plan_sha256') != plan_hash:
        raise ValueError('Source map belongs to a stale source or plan')
    fps = Fraction(str(info['fps']))
    if Fraction(str(mapping.get('source_fps'))) != fps:
        raise ValueError('Source map fps mismatch')
    if mapping.get('source_frames') != info['frames'] or mapping.get('output_frames') != edit['output_frames']:
        raise ValueError('Source map duration mismatch')
    sr = integer(mapping.get('sr'), 'Map sr')
    if sr != 16000:
        raise ValueError('Timeline audit requires 16000 Hz PCM')
    ranges = mapping.get('ranges')
    if not isinstance(ranges, list) or len(ranges) != len(edit['ranges']):
        raise ValueError('Source map range count mismatch')
    for i, (row, (a, b, c, d)) in enumerate(zip(ranges, edit['ranges']), 1):
        expected = dict(clip=i, source_start_frame=a, source_end_frame=b,
                        output_start_frame=c, output_end_frame=d)
        out_a, out_b = round(Fraction(c * sr, 1) / fps), round(Fraction(d * sr, 1) / fps)
        source_a = round(Fraction(a * sr, 1) / fps)
        expected.update(source_start_sample=source_a, source_end_sample=source_a + out_b - out_a,
                        output_start_sample=out_a, output_end_sample=out_b)
        for field, value in expected.items():
            if integer(row.get(field), f'Map {field}') != value:
                raise ValueError(f'Source map {field} mismatch')
        for field, frame_value in (('source_start', a), ('source_end', b), ('output_start', c), ('output_end', d)):
            value = row.get(field)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or abs(value - float(Fraction(frame_value) / fps)) > 1e-7:
                raise ValueError(f'Source map {field} seconds mismatch')
    output_samples = round(Fraction(edit['output_frames'] * sr, 1) / fps)
    if mapping.get('output_samples') != output_samples:
        raise ValueError('Source map sample count mismatch')
    audio = reference(dict(path=data.get('audio'), sha256=data.get('audio_sha256')), base, 'Timeline audio', bindings)
    with wave.open(str(audio), 'rb') as wav:
        if (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getnframes()) != (1, 2, sr, output_samples):
            raise ValueError('Timeline audio PCM geometry mismatch')
    ids, windows = data.get('window_ids'), data.get('windows')
    if not isinstance(ids, list) or not isinstance(windows, list) or len(ids) != len(windows) or not ids:
        raise ValueError('Timeline requires identified review windows')
    if any(not isinstance(x, str) or not x for x in ids) or len(set(ids)) != len(ids):
        raise ValueError('Timeline window ids invalid or duplicate')
    window_ranges = {}
    for ident, pair in zip(ids, windows):
        if not isinstance(pair, list) or len(pair) != 2 or any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in pair):
            raise ValueError('Timeline window interval malformed')
        a, b = [Fraction(str(x)) * fps for x in pair]
        if not 0 <= a < b or float(b) > edit['output_frames'] + 1e-6:
            raise ValueError('Timeline window outside output')
        # Preserve fractional positions: outward rounding could erase real gaps
        # between two windows. Only normalize the final float representation.
        if abs(b - edit['output_frames']) < Fraction(1, 1000000):
            b = Fraction(edit['output_frames'])
        window_ranges[ident] = (a, b)
    if not covers(window_ranges.values(), edit['output_frames']):
        raise ValueError('Timeline windows leave uncovered output')
    seams = expected_seams(edit)
    actual_seams = rows_by_id(data.get('seams'), 'timeline seams')
    if actual_seams.keys() != seams.keys():
        raise ValueError('Timeline seam inventory mismatch')
    for ident, expected in seams.items():
        if any(integer(actual_seams[ident].get(k), f'Seam {k}') != v for k, v in expected.items()):
            raise ValueError('Timeline seam identity mismatch')
    return data, window_ranges, seams


def validate_qa(report_path, plan_path, source, info, edit, mp4_path=None):
    report_path = Path(report_path).resolve()
    report_bytes = report_path.read_bytes()
    report_hash = hashlib.sha256(report_bytes).hexdigest()
    report = json.loads(report_bytes.decode('utf-8-sig'))
    bindings = [dict(path=str(report_path), sha256=report_hash),
                dict(path=str(Path(plan_path).resolve()), sha256=digest(plan_path))]
    if report.get('version') != 1:
        raise ValueError('Unsupported QA version')
    if report.get('source_sha256') != digest(source) or report.get('plan_sha256') != digest(plan_path):
        raise ValueError('QA belongs to a stale source or plan')
    manifest_path = reference(report.get('audit_manifest'), report_path.parent, 'QA audit manifest', bindings)
    _, windows, seams = validate_timeline(manifest_path, plan_path, source, info, edit, bindings)
    evidence = rows_by_id(report.get('evidence'), 'evidence')
    for row in evidence.values():
        reference(row, report_path.parent, 'QA evidence', bindings)

    def check_evidence(row):
        refs = row.get('evidence_ids')
        if not isinstance(refs, list) or not refs or any(not isinstance(x, str) or x not in evidence for x in refs) or len(set(refs)) != len(refs):
            raise ValueError('Review requires unique known evidence_ids')

    missing = []

    def reviews(container, render=False):
        label = 'render ' if render else ''
        checked_windows = rows_by_id(container.get('reviewed_windows', []), label + 'reviewed windows')
        checked_seams = rows_by_id(container.get('reviewed_seams', []), label + 'reviewed seams')
        if set(checked_windows) - set(windows) or set(checked_seams) - set(seams):
            raise ValueError('Review refers to unknown current window or seam')
        for row in list(checked_windows.values()) + list(checked_seams.values()):
            check_evidence(row)
            if render and row.get('target') != 'render':
                raise ValueError('Render review requires target=render')
        for ident, row in checked_seams.items():
            if any(integer(row.get(k), f'Review seam {k}') != v for k, v in seams[ident].items()):
                raise ValueError('Reviewed seam identity is stale')
        required_windows = {next(iter(windows)), next(reversed(windows))} if render else set(windows)
        if not required_windows.issubset(checked_windows) or (not render and not covers([windows[k] for k in checked_windows], edit['output_frames'])):
            missing.append(label + 'window reviews incomplete')
        if set(checked_seams) != set(seams):
            missing.append(label + 'seam reviews incomplete')

    reviews(report)
    candidates = rows_by_id(report.get('candidates'), 'candidates')
    findings = rows_by_id(report.get('findings'), 'findings')
    if set(findings) - set(candidates):
        raise ValueError('Finding has no declared candidate')
    for ident, candidate in candidates.items():
        pair = candidate.get('source_frames')
        if not isinstance(pair, list) or len(pair) != 2:
            raise ValueError('Candidate requires source_frames pair')
        a, b = [integer(v, 'Candidate frame') for v in pair]
        if not 0 <= a < b <= info['frames'] or not isinstance(candidate.get('kind'), str) or not candidate['kind'].strip():
            raise ValueError('Candidate range or kind invalid')
        if ident not in findings:
            missing.append(f'Candidate {ident} has no decision')
            continue
        finding = findings[ident]
        reason = finding.get('reason')
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError('Finding requires a reason')
        check_evidence(finding)
        retained = sum(max(0, min(b, y) - max(a, x)) for x, y, _, _ in edit['ranges'])
        disposition = finding.get('disposition')
        if disposition == 'removed':
            if retained:
                raise ValueError('Claimed removed candidate still intersects retained frames')
        elif disposition == 'retained_with_reason':
            if retained != b - a:
                raise ValueError('Claimed retained candidate is not fully retained')
        elif disposition == 'unresolved':
            missing.append(f'Candidate {ident} unresolved')
        else:
            raise ValueError('Unknown finding disposition')
    if mp4_path is not None:
        bindings.append(dict(path=str(Path(mp4_path).resolve()), sha256=digest(mp4_path)))
        render = report.get('render_review')
        if render is None:
            missing.append('Actual render review missing')
        elif not isinstance(render, dict) or render.get('mp4_sha256') != digest(mp4_path):
            raise ValueError('Render QA belongs to a stale MP4')
        else:
            reviews(render, render=True)
    elif report.get('render_review') is not None:
        raise ValueError('Render review supplied without an actual MP4')
    recheck_bindings(bindings)
    return dict(editorial_status='REVIEW_REQUIRED' if missing else 'READY',
                incomplete_reasons=missing, report=dict(path=str(report_path), sha256=report_hash),
                audit_manifest=dict(path=str(manifest_path), sha256=report['audit_manifest']['sha256']), validated_files=bindings,
                reviewed_windows=len(report.get('reviewed_windows', [])),
                reviewed_seams=len(report.get('reviewed_seams', [])), findings=len(findings))
