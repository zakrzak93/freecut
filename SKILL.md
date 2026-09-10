---
name: freecut
description: Edit video by conversation with local transcription. Clean scripted narration by keeping the last complete local takes and checking false starts, breaths and word boundaries. Deliver editable source-based Premiere XML or a rendered MP4 as requested. Also supports creative video edits.
---

# freecut

## Scripted dialogue cleanup

For recorded narration, a talking head following a script, or requests to remove retakes, false starts, silence, coughing or throat clearing, read [references/scripted-dialogue.md](references/scripted-dialogue.md) and the relevant `helpers/` code before editing. This is the default cleanup workflow for these requests. Its content-preservation rules override the generic montage/editor brief below; user instructions override this default.

Use the script only for order and completeness, never to supply inaudible words or cut times. Keep the **last complete attempt in a local retry group**, remove earlier attempts in full, and preserve the original order and unique content. Confirmed defects are removed, not merely marked. Verify that the input is the full source rather than a previous flattened edit. Complete the independent source review, breath review and boundary decisions **before freezing the delivery plan**; follow the staged workflow in the reference. A fluent Whisper transcript, including short crops, can hide retries. When both MP4 and XML are requested, one reviewed keep plan must drive both.

For this installation's user, narration delivery defaults to **XML only, separate editable clips with full source handles, one mono audio track and no markers or content labels**. Keep review notes outside the timeline. Do not render an MP4 unless requested. These are this user's preferences, not universal creative-edit rules; an explicit project instruction overrides them. Preserve the user's manual edits when revising an existing sequence.

The same user's **standard narration-cleanup profile** is aggressive: detect silence candidates longer than 175 ms, preserve speech islands at least 175 ms long, and protect 175 ms before and after retained speech. Remove confirmed non-content breaths and mouth noises even when they are loud; do not keep them merely to make the delivery sound "natural." Never cut a phoneme, quiet consonant, vowel tail or meaningful hesitation. This is the normal workflow, not a separate fast mode. Reuse immutable source analysis, batch editorial decisions into one plan, and perform one complete current-timeline QA after the batch instead of restarting every expensive pass after each small correction. Details and certification rules are in the scripted-dialogue reference.

For narration cleanup, remove genuinely abandoned unfinished sentences even without a later complete take; retain intentional trailing-off and follow project-specific user instructions. Before delivery, audit the actual edited audio with `dialogue_audit.py timeline`, including XML-only work. Review short overlapping windows, resolve conflicting readings with separate source attempts, and check final joins. Generate `dialogue_risks.py` obligations for the full current timeline; independently review every source edge in both kept and extended-source views, plus short clips and micro-islands. Record an explicit evidence-backed resolution for every risk and editorial candidate. Restore clipped speech when justified. A technical export remains a draft until scoped QA v2 passes and `editorial_status` is `READY`; legacy QA v1 cannot certify it. See the scripted-dialogue reference for the report and commands.

## Principle

1. **Reason from source evidence.** Use the raw word transcript and `takes_packed.md` for navigation, then inspect source audio and on-demand visuals. Keep short-crop verification, acoustic event candidates and versioned cut decisions when they support reproducible edits; ASR text alone is not proof of clean delivery.
2. **Audio is primary, visuals follow.** Cut candidates come from speech boundaries and silence gaps. Drill into visuals only at decision points.
3. **Ask → confirm → execute → iterate → persist.** Never touch the cut until the user has confirmed the strategy in plain English.
4. **Generalize.** Do not assume what kind of video this is. Look at the material, ask the user, then edit.
5. **Artistic freedom is the default for creative choices.** Values, presets, fonts, colors and pitch structures below are examples, not a mandate. The Hard Rules and, when applicable, the scripted-dialogue contract and verification requirements still apply. Do not use creative freedom to override content preservation or evidence checks.
6. **Invent freely.** If the material calls for a technique not described here — split-screen, picture-in-picture, lower-third identity cards, reaction cuts, speed ramps, freeze frames, crossfades, match cuts, L-cuts, J-cuts, speed ramps over breath, whatever — build it. The helpers are ffmpeg and PIL. They can do anything the format supports. Do not wait for permission.
7. **Verify your own output before showing it to the user.** If you wouldn't ship it, don't present it.

