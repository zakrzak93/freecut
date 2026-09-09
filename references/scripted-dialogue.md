# Scripted dialogue: preserve content, remove failed delivery

Read for narration/talking-head cleanup, retakes, word restarts, pauses, coughs or throat clearing. These defaults reflect the user's accepted editing workflow; they do not apply to intentional performance sounds in music, comedy, interviews or other creative edits unless requested.

## Editorial contract

- A supplied script is a reference for sequence and completeness. Actual recorded words and source-media timing are authoritative. Never put the script in a transcription prompt or fabricate missing speech.
- In a **local group of attempts of the same sentence**, retain the last complete acceptable attempt and remove all earlier attempts, including their beginnings. A one- or two-word difference is not itself a reason to reject a good take. Preserve similar lines in different contexts.
- Preserve sentence order and unique content. Do not summarize, target an arbitrary shorter runtime, or silently drop a sentence. Do not assemble a sentence from several takes when a complete final take exists.
- For narration cleanup, remove the whole genuinely abandoned unfinished sentence even if no complete later attempt exists. Preserve intentional trailing-off, rhetorical fragments and complete author additions. Neither ASR ellipses nor absence from the script proves an unfinished sentence. Confirm abandonment from source evidence; unresolved meaning stays explicitly unresolved. Project-specific user instructions override this default.
- If the speaker restarts only a word or clause and there is no complete sentence retake, remove an unambiguous abandoned local start while preserving the unique surrounding speech and the final complete continuation. Do not use this exception to construct new wording. If completeness, meaning or a safe boundary is unresolved, preserve the content and flag that specific uncertainty.
- Remove confirmed false starts, truncated syllables, repeated word beginnings and non-content coughs/throat clears. Markers explain actual edits and genuine review points; they do not replace deleting a confirmed defect.
- Shorten excess pauses while retaining natural articulation and breathing space. Do not interpret “remove silence” as eliminating every acoustic gap inside speech.
- Work autonomously from the available footage. A user timestamp is useful additional evidence, not a prerequisite for investigating audible defects.

## Evidence pass before the first render

1. **Inventory and cache.** Read project history, probe the actual source frame rate/resolution/audio, and keep a fingerprinted, word-timed raw transcript. Use local Whisper: normally large-v3 for detailed dialogue review when resources permit; Apple Silicon can use mlx-whisper, other machines faster-whisper. A larger model alone does not fix repetition smoothing.
2. **Review broad text and script.** Use phrase packing to navigate, flag missing or unexpected content, and group only nearby attempts. Never derive source cut times from the script.
3. **Inspect short acoustic attempts independently.** Detect low-energy boundaries in the actual waveform. Read each short speech island on its own, with word timestamps, no script prompt and no carried-over previous-text context. Compare adjacent islands and use a wider context crop where an isolated fragment is unintelligible. Preserve both readings: a long crop can smooth “W Techno… W Technotronic…” into one fluent sentence; a tiny crop can hallucinate unrelated words.
4. **Cover the full retained timeline.** Do not inspect only the obvious retakes or edit seams. Track which retained regions were checked, including empty/low-confidence ASR and unusually short retained islands. Subdivide long uninterrupted spans with overlapping context so their interiors are not ignored; do not mistake overlap in the verification windows for a retake in the source.
5. **Check non-speech sounds separately.** Speech-free but acoustically active spans are candidates for coughs, throat clearing, clicks and breaths. ASR can omit these sounds; a silence detector cannot classify them. Use local sound-event classification when available, plus waveform, nearby spoken content, source frames and actual listening when supported. A low score does not rule a cough out; a high score is not permission to cut speech. Check the whole retained timeline, then inspect suspicious events more narrowly.
6. **Resolve boundaries in source time.** Start with raw/verification word times, confirm the actual end and onset, and use frame-aligned boundaries with a small protective margin. Typical starting values from the accepted workflow: DC-removed RMS in 10 ms windows, -42 dBFS for quiet candidates, 0.35–0.5 s gaps for separate attempts, excess pauses ≥0.5 s shortened with roughly 120 ms of air per edge. These are configurable candidates, not universal thresholds or proof that a quiet consonant is absent. Use 30 ms audio edge fades in the rendered clips.

## Review the actual edited timeline

After selecting source ranges, run `timeline --plan ... --out-dir ...` even when the user requests XML only. It creates an internal mono PCM WAV with the XML's actual hard cuts, a frame-based output-to-source map and a fingerprinted manifest. Do not add fades to this verification WAV that are absent from the XML. It is an audit artifact, not a requested MP4 or a substitute for checking an actual rendered MP4.

