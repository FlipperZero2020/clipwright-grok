# Clipwright — Talk Your Clips Into Existence

*A $0-per-render, conversation-driven foundry for custom videos and GIFs.*

## The one-paragraph vision

Today, making a clip here means `generate_video.py`: type a prompt, spend Veo credits, wait
minutes, get one MP4, no GIFs, no editing, and iteration is rationed by a prepaid balance.
Clipwright flips every one of those. You describe a clip in plain English inside Claude Code
("looping gif of the cat clip, caption it 'me when the build passes', keep it under Discord's
size cap"), an agent skill writes a tiny **recipe file**, and a deterministic local renderer —
ffmpeg + Pillow, all already installed — turns it into an MP4 *and* a palette-optimized GIF in
seconds. Every render costs $0, so iteration becomes gleeful instead of rationed: reroll a 3×3
grid of variants, nudge one thing, export when it makes you grin. And every file Clipwright
exports carries its own recipe inside it, so any GIF you ever made can be reopened and remixed
months later.

## Why this shape (and not something else)

Three product concepts were pitted against each other and judged on ease, real-world $0
feasibility, and delight. No single one won all three, but they compose cleanly into layers:

| Layer | Stolen from | What it contributes |
|---|---|---|
| **Recipes + ffmpeg engine** | "Clipbook" concept | The feasible core: ~15-line declarative files compiled to ffmpeg/Pillow pipelines. Shippable in a day, zero cloud surface. |
| **Conversation as the interface** | "Clipwright" concept | The ease win: you already run Claude Code all day. A `/clip` skill authors and edits recipes from plain English — no flags to memorize, no new UI to learn. |
| **Procedural fun + rerolls** | "Loopsmith" concept | The delight win: seeded generative scenes (kinetic typography, emoji physics, gradient loops) and contact-sheet variant grids, so Clipwright creates from *nothing*, not just remixes. |

A fourth concept — a single-file in-browser studio using ffmpeg.wasm — was set aside: `file://`
restrictions on ES modules/workers and SharedArrayBuffer requirements make "double-click an HTML
file" quietly hard, and it scored last on feasibility. Direct-manipulation ideas from it are
kept as a later Workbench milestone instead.

## Design principles

1. **$0 per render, by construction.** No generation API anywhere in the pipeline. The "brain"
   is the Claude Code subscription already being paid for; the "hands" are ffmpeg, ffprobe,
   Pillow, and gifsicle — all verified already installed on this machine (ffmpeg 6.1.1,
   Python 3.12, Noto Color Emoji present).
2. **Recipes are the format; prompts are sugar.** Every clip is a small TOML file in git.
   Conversation, CLI, and any future UI are all just ways of writing the same file.
   "Make the text bigger" is a one-line reviewable diff.
3. **Outputs carry their source.** The recipe + arguments get embedded in the exported file's
   metadata *and* written as a sidecar `.recipe.toml` next to it (chat apps strip metadata on
   upload, so the sidecar is the reliable copy). `clipwright remix old.gif --text "new caption"`
   reopens anything.
4. **GIF is a first-class citizen, not an afterthought.** Two-pass palettegen/paletteuse,
   perfect-loop finding, boomerang/ping-pong modes, and platform size budgets built in.
5. **The CLI is the complete product.** The skill and any UI are strictly additive, so nothing
   breaks when working offline or outside Claude Code.

## Feature set

### Core engine
- `clipwright cook <recipe> <input> [--text ... --from ... --to ...]` → emits GIF + MP4 with a
  size report, and prints the exact command it ran so every render is reproducible.
- Starter cookbook (~8 recipes): reaction-GIF cutter, top/bottom Impact-style caption card
  (Pillow-rendered — far better typography than ffmpeg drawtext), boomerang, speed ramp,
  before/after wipe, subtitle burn from `.srt`, Ken Burns pan over a photo, freeze-frame ending.
- **Generative recipes** (no input footage needed): kinetic typography, emoji-physics particle
  scenes (rain/bounce/orbit using Noto Color Emoji as a free sprite library), seamless gradient
  loops using phase-aligned periodic noise so every loop closes perfectly.
- Each recipe compiles to a *fixed, known* ffmpeg pipeline per recipe type — not a general
  filtergraph composer (a known quoting/escaping minefield; see Risks).

### GIF intelligence
- **Size-budget solver**: recipes declare `fits = "discord"` (or `slack`, `whatsapp-sticker`,
  `shorts-9x16`, or a raw MB number); the engine auto-searches fps/scale/color-count at proxy
  resolution and only does the final encode at full quality, with an optional
  `gifsicle -O3 --lossy` squeeze pass.
- **Perfect-loop finder**: frame-similarity scan near the cut points nudges the out-point to
  the best-matching frame; when footage has no near-repeating frames it degrades *explicitly*
  to boomerang or crossfade rather than shipping a visibly seamed "seamless" loop.

### The conversational layer (`/clip` skill)
- A Claude Code skill that knows the recipe schema and the cookbook. You say what you want;
  it picks or instantiates a recipe, runs a fast low-res proxy render, and iterates on your
  notes ("bigger text", "slow the zoom", "loop it twice") as tiny recipe diffs.
- Auto-indexed asset library: `assets.toml` built by ffprobe over the repo's existing MP4s and
  photos (with thumbnails), so "use the paper airplane clip" resolves by name.
- **Template distillation**: after several similar clips, the skill proposes saving a
  parameterized template with `{{slots}}` — the cookbook grows itself.

### The reroll loop
- `--grid 9`: nine seeded 240p variants (palette / motion / font / loop-point differing) in
  seconds; pick by eye instead of re-describing blind, then mutate a single "gene".

### Later: the Workbench
- A one-file Flask page: drag-and-drop, frame scrubber for in/out points, live proxy preview,
  click-to-pick variant grid, and a panel showing the equivalent CLI command for every action
  (the UI teaches the CLI). Strictly optional polish — the scope-creep magnet lives here, so
  it comes last.

## What a session looks like

```
you>  /clip looping gif of paper_airplane.mp4 but caption it "my sprint velocity",
      keep it discord-sized

clip> Using recipe caption-loop on assets/paper_airplane.mp4 (found in assets.toml).
      Proxy render: clips/sprint-velocity/proxy.gif (2.1s, loop point auto-nudged +4 frames)
      → open it. Want changes, or export?

you>  caption in yellow, and show me a grid of loop points

clip> 9 variants → clips/sprint-velocity/grid.png — pick one.

you>  #6, export

clip> Done: sprint-velocity.gif (1.8 MB, fits discord) + sprint-velocity.mp4
      Recipe saved + embedded. Re-run anytime:
      clipwright cook caption-loop assets/paper_airplane.mp4 --text "my sprint velocity" \
        --color ffdd00 --seed 6 --fits discord
```

## A recipe file, concretely

```toml
# clips/sprint-velocity/recipe.toml
recipe  = "caption-loop"
input   = "assets/paper_airplane.mp4"   # referenced by path + content hash for re-linking
from    = "0:02.0"
to      = "0:04.5"                      # loop finder may nudge this ±10 frames
loop    = "seamless"                    # seamless | boomerang | crossfade | none
fits    = "discord"

[caption]
text  = "my sprint velocity"
style = "impact-outline"
color = "#ffdd00"
pos   = "bottom"
```

## Milestones

1. **Day 1 — cookable core.** `clipwright cook` CLI (Typer + stdlib `tomllib`), three recipes:
   gifify with two-pass palette, Pillow caption card, boomerang. GIF + MP4 out, size report,
   sidecar recipe written. *This alone already beats the current workflow.*
2. **Weekend 1 — GIF intelligence + assets.** Perfect-loop finder, size-budget solver with
   platform presets, gifsicle squeeze, `assets.toml` auto-indexer, `remix` command reading
   sidecars/metadata.
3. **Weekend 2 — the `/clip` skill.** Skill file defining the schema, cook/iterate/export
   protocol, proxy-render habit, and asset-library lookup. Grid rerolls (`--grid 9`).
4. **Weekend 3 — generative recipes.** Kinetic typography, gradient loops with phase-aligned
   noise, first emoji-physics behavior (rain). Seeded and mutable.
5. **Later — Workbench UI, template distillation, breeding** (cross two recipes' genes into an
   offspring grid), fun-filter shelf (deep-fry, VHS), batch renders (one recipe × ten labels).

## Risks the judges flagged (and the mitigations baked in above)

- **GIF metadata is unreliable** — ffmpeg's GIF muxer won't write comments cleanly, and chat
  apps strip metadata on upload → sidecar `.recipe.toml` is the canonical copy; embedding is
  best-effort via `gifsicle --comment`.
- **General filtergraph composition is a quoting minefield** → fixed pipeline per recipe type.
- **Size-solver re-encodes could take minutes** → search at proxy resolution, final encode once.
- **Loop finder can't find loops in non-repeating footage** → explicit degrade, never a seamed loop.
- **Agent round-trips are slow for fine positioning** → proxy renders + variant grids replace
  "nudge it left" ping-pong; the printed CLI command is the always-available escape hatch.
- **Noto Color Emoji is a bitmap font Pillow handles awkwardly** → treat emoji-physics as a
  weekend-3 feature with a sprite-extraction spike first, not a day-1 dependency.
- **Environment checks worth doing in milestone 1**: ffmpeg built with libass (for subtitle
  burn), font paths for Pillow. Five-minute checks that prevent day-one confusion.

## Explicitly out of scope

- Anything that calls a paid generation API — `generate_video.py` can stay in the repo as a
  separate, optional tool, but no Clipwright feature may depend on it.
- Cloud anything: no accounts, no telemetry, no uploads. Works offline forever.