## Hard Rules (production correctness — non-negotiable)

These are the things where deviation produces silent failures or broken output. They are not taste, they are correctness. Memorize them.

1. **Subtitles are applied LAST in the filter chain**, after every overlay. Otherwise overlays hide captions. Silent failure.
2. **Per-segment extract → lossless `-c copy` concat**, not single-pass filtergraph. Otherwise you double-encode every segment when overlays are added.
3. **30ms audio fades at rendered segment boundaries** (`afade=t=in:st=0:d=0.03,afade=t=out:st={dur-0.03}:d=0.03`). For editable XML, audit the actual timeline behavior; do not add fades only to an audit WAV or create a flattened render to simulate editable clips.
4. **Overlays use `setpts=PTS-STARTPTS+T/TB`** to shift the overlay's frame 0 to its window start. Otherwise you see the middle of the animation during the overlay window.
5. **Master SRT uses output-timeline offsets**: `output_time = word.start - segment_start + segment_offset`. Otherwise captions misalign after segment concat.
6. **Never cut inside a word.** Start from raw word times, then verify the actual word end/onset in the source. ASR can omit words or place times early; the script and a low-energy threshold cannot establish safe boundaries on their own.
7. **Pad every cut edge.** Working window: 30–200ms. ASR timestamps drift 50–100ms — padding absorbs the drift. Tighter for fast-paced, looser for cinematic.
8. **Word-level verbatim ASR only.** Never SRT/phrase mode (loses sub-second gap data). Never normalized fillers (loses editorial signal).
9. **Cache transcripts per source.** Preserve the original transcript. Verification of a suspected omission or restart may use separately cached short source crops, keyed by source fingerprint, interval, model and settings. Do not overwrite the original or repeat an unchanged cached verification run.
10. **Parallel sub-agents for multiple animations.** Never sequential. Spawn N at once via the `Agent` tool; total wall time ≈ slowest one.
11. **Strategy confirmation before execution.** Never touch the cut until the user has approved the plain-English plan.
12. **All session outputs in `<videos_dir>/edit/`.** Never write inside the `freecut/` project directory.

Aesthetic examples are flexible. The scripted-dialogue contract and its evidence/delivery checks remain applicable requirements for that mode.

## Directory layout

The skill lives in `freecut/`. User footage lives wherever they put it. All session outputs go into `<videos_dir>/edit/`.

```
<videos_dir>/
├── <source files, untouched>
└── edit/
    ├── project.md               ← memory; appended every session
    ├── takes_packed.md          ← phrase-level transcripts, the LLM's primary reading view
    ├── edl.json                 ← cut decisions
    ├── transcripts/<name>.json  ← cached normalized ASR JSON
    ├── animations/slot_<id>/    ← per-animation source + render + reasoning
    ├── clips_graded/            ← per-segment extracts with grade + fades
    ├── master.srt               ← output-timeline subtitles
    ├── downloads/               ← yt-dlp outputs
    ├── verify/                  ← debug frames / timeline PNGs
    ├── preview.mp4
    └── final.mp4
```

## Setup

First-time install lives in `install.md` (clone, deps, ffmpeg, skill registration, API key). Don't re-run it every session; on cold start just verify:

- Transcription backend is available. The default is `whisper` (free, local) — verify `mlx-whisper` (Apple Silicon) or `faster-whisper` is importable. If neither is installed, tell the user to `pip install mlx-whisper` (or `faster-whisper`) rather than falling through to a paid backend silently. For the optional `vibevoice` / `elevenlabs` backends, check `VIBEVOICE_ASR_URL` / `ELEVENLABS_API_KEY` in the environment or `.env` at the freecut repo root.
- `ffmpeg` + `ffprobe` on PATH.
- Python deps installed (`uv sync` or `pip install -e .` inside the repo).
- Node.js + npm available if the session needs HyperFrames or Remotion slots. HyperFrames currently requires Node.js 22+.
- `yt-dlp`, HyperFrames, Remotion, Manim installed only on first use.
- First-use animation setup happens inside the slot directory, never at the freecut repo root. HyperFrames can be invoked with `npx --yes hyperframes ...`; Remotion can be scaffolded with `npx create-video@latest` or installed as a project-local dependency before using its `remotion render` command.
- This skill vendors `skills/manim-video/`. Read its SKILL.md when building a Manim slot.