Review all default 4-second windows with 1.25-second overlap, including quiet/empty windows and clip interiors. These configurable values are a starting point, not a guarantee against smoothing. Record each reviewed window and its evidence; completed ASR jobs alone do not count as review. Inspect short acoustic islands independently as well: even a four-second window can swallow an earlier two-word attempt.

When adjacent or differently sized crops disagree about a repeated beginning, preserve the suspicion until it is resolved. Check the failed attempt and final continuation in separate source crops and inspect their actual boundaries. Use source frame mapping to distinguish two real utterances from the same utterance appearing in overlapping windows. A fluent wider crop cannot overrule evidence of a short retry. For unintelligible micro-crops, add context; use another cached local model only if the disagreement remains. Keep both readings and the decision evidence.

Review each final join with enough context to establish the preceding word's complete tail, exactly one last acceptable attempt and the subsequent words. Check newly edited spans and their joins again after every revision. Reuse old evidence only for unchanged source content; remap it into a new report bound to the current plan. A previous report never automatically approves a changed plan or new join.

Check short active islands even when ASR assigned words to them. A hallucinated "Dziękuję" must not hide an impulse or breath from the event pass. The event helper preserves its original respiratory scores, top ten classes and `Speech`; these remain uncalibrated evidence, never automatic deletion rules.

The helpers automate evidence collection and delivery mechanics. They do **not** certify semantic take selection or automatically turn classifier scores into deletions. Save the review decisions and unresolved issues explicitly.

## Case-derived checks that must not regress

- Broad ASR can report “Modelka …” only once even though a failed full beginning precedes the later sentence. Compare isolated attempts instead of repeatedly trusting the same smoothed transcript.
- A short repeated tail such as “Do tego…” can survive after an earlier longer retry was removed. Audit the selected last attempt itself, not only rejected material.
- A zero-duration repeated ASR word is not reliable proof of an audible repeat. Inspect the waveform/crop rather than cutting a guessed syllable.
- An ASR word may end early: in the accepted case, the actual tail of “Sensation” continued beyond its text timestamp, followed by two throat-clearing bursts. Protect the word before removing the non-speech event.
- Correctly encoded/exported video can still contain bad editorial choices. Passing ffprobe or full decoding is technical validation, not a listening or content-review certificate.
- In the Śmierdziele case, "dziecko widziało", two earlier "ściskasz" attempts and an incomplete "nie ma skomplikowanych" survived broad review. Separate attempts and the actual edited timeline exposed them. Treat these as regression cases, not strings to remove automatically.
- A cropped fluent passage was misread as "KONIEC"; another local model recovered its real content. Conversely, an impulse and breath were misread as "Dziękuję". Neither a familiar closing phrase nor a single decoder's confidence establishes spoken content.

## Plan, approval and correction

Persist a versioned source keep plan with ordered source `start`/`end` ranges and explicit removal reasons. Keep the source, script and raw transcript unchanged. Record the audit settings, verified coverage and unresolved regions.

Show a concrete cut plan before rendering when requested. Existing approval covers corrections within that agreed strategy; do not repeatedly ask the same scope question. A command-line approval flag records the agent's use of existing user authorization, not an independent source of permission. New editorial changes outside the approved strategy still need review.

When the user reports a missed cut:

- Confirm the actual file/sequence/version they are viewing when available, without assuming the active Premiere project is this footage. Read-only sequence inspection can distinguish an import problem from a flawed keep plan.
- Interpret the supplied timestamp using that version's timeline and frame rate; map it back to the original source. A timestamp from an older cut is not a source timestamp.
- Inspect the reported source region narrowly, then audit other regions susceptible to the same failure. Do not issue the same plan again under a new filename and describe it as fixed.
- Update the keep plan, rebuild requested artifacts and test the actual changed intervals. State what changed, what was checked, and any material unresolved point.

## MP4 and Premiere handoff

Use one canonical plan for both outputs and a fresh version directory. The default requested pair is a viewable MP4 and a single edited Premiere XML sequence. Do not add an uncut comparison sequence by default; if specifically requested, name it unmistakably as an original/reference.

The XML must refer to the **full original source**, not the flattened rendered MP4 or bounded subclips. Preserve source in/out ranges, full-duration handles, video/audio alignment and links, actual frame rate, resolution and source timecode where supported. The user must be able to expand or shorten clip edges. Mark actual cut locations and content/boundary review points. Do not bake technical commentary or markers into the viewed image.

