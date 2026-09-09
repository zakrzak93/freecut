"""Validate evidence-backed editorial QA for one immutable dialogue plan.

This validates provenance and reviewer declarations, not the truth of an ASR
transcript or a claim that a human listened. Incomplete reviews are drafts;
malformed or stale supplied evidence is an error.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import wave
from contextvars import ContextVar
from fractions import Fraction
from pathlib import Path


# A validation owns its cache. Public digest calls outside validation remain fresh.
_DIGEST_CACHE = ContextVar('dialogue_qa_digest_cache', default=None)


def file_signature(path):
    stat = path.stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def hash_file(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def digest(path):
    path = Path(path).resolve()
    before = file_signature(path)
    cache = _DIGEST_CACHE.get()
    key = (str(path), before)
    if cache is not None and key in cache:
        return cache[key]
    result = hash_file(path)
    if file_signature(path) != before:
        raise ValueError('QA file changed while hashing')
    if cache is not None:
        cache[key] = result
    return result


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
    # Never reuse validation hashes here: force a full fresh read of every
    # distinct bound file, even if its size and timestamp appear unchanged.
    unique = {}
    for value in bindings:
        path = str(Path(value['path']).resolve())
        if path in unique and unique[path]['sha256'] != value['sha256']:
            raise ValueError('QA bound file has conflicting hashes')
        unique[path] = value
    token = _DIGEST_CACHE.set(None)
    try:
        for value in unique.values():
            reference(value, Path.cwd(), 'QA bound file')
    finally:
        _DIGEST_CACHE.reset(token)


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


def risk_module():
    spec = importlib.util.spec_from_file_location('dialogue_risks_qa', Path(__file__).with_name('dialogue_risks.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def scoped_ranges(rows, limit, label):
    if not isinstance(rows, list) or not rows:
        raise ValueError(f'{label} requires nonempty ranges')
    result = []
    for pair in rows:
        if not isinstance(pair, list) or len(pair) != 2 or any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in pair):
            raise ValueError(f'{label} range malformed')
        a, b = map(lambda x: Fraction(str(x)), pair)
        if not 0 <= a < b or b > limit + Fraction(1, 1000000):
            raise ValueError(f'{label} range outside declared time basis')
        result.append((a, min(b, limit)))
    return result


def interval_covered(interval, scopes):
    a, b = interval
    clipped = [(max(a, x) - a, min(b, y) - a) for x, y in scopes if y > a and x < b]
    return covers(clipped, b - a)


def validate_risk_inventory(report, base, manifest_path, bindings, missing):
    value = report.get('risk_inventory')
    if value is None:
        missing.append('Risk inventory missing')
        return None
    path = reference(value, base, 'Risk inventory', bindings)
    inventory = read(path)
    settings = inventory.get('settings', {})
    if inventory.get('version') != 1 or settings.get('manifest', {}).get('sha256') != digest(manifest_path):
        raise ValueError('Risk inventory belongs to another audit')
    for key in ('manifest', 'source', 'plan', 'source_map', 'audio', 'source_audio'):
        reference(settings.get(key), path.parent, f'Risk {key}', bindings)
    transcript = settings.get('transcript')
    transcript_path = reference(transcript, path.parent, 'Risk transcript', bindings) if transcript else None
    regenerated = risk_module().build_inventory(manifest_path, transcript_path, settings.get('parameters'))
    # Regeneration prevents deleting difficult checks and rehashing the inventory.
    if any(inventory.get(k) != regenerated.get(k) for k in ('settings', 'checks', 'asr_complete')):
        raise ValueError('Risk inventory checks differ from deterministic current inventory')
    if not inventory.get('asr_complete') or transcript_path is None:
        missing.append('Risk inventory lacks complete current timeline ASR')
    return inventory


def proof_scopes(evidence, base, inventory, manifest_path, source, info, edit, bindings, missing, mp4_path=None):
    """Typed evidence earns coverage; prose and arbitrary file hashes do not."""
    result = {}
    fps = Fraction(str(info['fps']))
    limits = dict(source=Fraction(info['frames']) / fps, output=Fraction(edit['output_frames']) / fps)
    if inventory is None:
        return result
    settings = inventory['settings']
    known_source_hashes = {digest(source), settings['source_audio']['sha256']}
    for ident, item in evidence.items():
        kind, basis, role = item.get('kind'), item.get('time_basis'), item.get('role')
        if kind == 'review':
            continue  # Written reasoning supports a decision, never coverage alone.
        if kind is None or basis is None or role is None or item.get('ranges') is None:
            missing.append(f'Evidence {ident} lacks typed coverage')
            continue
        if kind not in ('asr', 'waveform', 'audio') or basis not in limits or role not in ('kept', 'extended_context', 'candidate', 'timeline', 'render'):
            raise ValueError('Unknown evidence kind, time_basis or role')
        if item.get('source_sha256') != digest(source):
            raise ValueError('Evidence source identity mismatch')
        if basis == 'output' and item.get('plan_sha256') != settings['plan']['sha256']:
            raise ValueError('Output evidence plan identity mismatch')
        declared = scoped_ranges(item['ranges'], limits[basis], f'Evidence {ident}')
        if basis == 'source' and role == 'kept':
            kept = [(Fraction(a) / fps, Fraction(b) / fps) for a, b, _, _ in edit['ranges']]
            if not all(interval_covered(pair, kept) for pair in declared):
                raise ValueError('Kept evidence scope includes removed source material')
        path = reference(item, base, 'Typed evidence', bindings)
        authorized_audio_hash = settings['source_audio']['sha256'] if basis == 'source' else settings['audio']['sha256']
        if role == 'render':
            if basis != 'output' or mp4_path is None:
                raise ValueError('Render evidence requires actual output MP4')
            authorized_audio_hash = digest(mp4_path)
        if kind == 'audio':
            if digest(path) != authorized_audio_hash:
                raise ValueError('Audio evidence is not the bound audio view')
        elif kind == 'waveform':
            artifact = read(path)
            audio = reference(artifact.get('audio'), path.parent, 'Waveform audio', bindings)
            if digest(audio) != authorized_audio_hash or artifact.get('time_basis') != basis:
                raise ValueError('Waveform is not bound to the declared audio view')
            actual = scoped_ranges(artifact.get('ranges'), limits[basis], 'Waveform artifact')
            if not all(interval_covered(pair, actual) for pair in declared):
                raise ValueError('Waveform evidence overclaims artifact ranges')
        else:
            artifact = read(path)
            ids = item.get('window_ids')
            if not isinstance(ids, list) or not ids:
                missing.append(f'ASR evidence {ident} lacks selected window ids')
                continue
            if len(set(ids)) != len(ids):
                raise ValueError('Duplicate ASR evidence window id')
            if artifact.get('time_basis') != basis:
                raise ValueError('ASR evidence time basis mismatch')
            audit_path = reference(artifact.get('settings', {}).get('manifest'), path.parent, 'ASR manifest', bindings)
            audit_data = read(audit_path)
            original = reference(audit_data.get('settings', {}).get('source'), audit_path.parent, 'ASR original source', bindings)
            if basis == 'source':
                if digest(original) not in known_source_hashes:
                    raise ValueError('Source ASR is not bound to original media or verified decoded audio')
            elif role != 'render':
                if digest(audit_path) != digest(manifest_path):
                    raise ValueError('Output ASR belongs to another timeline')
            else:
                if digest(original) != authorized_audio_hash:
                    raise ValueError('Render ASR belongs to another MP4')
            audio_ref = dict(path=audit_data.get('audio'), sha256=audit_data.get('audio_sha256'))
            reference(audio_ref, audit_path.parent, 'ASR decoded audio', bindings)
            rows = artifact.get('windows', [])
            indexed = {}
            for row in rows:
                key = row.get('window_id')
                if key in indexed:
                    raise ValueError('Duplicate typed ASR window id')
                indexed[key] = row
            if any(key not in indexed for key in ids):
                raise ValueError('Unknown selected ASR window id')
            manifest_ids = audit_data.get('window_ids', [])
            manifest_windows = audit_data.get('windows', [])
            if len(manifest_ids) != len(manifest_windows) or len(set(manifest_ids)) != len(manifest_ids):
                raise ValueError('ASR manifest lacks unique typed windows')
            authorized_windows = dict(zip(manifest_ids, manifest_windows))
            for key in ids:
                if indexed[key].get('time_basis', basis) != basis:
                    raise ValueError('Selected ASR window time basis mismatch')
                if key not in authorized_windows or [indexed[key].get('start'), indexed[key].get('end')] != authorized_windows[key]:
                    raise ValueError('Selected ASR window differs from its manifest')
            actual = scoped_ranges([[indexed[key]['start'], indexed[key]['end']] for key in ids], limits[basis], 'Selected ASR windows')
            if not all(interval_covered(pair, actual) for pair in declared):
                raise ValueError('ASR evidence overclaims selected windows')
            # Kept source proof must be a crop of retained material, not a broad
            # source window crossing a removed side of a cut.
            if basis == 'source' and role == 'kept':
                kept = [(Fraction(a) / fps, Fraction(b) / fps) for a, b, _, _ in edit['ranges']]
                if not all(interval_covered(pair, kept) for pair in actual):
                    raise ValueError('Kept ASR view includes removed source material')
        result[ident] = dict(time_basis=basis, role=role, ranges=declared)
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
    token = _DIGEST_CACHE.set({})
    try:
        return _validate_qa(report_path, plan_path, source, info, edit, mp4_path)
    finally:
        _DIGEST_CACHE.reset(token)


def _validate_qa(report_path, plan_path, source, info, edit, mp4_path=None):
    report_path = Path(report_path).resolve()
    report_bytes = report_path.read_bytes()
    report_hash = hashlib.sha256(report_bytes).hexdigest()
    report = json.loads(report_bytes.decode('utf-8-sig'))
    bindings = [dict(path=str(report_path), sha256=report_hash),
                dict(path=str(Path(plan_path).resolve()), sha256=digest(plan_path))]
    version = report.get('version')
    if isinstance(version, bool) or version not in (1, 2):
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
    inventory = None
    scopes = {}
    if version == 1:
        missing.append('Legacy QA v1 cannot certify editorial READY; scoped risk review v2 required')
    else:
        inventory = validate_risk_inventory(report, report_path.parent, manifest_path, bindings, missing)
        scopes = proof_scopes(evidence, report_path.parent, inventory, manifest_path, source,
                              info, edit, bindings, missing, mp4_path)

    def scope_covers(refs, basis, interval, role=None):
        available = []
        for ident in refs:
            value = scopes.get(ident)
            if value and value['time_basis'] == basis and (role is None or value['role'] == role):
                available.extend(value['ranges'])
        return interval_covered(interval, available)

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
        if version == 2:
            fps = Fraction(str(info['fps']))
            duration = Fraction(edit['output_frames']) / fps
            for ident, row in checked_windows.items():
                interval = tuple(x / fps for x in windows[ident])
                if not scope_covers(row['evidence_ids'], 'output', interval, 'render' if render else None):
                    missing.append(f'{label}window {ident} lacks scoped evidence coverage')
            for ident, row in checked_seams.items():
                edge = Fraction(seams[ident]['output_frame']) / fps
                interval = (max(Fraction(0), edge - 1), min(duration, edge + 1))
                if not scope_covers(row['evidence_ids'], 'output', interval, 'render' if render else None):
                    missing.append(f'{label}seam {ident} lacks scoped evidence coverage')
        required_windows = {next(iter(windows)), next(reversed(windows))} if render else set(windows)
        if not required_windows.issubset(checked_windows) or (not render and not covers([windows[k] for k in checked_windows], edit['output_frames'])):
            missing.append(label + 'window reviews incomplete')
        if set(checked_seams) != set(seams):
            missing.append(label + 'seam reviews incomplete')

    reviews(report)
    if version == 2 and inventory is not None:
        checks = rows_by_id(inventory['checks'], 'inventory checks')
        decisions = rows_by_id(report.get('check_reviews', []), 'check reviews')
        if set(decisions) - set(checks):
            raise ValueError('Review refers to an unknown inventory check')
        fps = Fraction(str(info['fps']))
        previous = None
        for ident, check in checks.items():
            if ident not in decisions:
                missing.append(f'Risk check {ident} not reviewed')
                continue
            decision = decisions[ident]
            if not isinstance(decision.get('reason'), str) or not decision['reason'].strip():
                raise ValueError('Risk check review requires reason')
            check_evidence(decision)
            disposition = decision.get('disposition')
            if disposition not in ('resolved_kept', 'resolved_cut', 'restored', 'unresolved'):
                raise ValueError('Unknown risk disposition')
            if disposition == 'unresolved':
                missing.append(f'Risk check {ident} unresolved')
            for required in check['evidence_windows']:
                interval = (Fraction(str(required['start'])), Fraction(str(required['end'])))
                if not scope_covers(decision['evidence_ids'], required['time_basis'], interval, required['role']):
                    missing.append(f'Risk check {ident} lacks {required["role"]} evidence coverage')
            if disposition in ('resolved_cut', 'restored'):
                pair = decision.get('source_frames')
                if not isinstance(pair, list) or len(pair) != 2:
                    raise ValueError('Cut/restoration review requires source_frames')
                a, b = [integer(x, 'Changed source frame') for x in pair]
                if not 0 <= a < b <= info['frames']:
                    raise ValueError('Changed source frames outside source')
                if not any(Fraction(a) / fps < Fraction(str(y)) and Fraction(b) / fps > Fraction(str(x)) for x, y in check['source_ranges']):
                    raise ValueError('Claimed change is unrelated to inventory check')
                kept = sum(max(0, min(b, y) - max(a, x)) for x, y, _, _ in edit['ranges'])
                if disposition == 'resolved_cut' and kept:
                    raise ValueError('Resolved cut still includes retained frames')
                if disposition == 'restored':
                    if kept != b - a:
                        raise ValueError('Restored source frames are not fully retained')
                    if previous is None:
                        prev_path = reference(report.get('previous_plan'), report_path.parent, 'Previous plan', bindings)
                        prev = read(prev_path)
                        prev_source = Path(prev['source'])
                        if not prev_source.is_absolute():
                            prev_source = prev_path.parent / prev_source
                        if prev_source.resolve() != Path(source).resolve():
                            raise ValueError('Previous plan source mismatch')
                        previous = []
                        for row in prev['ranges']:
                            x, y = Fraction(str(row['start'])) * fps, Fraction(str(row['end'])) * fps
                            if x.denominator != 1 or y.denominator != 1 or not 0 <= x < y <= info['frames']:
                                raise ValueError('Previous plan range invalid')
                            previous.append((int(x), int(y)))
                    if any(max(a, x) < min(b, y) for x, y in previous):
                        raise ValueError('Claimed restored frames already existed in previous plan')
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