Helpers (`helpers/transcribe.py`, `helpers/render.py`, etc.) live alongside this SKILL.md. Resolve their paths relative to the directory containing this file — the skill is typically symlinked at `~/.claude/skills/freecut/` or `~/.codex/skills/freecut/`.

## Helpers

- **`transcribe.py <video>`** — single-file transcription. Pluggable backend via `--backend {whisper,vibevoice,elevenlabs}` (default `whisper`, local + free, single speaker). `vibevoice` uses `VIBEVOICE_ASR_URL` for real diarization; `elevenlabs` uses `ELEVENLABS_API_KEY` and supports `--num-speakers`. Cached.
- **`transcribe_batch.py <videos_dir>`** — 4-worker parallel transcription. Same `--backend` flag. Use for multi-take.
- **`pack_transcripts.py --edit-dir <dir>`** — `transcripts/*.json` → `takes_packed.md` (phrase-level, break on silence ≥ 0.5s or speaker change). Works identically across backends because the JSON shape is normalized.
- **`timeline_view.py <video> <start> <end>`** — filmstrip + waveform PNG. On-demand visual drill-down. **Not a scan tool** — use it at decision points, not constantly.
- **`render.py <edl.json> -o <out>`** — per-segment extract → concat → overlays (PTS-shifted) → subtitles LAST. `--preview` for 720p fast. `--build-subtitles` to generate master.srt inline.
- **`grade.py <in> -o <out>`** — ffmpeg filter chain grade. Presets + `--filter '<raw>'` for custom.
- **`dialogue_audit.py`** — prepare acoustic islands, independently transcribe short source windows, and optionally classify non-speech sound candidates locally. Produces evidence for review, not automatic edit decisions. See the scripted-dialogue reference and `--help`.
- **`dialogue_risks.py --manifest ... --transcript ... --out ...`** — immutable exhaustive detector inventory bound to the current timeline, source PCM and completed ASR; every generated check requires a scoped decision. No automatic cuts.
- **`dialogue_qa.py`** — delivery-invoked QA v2 validator for current hashes, complete window/seam/risk reviews and scoped evidence; written reasons alone cannot prove acoustic coverage.
- **`dialogue_delivery.py`** — validate a single-source keep plan and deliver a versioned, matching MP4/Premiere XML bundle with full original-media handles. Use this route for dialogue handoff instead of case-specific export scripts. Use `--qa` for review validation and `--finalize DIR --qa REPORT` after reviewing a technical draft; only `READY` advances latest pointers. See `--help` for approval and XML-only options.

For animations, create `<edit>/animations/slot_<id>/` with `Bash` and spawn a sub-agent via the `Agent` tool.

## The process

For scripted dialogue, follow `references/scripted-dialogue.md` and use `dialogue_audit.py` / `dialogue_delivery.py` for the audit and handoff. The generic `render.py` steps below apply to creative montage, grading and overlays, not as a replacement for the dialogue checks.

1. **Inventory.** `ffprobe` every source. `transcribe_batch.py` on the directory. `pack_transcripts.py` to produce `takes_packed.md`. Sample one or two `timeline_view`s for a visual first impression.
2. **Pre-scan for problems.** One pass over `takes_packed.md` to note verbal slips, obvious mis-speaks, or phrasings to avoid. Plain list, feed into the editor brief.
3. **Converse.** Describe what you see in plain English. Ask questions *shaped by the material*. Collect: content type, target length/aspect, aesthetic/brand direction, pacing feel, must-preserve moments, must-cut moments, animation and grade preferences, subtitle needs. Do not use a fixed checklist — the right questions are different every time.
4. **Propose strategy.** 4–8 sentences: shape, take choices, cut direction, animation plan, grade direction, subtitle style, length estimate. **Wait for confirmation.**
5. **Execute.** Produce `edl.json` via the editor sub-agent brief. Drill into `timeline_view` at ambiguous moments. Build animations in parallel sub-agents. Apply grade per-segment. Compose via `render.py`.
6. **Preview.** `render.py --preview`.
7. **Self-eval (before showing the user).** Run `timeline_view` on the **rendered output** (not the sources) at every cut boundary (±1.5s window). Check each image for:
   - Visual discontinuity / flash / jump at the cut
   - Waveform spike at the boundary (audio pop that slipped past the 30ms fade)
   - Subtitle hidden behind an overlay (Rule 1 violation)
   - Overlay misaligned or showing wrong frames (Rule 4 violation)

   Also sample: first 2s, last 2s, and 2–3 mid-points — check grade consistency, subtitle readability, overall coherence. Run `ffprobe` on the output to verify duration matches the EDL expectation.

   If anything fails: fix → re-render → re-eval. **Cap at 3 self-eval passes** — if issues remain after 3, flag them to the user rather than looping forever. Only present the preview once the self-eval passes.