For Windows paths use a round-trip-tested Premiere-compatible local URI (`file://localhost/C%3a/...` with spaces and Unicode encoded). Resolve it back to the actual original media and check existence. Do not let a local drive become `\\C:\...`. If locating media fails, inspect the concrete path and fix the reference; do not rerender identical cuts as a solution to an import-path problem.

For MP4, encode kept source segments once using the source timing, short audio edge fades and PCM intermediates; concatenate with video stream copy and encode final AAC once. A cached segment is reusable only if the source fingerprint, in/out frames and encoding settings match.

Before marking a requested bundle ready, verify:

- Both artifacts were generated from the same canonical plan and source fingerprint; requested MP4 exists and is current, not an older version alongside a revised XML.
- Every kept source frame appears once and in order; rejected intervals are excluded. Revisions preserve previous kept content except explicitly reviewed cuts or evidence-backed edge restorations.
- XML video and audio ranges map to the plan, full source media resolves, and markers are in the edited timeline coordinate system.
- MP4 frame count, duration, A/V timing and full decode pass. Inspect changed seams and known prior failure points in the rendered output as well as the source.
- The manifest identifies actual output filenames, plan/source hashes and which checks passed. An XML-only export is not a complete MP4 delivery. Structural XML validation is not proof of a successful live Premiere import.
- Technical export completion and editorial readiness are separate. Only a current validated QA report can produce `editorial_status: READY` and update a ready-delivery pointer. A draft export may be structurally complete while its review is still required.

Do not say “listened to the whole recording” when only ASR, signal analysis or images were available. Do not promise perfect automatic detection. Report unresolved content specifically rather than presenting it as clean or asking the user to do the entire audit.

## Parallel review brief

When agents are available and authorized, divide independent timeline regions for editorial review and use another reviewer for artifact/frame conservation. Give each reviewer the same source fingerprint and plan version. Have them inspect independent short-crop evidence, not just re-read the same broad transcript. Coordinate GPU jobs centrally rather than launching competing full transcriptions. A reviewer must account for every assigned window before claiming coverage.

Return candidate source intervals, the failed attempt and retained continuation, evidence and uncertainty. Never return a guessed edit just to fill an agent slot. Final integration remains one ordered source keep plan.

## QA report and readiness

Create a separate `qa.json` only from the review actually performed. The validator checks consistency and evidence provenance; it cannot determine whether a human or agent truly understood speech. Do not mass-fill reviewed rows merely because ASR completed.

