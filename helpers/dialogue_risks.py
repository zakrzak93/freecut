"""Discover immutable review obligations, never automatic audio cuts."""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
from fractions import Fraction
from pathlib import Path


def audit_module():
    spec = importlib.util.spec_from_file_location('_risk_audit', Path(__file__).with_name('dialogue_audit.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PARAMETERS = dict(context=.35, short_clip=1.2, quiet_gap=.5, micro_gap=.10,
                  micro_max=1.2, quiet_dbfs=-42, repeat_max_gap=2., ngram_max=3)


def mapped(interval, rows, inverse=False):
    a, b = interval
    origin, target = ('source', 'output') if inverse else ('output', 'source')
    return [[max(a, r[origin + '_start']) - r[origin + '_start'] + r[target + '_start'],
             min(b, r[origin + '_end']) - r[origin + '_start'] + r[target + '_start']]
            for r in rows if max(a, r[origin + '_start']) < min(b, r[origin + '_end'])]


def tokens(transcript):
    """Deduplicate temporal observations, never repeats within the same window."""
    import re
    merged = []
    observations = []
    for i, window in enumerate(transcript.get('windows', [])):
        for segment in window.get('segments', []):
            for word in segment.get('words', []):
                text = re.sub(r'[^\w]+', '', word.get('text', '').lower())
                a, b = word.get('start'), word.get('end')
                if not text or not isinstance(a, (float, int)) or not isinstance(b, (float, int)):
                    continue
                if not math.isfinite(a) or not math.isfinite(b) or not 0 <= a < b:
                    continue
                observations.append(dict(text=text, start=a, end=b, windows={i}))
    for word in sorted(observations, key=lambda w: (w['start'], w['end'])):
        match = None
        for old in reversed(merged):
            overlap = min(word['end'], old['end']) - max(word['start'], old['start'])
            if (old['text'] == word['text'] and not old['windows'] & word['windows'] and
                    overlap >= .5 * min(word['end'] - word['start'], old['end'] - old['start'])):
                match = old
                break
        if match is None:
            merged.append(word)
        else:
            match['windows'].update(word['windows'])
    return sorted(merged, key=lambda w: (w['start'], w['end']))


def repeats(words):
    result, seen = [], set()
    for n in range(1, PARAMETERS['ngram_max'] + 1):
        for i in range(len(words) - 2 * n + 1):
            left, right = words[i:i+n], words[i+n:i+2*n]
            if [w['text'] for w in left] != [w['text'] for w in right]:
                continue
            if not -.05 <= right[0]['start'] - left[-1]['end'] <= PARAMETERS['repeat_max_gap']:
                continue
            signature = (left[0]['start'], right[-1]['end'])
            if signature not in seen:
                result.append(dict(start=signature[0], end=signature[1],
                                   quote=' '.join(w['text'] for w in left + right), ngram=n))
                seen.add(signature)
    return sorted(result, key=lambda x: (x['start'], x['end']))


def discover(data, mapping, source_samples, output_samples, transcript=None):
    import numpy as np
    audit = audit_module()
    rows = mapping['ranges']
    duration = float(Fraction(mapping['source_frames']) / Fraction(mapping['source_fps']))
    checks = []

    def add(ident, kind, out, src, evidence, **details):
        checks.append(dict(id=ident, kind=kind, output_ranges=out, source_ranges=src,
                           evidence_windows=evidence, **details))

    def evidence(a, b, basis, role):
        return dict(time_basis=basis, role=role, start=a, end=b)

    def edge_levels(t):
        n, pos = round(.01 * audit.SR), round(t * audit.SR)
        def db(values):
            if not len(values):
                return -240.
            rms = float(np.sqrt(np.mean((values - values.mean()) ** 2)))
            return round(20 * math.log10(max(rms, 1e-12)), 3)
        before, after = db(source_samples[max(0, pos-n):pos]), db(source_samples[pos:pos+n])
        return dict(rms_before_dbfs=before, rms_after_dbfs=after,
                    edge_active=max(before, after) >= PARAMETERS['quiet_dbfs'], measurement_seconds=.01)

    for i, row in enumerate(rows, 1):
        a, b, c, d = (row[k] for k in ('source_start', 'source_end', 'output_start', 'output_end'))
        for side, edge in (('start', a), ('end', b)):
            kept = [a, min(b, a+.35)] if side == 'start' else [max(a, b-.35), b]
            extended = [max(0, edge-.35), min(duration, edge+.35)]
            add(f'edge_{i:05d}_{side}', 'source_edge', mapped(kept, rows, True), [extended],
                [evidence(*kept, 'source', 'kept'), evidence(*extended, 'source', 'extended_context')],
                clip=i, side=side, source_frame=row['source_' + side + '_frame'], **edge_levels(edge))
        if b-a <= PARAMETERS['short_clip'] + 1e-9:
            add(f'short_clip_{i:05d}', 'short_kept_clip', [[c, d]], [[a, b]],
                [evidence(a, b, 'source', 'kept'),
                 evidence(max(0, a-.35), min(duration, b+.35), 'source', 'extended_context')])
    output_quiet = audit.quiet_gaps(output_samples, threshold=PARAMETERS['quiet_dbfs'],
                                    minimum=PARAMETERS['quiet_gap'])
    for i, (a, b) in enumerate(output_quiet, 1):
        if b-a >= PARAMETERS['quiet_gap']:
            a, b = max(0, a), min(data['duration'], b)
            add(f'quiet_{i:05d}', 'long_output_quiet_gap', [[a, b]], mapped([a, b], rows),
                [evidence(a, b, 'output', 'candidate')])
    source_quiet = audit.quiet_gaps(source_samples, threshold=PARAMETERS['quiet_dbfs'],
                                    minimum=PARAMETERS['micro_gap'])
    islands = audit.subtract([[0, duration]], source_quiet)
    for i, (a, b) in enumerate(islands, 1):
        out = mapped([a, b], rows, True)
        if b-a <= PARAMETERS['micro_max'] and out:
            add(f'micro_{i:05d}', 'micro_island', out, [[a, b]],
                [evidence(max(0, a-.35), min(duration, b+.35), 'source', 'extended_context')])
    if transcript is not None:
        for i, repeat in enumerate(repeats(tokens(transcript)), 1):
            a, b = repeat['start'], repeat['end']
            if b > data['duration'] + 1 / audit.SR:
                raise ValueError('ASR repeat lies outside output timeline')
            add(f'repeat_{i:05d}', 'adjacent_repeat', [[a, b]], mapped([a, b], rows),
                [evidence(max(0, a-.35), min(data['duration'], b+.35), 'output', 'extended_context')],
                quote=repeat['quote'], ngram=repeat['ngram'])
    return checks


def build_inventory(manifest_path, transcript_path=None, parameters=None):
    import numpy as np
    if parameters is not None and parameters != PARAMETERS:
        raise ValueError('Unsupported risk parameters; use this version defaults')
    audit = audit_module()
    manifest, transcript = Path(manifest_path).resolve(), transcript_path
    manifest_fp = audit.fingerprint(manifest)
    data = audit.checked_manifest(manifest)
    if data.get('time_basis') != 'output':
        raise ValueError('Risk inventory requires an output timeline manifest')
    source_audio = manifest.parent / 'source_decoded_16k.wav'
    if data.get('source_audio') != audit.fingerprint(source_audio):
        raise ValueError('Decoded source PCM provenance missing or stale; regenerate timeline')
    bindings = dict(manifest=manifest_fp, source=data['settings']['source'], plan=data['settings']['plan'],
                    source_map=data['source_map'], audio=audit.fingerprint(data['audio']),
                    source_audio=data['source_audio'],
                    transcript=audit.fingerprint(transcript) if transcript else None)
    transcribed = audit.read_json(transcript) if transcript else None
    if transcribed is not None:
        if transcribed.get('settings', {}).get('manifest') != manifest_fp or transcribed.get('time_basis') != 'output':
            raise ValueError('ASR is not bound to this output manifest')
        if transcribed.get('completed_windows') != len(data['windows']) or transcribed.get('expected_windows') != len(data['windows']):
            raise ValueError('ASR timeline inventory is incomplete')
        actual = transcribed.get('windows', [])
        if len(actual) != len(data['windows']):
            raise ValueError('ASR completed count does not match actual windows')
        for row, ident, pair in zip(actual, data['window_ids'], data['windows']):
            if row.get('window_id') != ident or [row.get('start'), row.get('end')] != pair:
                raise ValueError('ASR window identity or interval mismatch')
    settings = dict(bindings, parameters=PARAMETERS)
    source = audit.load_audio(source_audio)
    output = audit.load_audio(data['audio'])
    mapping = audit.read_json(data['source_map']['path'])
    for row in mapping['ranges']:
        if not np.array_equal(source[row['source_start_sample']:row['source_end_sample']],
                              output[row['output_start_sample']:row['output_end_sample']]):
            raise ValueError('Source PCM does not match sampled timeline map')
    report = dict(version=1, settings=settings, asr_complete=transcribed is not None,
                  checks=discover(data, mapping, source, output, transcribed),
                  warning='Required review candidates only, never automatic cuts or a READY certificate.')
    for value in bindings.values():
        if value is not None and audit.fingerprint(value['path']) != value:
            raise ValueError('Risk input changed during discovery')
    return report


def generate(manifest, transcript, out):
    audit, out = audit_module(), Path(out).resolve()
    report = build_inventory(manifest, transcript)
    if out.exists():
        if audit.read_json(out) != report:
            raise ValueError('Existing risk report differs; choose a new output path')
    else:
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open('x', encoding='utf-8') as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--transcript')
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    try:
        generate(args.manifest, args.transcript, args.out)
    except (ValueError, FileNotFoundError) as exc:
        parser.exit(1, f'Risk inventory failed: {exc}\n')
    print(Path(args.out).resolve())


if __name__ == '__main__':
    main()