8. **Iterate + persist.** Natural-language feedback, re-plan, re-render. Reuse original transcripts and keyed verification caches; investigate new evidence with separate short crops when needed. Respect existing approval scope and any requested plan-before-render checkpoint. Append to `project.md` and identify which artifact version is current.

## Cut craft (techniques)

- **Audio-first.** Candidate cuts from word boundaries and silence gaps.
- **Preserve peaks.** Laughs, punchlines, emphasis beats. Extend past punchlines to include reactions — the laugh IS the beat.
- **Speaker handoffs** benefit from air between utterances. Common values: 400–600ms. Less for fast-paced, more for cinematic. Taste call.
- **Audio events as signals.** `(laughs)`, `(sighs)`, `(applause)` mark beats. Extend past them.
- **Silence gaps are cut candidates.** Silences ≥400ms are usually the cleanest. 150–400ms phrase boundaries are usable with a visual check. <150ms is unsafe (mid-phrase).
- **Example cut padding** (the launch video shipped with this): 50ms before the first kept word, 80ms after the last. Tighter for montage energy, looser for documentary. Stay in the 30–200ms working window (Hard Rule 7).
- **Never reason audio and video independently.** Every cut must work on both tracks.

## The packed transcript (primary reading view)

`pack_transcripts.py` reads all `transcripts/*.json` and produces phrase-level lines prefixed with `[start-end]`. Phrases break on ASR gaps ≥ 0.5s or speaker changes. This is a compact navigation view, not an acoustic silence detector or a source of word-boundary precision. Read the raw word records and verify the source before deciding cuts.

Example line:
```
## C0103  (duration: 43.0s, 8 phrases)
  [002.52-005.36] S0 Ninety percent of what a web agent does is completely wasted.
  [006.08-006.74] S0 We fixed this.
```

## Editor sub-agent brief (for multi-take selection)

This creative selection brief is for montages or rearrangement that the user requested. For scripted dialogue cleanup, use the brief in `references/scripted-dialogue.md`; do not reorder sentences, drop beats for runtime, or choose an earlier preferred delivery over the last complete local attempt.

When the task is "pick the best take of each beat across many clips," spawn a dedicated sub-agent with a brief shaped like this. The structure is load-bearing; the pitch-shape example is not.

```
You are editing a <type> video. Pick the best take of each beat and 
assemble them chronologically by beat, not by source clip order.

INPUTS:
  - takes_packed.md (time-annotated phrase-level transcripts of all takes)
  - Product/narrative context: <2 sentences from the user>
  - Speaker(s): <name, role, delivery style note>
  - Expected structure: <pick an archetype or invent one>
  - Verbal slips to avoid: <list from the pre-scan pass>
  - Target runtime: <seconds>

Common structural archetypes (pick, adapt, or invent):
  - Tech launch / demo:   HOOK → PROBLEM → SOLUTION → BENEFIT → EXAMPLE → CTA
  - Tutorial:             INTRO → SETUP → STEPS → GOTCHAS → RECAP
  - Interview:            (QUESTION → ANSWER → FOLLOWUP) repeat
  - Travel / event:       ARRIVAL → HIGHLIGHTS → QUIET MOMENTS → DEPARTURE
  - Documentary:          THESIS → EVIDENCE → COUNTERPOINT → CONCLUSION
  - Music / performance:  INTRO → VERSE → CHORUS → BRIDGE → OUTRO
  - Or invent your own.

RULES:
  - Start/end times must fall on word boundaries from the transcript.
  - Pad cut boundaries (working window 30–200ms).
  - Prefer silences ≥ 400ms as cut targets.
  - Unavoidable slips are kept if no better take exists. Note them in "reason".
  - If over budget, revise: drop a beat or trim tails. Report total and self-correct.

OUTPUT (JSON array, no prose):
  [{"source": "C0103", "start": 2.42, "end": 6.85, "beat": "HOOK",
    "quote": "...", "reason": "..."}, ...]

Return the final EDL and a one-line total runtime check.
```