The current report uses `version: 2`, `source_sha256` and `plan_sha256` (the SHA256 of the **original plan file bytes**, not delivery's canonical JSON hash). Bind `audit_manifest` and `risk_inventory` as `{ "path": "...", "sha256": "..." }`. Paths resolve relative to their containing report or artifact. Version 1 remains readable but always requires review; it cannot earn `READY`.

Generate an immutable, exhaustive risk inventory from the current timeline manifest and its completed ASR with `dialogue_risks.py`. It binds the manifest, original media, raw plan, map, edited PCM, verified decoded source PCM, transcript and detector parameters. It requires both endpoints of every retained clip, short kept clips, long output quiet gaps, independently detected micro-islands and adjacent ASR repeat candidates. These are review obligations, not automatic cuts or an exhaustive semantic detector. The validator regenerates the inventory: deleting difficult rows and recalculating its hash cannot certify the result. An inventory without `--transcript` is preliminary and cannot earn `READY`.

Record these arrays:

- `reviewed_windows`: `{ "id": "<manifest window ID>", "evidence_ids": ["E1"] }` for each window actually reviewed, including empty windows. Referenced output evidence must cover that entire window.
- `reviewed_seams`: `{ "id": "<manifest seam ID>", "output_frame": 0, "left_source_end_frame": 0, "right_source_start_frame": 0, "evidence_ids": ["E1"] }`; use the manifest's actual frame values. Evidence must cover one second on either side of each join, bounded by the timeline endpoints.
- `check_reviews`: `{ "id": "<inventory check ID>", "disposition": "resolved_kept", "reason": "<specific observation>", "evidence_ids": ["E2", "E3"] }`. Account for every inventory ID exactly once; dispositions are `resolved_kept`, `resolved_cut`, `restored` and `unresolved`. Each review's evidence must cover **every** required `evidence_windows` interval with its specified `time_basis` and `role`. For every source edge, independently inspect both the retained side (`kept`) and the original source extending beyond the cut (`extended_context`). One broad ASR view does not stand in for both.
- `candidates`: `{ "id": "F1", "source_frames": [100, 125], "kind": "false_start" }`; retain every editorial suspicion, including previously detected issues that disappeared after revision, unfinished sentences and acoustic events. Example frames are illustrative.
- `findings`: one decision per candidate ID, with `disposition` (`removed`, `retained_with_reason` or `unresolved`), a nonempty `reason` and `evidence_ids`. A removed candidate must actually be outside the keep plan; a retained candidate must be fully inside it. Keep rejected suspicions with their reasons instead of deleting their records.

A check review using `resolved_cut` or `restored` also requires `source_frames: [start, end]`, intersecting that check's source interval. A cut must be absent from the current plan. A restoration must be fully kept and newly added relative to a fingerprinted `previous_plan` reference in the QA report. Restore clipped consonants or word tails when source evidence supports them; do not restrict corrections to further deletions. Rebuild the timeline and inventory after changing the plan, review all new edges and carry earlier decisions into the candidate ledger. No disposition itself authorizes a new cut.

Evidence entries contain `id`, `path`, `sha256`, `kind`, `time_basis`, `ranges`, `source_sha256` and `role`. Output evidence additionally requires the current raw `plan_sha256`. Use numeric second pairs for `ranges`; source and output limits are checked independently. Kinds are `audio`, `waveform`, `asr` and `review`; roles are `kept`, `extended_context`, `candidate`, `timeline` and `render`. A written `review` supports reasoning but never earns acoustic coverage by itself. Do not declare an entire file reviewed when only a small interval was inspected.

```json
{
  "id": "E2",
  "path": "/footage/edit/timeline-audit/source_decoded_16k.wav",
  "sha256": "<actual file digest>",
  "kind": "audio",
  "time_basis": "source",
  "ranges": [[12.0, 12.35]],
  "source_sha256": "<original media digest>",
  "role": "kept"
}
```

Use the actual source-audio path from `risk_inventory.settings.source_audio`, not the illustrative path above. `audio` evidence must reference that verified source PCM, the bound timeline PCM for output, or the actual MP4 for `render`. Only claim audio inspection when supported and actually performed. A waveform evidence file is JSON containing `audio: {path, sha256}`, `time_basis` and `ranges`; its bound audio must be the corresponding verified view and its ranges must contain the claimed evidence scope. Preserve the measurements/plots and observations used in the review alongside this metadata; metadata alone is not an acoustic examination.

ASR evidence requires `window_ids` selecting actual rows from the fingerprinted transcript. The selected IDs and their intervals must match the bound manifest's `window_ids` and `windows`; declared ranges cannot exceed those selected windows. Source ASR must trace to the original media or verified decoded source PCM, and output ASR must bind the current timeline manifest. A `kept` source ASR window must lie entirely in retained material; prepare a separate crop for the extended source context. A complete job or fluent transcript is never proof that a restart or boundary is correct.

For MP4, also supply `render_review` bound to `mp4_sha256`. Its `reviewed_windows` covers the first and last timeline window (deduplicated), and `reviewed_seams` covers every current seam; each row includes `target: "render"` and scoped output evidence with `role: "render"` from the actual MP4. Reuse the same identity/frame fields as above. Do not certify a render using only the internal XML audit WAV.

Missing inventory, incomplete ASR, missing scoped coverage, missing check/candidate decisions or unresolved findings keep `editorial_status` at `REVIEW_REQUIRED`. Invalid identities, changed evidence, stale plan/source or mismatching removal claims are errors. A new plan requires a new report; unchanged source evidence can be linked again after checking that it still applies, but old output offsets and joins cannot be copied blindly. Record any unavailable inspection honestly.

```bash
# Export an XML draft; no READY pointer is updated.
python helpers/dialogue_delivery.py /footage/edit/plan.json -o /footage/edit/delivery-v1 --xml-only

# Export with an already completed timeline review.
python helpers/dialogue_delivery.py /footage/edit/plan.json -o /footage/edit/delivery-v2 --xml-only --qa /footage/edit/qa.json

# After reviewing the actual exported artifacts, finalize without rendering again.
python helpers/dialogue_delivery.py --finalize /footage/edit/delivery-v1 --qa /footage/edit/qa.json
```

Use `export_status` for technical completion and `editorial_status` for readiness. Legacy `status: COMPLETE` alone must never be described as editorial approval. Finalization rechecks the saved plan, source, artifact hashes and XML structure; it does not change the cut or grant new editorial authorization. Prior deliveries without the new QA fields are not retroactively certified.

## Helper commands and plan shape

Run these from the installed repository using its Python environment (see the local installation notes in SKILL.md). `prepare` prints the exact fingerprinted manifest path; `transcribe` prints the transcript path on its last line. Reuse those paths in subsequent commands rather than guessing the cache directory.

```bash
python helpers/dialogue_audit.py prepare --source /footage/take.mp4 --out-dir /footage/edit/audit
python helpers/dialogue_audit.py transcribe --manifest /path/printed/manifest.json --language pl --device cuda
python helpers/dialogue_audit.py events --manifest /path/printed/manifest.json --transcript /path/printed/transcript.json --classify --device cuda
```

After the initial keep plan exists, prepare and review its actual output timeline:

```bash
python helpers/dialogue_audit.py timeline --plan /footage/edit/plan.json --out-dir /footage/edit/timeline-audit
python helpers/dialogue_audit.py transcribe --manifest /path/printed/timeline/manifest.json --language pl --device cuda
python helpers/dialogue_audit.py events --manifest /path/printed/timeline/manifest.json --transcript /path/printed/transcript.json --classify --device cpu
python helpers/dialogue_risks.py --manifest /path/printed/timeline/manifest.json --transcript /path/printed/transcript.json --out /footage/edit/risks-v1.json
```

Use the inventory's exact source evidence windows to prepare independent kept and extended-context crops with `prepare --windows ...`; inspect waveform boundaries for every edge, not only ASR disagreements. Review micro-islands independently even when ASR labels them as words. Then write the QA report from completed observations. For XML-only delivery, full timeline review plus all source-edge/risk obligations is sufficient; no MP4 is required. For render delivery, first create a technical draft, inspect its actual MP4 endpoints and joins, add `render_review`, then finalize that same bundle.

Timeline windows use **output time**; source crops prepared by `prepare` use **source time**. Always use the timeline map to convert a suspected interval, including intervals crossing more than one clip. Use the manifest's window and seam identities in the review report rather than inventing offsets.

Use `--device auto` for faster-whisper auto selection, or CPU when appropriate. On Apple Silicon, `--backend auto` selects mlx-whisper; for AST event classification use a supported torch device such as CPU. `events` without `--classify` still reports acoustic candidates but does not run a sound classifier. The optional classifier dependencies are installed with the `audio-events` extra. Nothing requires API keys or upload of footage.

`prepare --plan plan.json` limits review to the plan's retained ranges. `--windows windows.json` accepts explicit source-second pairs such as `[[43.04,43.92],[44.88,49.44]]`; this is useful for independently checking a failed start and the next attempt. These example numbers illustrate the file format, not universal cut points. The manifest reports uncovered requested ranges, and the ASR result reports completed/empty windows. Preserve that distinction when recording coverage.

The delivery helper accepts a **single-source** plan. Source-relative paths resolve beside the plan. Cut times must be frame-aligned for the probed source; review note times may be approximate. Minimal shape:

```json
{
  "source": "../take.mp4",
  "ranges": [{"start": 1.0, "end": 4.0}, {"start": 5.0, "end": 8.0}],
  "notes": [{"id": "REVIEW_01", "start": 5.0, "end": 5.4, "text": "Check the first retained consonant."}]
}
```

Use real source times, not this illustrative plan. Include `removals` with `id`, `start`, `end`, `reason` and optional `keep_note` for cut markers. If supplied, their union and the keep ranges must account for the whole source. Optional `source_fps`, `source_duration`, `source_sha256`, `total_duration_s`, `output_start` and `output_end` are validated when present. `technical_reviews` are also preserved as markers.

```bash
# Export just the editable sequence for review; does not claim an MP4 exists.
python helpers/dialogue_delivery.py /footage/edit/plan.json -o /footage/edit/review-v1 --xml-only

# After the user's required plan approval, create both artifacts from that plan.
python helpers/dialogue_delivery.py /footage/edit/plan.json -o /footage/edit/delivery-v1 --render --approved
```

The output directory must be new. The bundle contains `plan.json`, `manifest.json`, `Dialogue_Premiere.xml` and, for a successful render, `Dialogue.mp4`. `latest-render.json` and `latest-xml.json` are separate **ready-delivery** pointers in the parent directory; a technical draft does not advance them. Both paths are versioned, so imports/reviews do not silently overwrite earlier material. Rendering cache keys include the source hash, source frame bounds and encoding settings.

The current delivery helper deliberately supports one CFR, progressive SDR, square-pixel video stream plus one mono/stereo audio stream. It rejects unsupported/VFR/HDR/UNC inputs explicitly; do not silently normalize or flatten source handles. Choose an appropriate source-preserving export route for those media or explain the concrete normalization needed. Check `--help` and read the helper before adapting it.