## Color grade (when requested)

Your job is to **reason about the image**, not apply a preset. Look at a frame (via `timeline_view`), decide what's wrong, adjust one thing, look again.

Mental model is ASC CDL. Per channel: `out = (in * slope + offset) ** power`, then global saturation. `slope` → highlights, `offset` → shadows, `power` → midtones.

**Example filter chains** (`grade.py` has `--list-presets`; use them as starting points or mix your own):

- **`warm_cinematic`** — retro/technical, subtle teal/orange split, desaturated. Shipped in a real launch video. Safe for talking heads.
- **`neutral_punch`** — minimal corrective: contrast bump + gentle S-curve. No hue shifts.
- **`none`** — straight copy. Default when the user hasn't asked.

For anything else — portraiture, nature, product, music video, documentary — invent your own chain. `grade.py --filter '<raw ffmpeg>'` accepts any filter string.

Hard rules: apply **per-segment during extraction** (not post-concat, which re-encodes twice). Never go aggressive without testing skin tones.

## Subtitles (when requested)

Subtitles have three dimensions worth reasoning about: **chunking** (1/2/3/sentence per line), **case** (UPPER/Title/Natural), and **placement** (margin from bottom). The right combo depends on content.

**Worked styles** — pick, adapt, or invent:

**`bold-overlay`** — short-form tech launch, fast-paced social. 2-word chunks, UPPERCASE, break on punctuation, Helvetica 18 Bold, white-on-outline, `MarginV=35`. `render.py` ships with this as `SUB_FORCE_STYLE`.

```
FontName=Helvetica,FontSize=18,Bold=1,
PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BackColour=&H00000000,
BorderStyle=1,Outline=2,Shadow=0,
Alignment=2,MarginV=35
```

**`natural-sentence`** (if you invent this mode) — narrative, documentary, education. 4–7 word chunks, sentence case, break on natural pauses, `MarginV=60–80`, larger font for readability, slightly wider max-width. No shipped force_style — design one if you need it.

Invent a third style if neither fits. Hard rules: subtitles LAST (Rule 1), output-timeline offsets (Rule 5).

## Animations (when requested)

Animations match the content and the brand. **Get the palette, font, and visual language from the conversation** — never assume a default. If the user hasn't told you, propose a palette in the strategy phase and wait for confirmation before building anything.

**Tool options:**

Pick the engine per animation slot. Do not default to Remotion just because the animation is web-adjacent.

- **HyperFrames** — Browser-native HTML/CSS/GSAP video compositions: product UI motion, website-to-video or mockup-to-video captures, kinetic typography, landing-page/storyboard promos, data-driven UI states, transparent WebM overlays, and clips that need deterministic frame capture plus HyperFrames lint/validate/render checks. Best when the animation should be authored and verified like a web composition instead of a React component tree.
- **Remotion** — React/CSS compositions with component state, reusable React primitives, or an existing Remotion brand system. Best when the user specifically asks for React/Remotion or when React composition is the simpler authoring model.
- **Manim** — formal diagrams, state machines, equation derivations, graph morphs. Read `skills/manim-video/SKILL.md` and its references for depth.
- **PIL + PNG sequence + ffmpeg** — simple overlay cards: counters, typewriter text, single bar reveals, progressive draws. Fast to iterate, any aesthetic you want. The launch video used this.

For HyperFrames slots, scaffold the slot inside `edit/animations/slot_<id>/` with `npx --yes hyperframes init . --example blank --non-interactive --skip-skills`, build the HTML composition there, run the HyperFrames checks that fit the slot (`lint`, `validate`, and a draft render when practical), then produce the final overlay video with `npx --yes hyperframes render . -o render.mp4` or `--format webm -o render.webm` when alpha is required. Point the EDL overlay `file` at the actual rendered path.

For Remotion slots, keep the Remotion project isolated inside the same slot directory, scaffold with `npx create-video@latest` or install Remotion locally there, render the composition to `render.mp4` with the project-local `remotion render` command, and verify duration and dimensions with `ffprobe`.

None is mandatory. Invent hybrids if useful (e.g., PIL background with a HyperFrames or Remotion layer on top).

**Duration rules of thumb, context-dependent:**

- **Sync-to-narration explanations.** A viewer needs to parse the content at 1×. Rough floor 3s, typical 5–7s for simple cards, 8–14s for complex diagrams. The launch video shipped at 5–7s per simple card.
- **Beat-synced accents** (music video, fast montage). 0.5–2s is fine — they're visual accents, not information. The "readable at 1×" rule becomes *"recognizable at 1×"*, not *"fully parseable."*
- **Hold the final frame ≥ 1s** before the cut (universal).
- **Over voiceover:** total duration ≥ `narration_length + 1s` (universal).
- **Never parallel-reveal independent elements** — the eye can't track two new things at once. One thing, pause, next thing.

**Animation payoff timing (rule for sync-to-narration):** get the payoff word's timestamp. Start the overlay `reveal_duration` seconds earlier so the landing frame coincides with the spoken payoff word. Without this sync the animation feels disconnected.

**Easing** (universal — never `linear`, it looks robotic):

```python
def ease_out_cubic(t):    return 1 - (1 - t) ** 3
def ease_in_out_cubic(t):
    if t < 0.5: return 4 * t ** 3
    return 1 - (-2 * t + 2) ** 3 / 2
```

`ease_out_cubic` for single reveals (slow landing). `ease_in_out_cubic` for continuous draws.

**Typing text anchor trick:** center on the FULL string's width, not the partial-string width — otherwise text slides left during reveal.

**Example palette** (the launch video — one aesthetic among infinite):
- Background `(10, 10, 10)` near-black
- Accent `#FF5A00` / `(255, 90, 0)` orange
- Labels `(110, 110, 110)` dim gray
- Font: Menlo Bold at `/System/Library/Fonts/Menlo.ttc` (index 1)
- ≤ 2 accent colors, ~40% empty space, minimal chrome
- Result: terminal / retro tech feel

This is one style. If the brand is warm and serif, use that. If it's colorful and playful, use that. If the user handed you a style guide, follow it. If they didn't, propose one and confirm.

**Parallel sub-agent brief** — each animation is one sub-agent spawned via the `Agent` tool. Each prompt is self-contained (sub-agents have no parent context). Include:

1. One-sentence goal: *"Build ONE animation: [spec]. Nothing else."*
2. Absolute output path (`<edit>/animations/slot_<id>/render.mp4`)
3. Exact technical spec: resolution, fps, codec, pix_fmt, CRF, duration
4. Style palette as concrete values (RGB tuples, hex, or reference to a design system)
5. Font path with index
6. Frame-by-frame timeline (what happens when, with easing)
7. Anti-list ("no chrome, no extras, no titles unless specified")
8. Code pattern reference (copy helpers inline, don't import across slots)
9. Deliverable checklist (script, render, verify duration via ffprobe, report)
10. **"Do not ask questions. If anything is ambiguous, pick the most obvious interpretation and proceed."**

One sub-agent = one file (unique filenames, parallel agents don't overwrite each other).

## Output spec

Match the source unless the user asked for something specific. Common targets: `1920×1080@24` cinematic, `1920×1080@30` screen content, `1080×1920@30` vertical social, `3840×2160@24` 4K cinema, `1080×1080@30` square. `render.py` defaults the scale to 1080p from any source; pass `--filter` or edit the extract command for other targets. Worth asking the user which delivery format matters.

## EDL format

```json
{
  "version": 1,
  "sources": {"C0103": "/abs/path/C0103.MP4", "C0108": "/abs/path/C0108.MP4"},
  "ranges": [
    {"source": "C0103", "start": 2.42, "end": 6.85,
     "beat": "HOOK", "quote": "...", "reason": "Cleanest delivery, stops before slip at 38.46."},
    {"source": "C0108", "start": 14.30, "end": 28.90,
     "beat": "SOLUTION", "quote": "...", "reason": "Only take without the false start."}
  ],
  "grade": "warm_cinematic",
  "overlays": [
    {"file": "edit/animations/slot_1/render.mp4", "start_in_output": 0.0, "duration": 5.0}
  ],
  "subtitles": "edit/master.srt",
  "total_duration_s": 87.4
}
```

`grade` is a preset name or raw ffmpeg filter. `overlays` are rendered animation clips. `subtitles` is optional and applied LAST.

## Memory — `project.md`

Append one section per session at `<edit>/project.md`:

```markdown
## Session N — YYYY-MM-DD

**Strategy:** one paragraph describing the approach
**Decisions:** take choices, cuts, grades, animations + why
**Reasoning log:** one-line rationale for non-obvious decisions
**Outstanding:** deferred items
```

On startup, read `project.md` if it exists and summarize the last session in one sentence before asking whether to continue.

## Anti-patterns

Things that consistently fail regardless of style:

- **Hierarchical pre-computed codec formats** with USABILITY / tone tags / shot layers. Over-engineering. Derive from the transcript at decision time.
- **Hand-tuned moment-scoring functions.** The LLM picks better than any heuristic you'll write.
- **Whisper SRT / phrase-level output.** Loses sub-second gap data. Always word-level verbatim.
- **Slow CPU-only Whisper on a big machine.** Use `mlx-whisper` on Apple Silicon, or `faster-whisper` with GPU/CoreML acceleration where available. Never fall back to a paid backend silently just because local is slow — tell the user.
- **Burning subtitles into base before compositing overlays.** Overlays hide them. (Hard Rule 1.)
- **Single-pass filtergraph when you have overlays.** Double re-encodes. Use per-segment extract → concat.
- **Linear animation easing.** Looks robotic. Always cubic.
- **Hard audio cuts at segment boundaries.** Audible pops. (Hard Rule 3.)
- **Typing text centered on the partial string.** Text slides left as it grows.
- **Sequential sub-agents for multiple animations.** Always parallel.
- **Editing before confirming the strategy.** Never.
- **Replacing a cached transcript to hide disagreements.** Preserve it; store source-crop verification separately with provenance.
- **Treating fluent Whisper text as proof of no retakes, or empty ASR as silence.** Inspect short attempts and non-speech sound candidates.
- **Calling a marker a removal, or shipping an old MP4 with a new XML.** Verify the actual keep ranges and the matching delivery manifest.
- **Assuming what kind of video it is.** Look first, ask second, edit last.

## Local Codex installation (Windows)

- Repository: `C:/Users/zakrz/Documents/Codex/freecut`; registered as `C:/Users/zakrz/.codex/skills/freecut` via a directory junction.
- Run helpers with `C:/Users/zakrz/Documents/Codex/freecut/.venv/Scripts/python.exe` so the installed dependencies and local faster-whisper backend are used. Resolve helper paths relative to this SKILL.md and read the relevant helper code before editing.
- To update dependencies, use `uv sync --extra whisper-fast` in the repository (keep the local Whisper extra enabled).
- FFmpeg and ffprobe are available on PATH. No API keys are needed. Model weights download on the first requested transcription.
- Setup alone does not authorize transcription. Wait for the user to supply a footage folder and request work.
- Use Python's `-X utf8` option when invoking helpers on Windows to preserve Unicode transcripts and console output. For rendering, use forward-slash paths and check FFmpeg filter escaping for drive-letter paths (especially subtitle files and grade-analysis temporary files).
- For GPU transcription in this installation, prepend these existing CUDA library directories to the helper process PATH: `C:/Users/zakrz/AppData/Local/Programs/Python/Python312/Lib/site-packages/nvidia/cublas/bin` and `C:/Users/zakrz/AppData/Local/Programs/Python/Python312/Lib/site-packages/nvidia/cudnn/bin`. An import-only check does not verify these inference-time DLLs.
- The optional `dialogue_audit.py events --classify` route has also been tested with the existing `C:/Users/zakrz/AppData/Local/Programs/Python/Python312/python.exe` runtime (PyTorch/Transformers and CUDA). It can read manifests and transcripts prepared by the repository environment, so reuse it for sound-event classification rather than reinstalling a second heavy stack unnecessarily. Continue using the repository environment for `prepare`, Whisper transcription and delivery.
