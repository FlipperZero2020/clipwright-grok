# Clipwright — Tap Your Clips Into Existence

*A $0-per-render, button-driven foundry for custom videos and GIFs, living inside Telegram.*

> **v2 — Telegram edition.** v1 (preserved as
> [CLIPWRIGHT_PLAN_v1_claude-code-skill.md](CLIPWRIGHT_PLAN_v1_claude-code-skill.md))
> made Claude Code the interface: you'd describe a clip in English and a `/clip` skill would
> write the recipe. The new goal is a Telegram bot — **@The_Ben_Chod_Dev_Bot** — as the front
> door, with **no LLM anywhere in the loop**. That single change is more clarifying than it
> sounds, and most of this document is the consequence.

## The one-paragraph vision

Today, making a clip here means `generate_video.py`: type a prompt, spend Veo credits, wait
minutes, get one MP4, no GIFs, no editing, and iteration is rationed by a prepaid balance.
Clipwright flips every one of those. You send a video to a Telegram bot from your phone — or
send nothing at all and pick a generative recipe — and a deterministic local renderer
(ffmpeg + Pillow, all already installed on this VM) hands back a looping GIF in seconds, with
a row of buttons under it: bigger, smaller, yellow, boomerang, reroll, export. Every tap is a
one-field edit to a small **recipe file** and a fresh proxy render that *replaces the previous
one in place*, so the chat stays a single morphing preview instead of a wall of near-misses.
Every render costs $0 and touches no API, so iteration becomes gleeful instead of rationed.
And every file the bot exports carries its recipe next to it, so any GIF you made months ago
can be replied to with `/remix` and reopened exactly where you left it.

## What changed from v1, and why

| v1 assumed | v2 does | Why |
|---|---|---|
| Claude Code skill is the interface | Telegram bot is the interface | You want this from your phone, and from friends' phones. Claude Code isn't there. |
| An LLM turns English into recipe fields | **Buttons and commands** turn taps into recipe fields | Chosen deliberately. Keeps the *entire* system free and offline, removes the slowest and least predictable component, and — see below — the thing that made NL feel necessary was already solved by variant grids. |
| Single user, implicitly trusted | Owner + a friends allowlist | Changes the threat model: uploads are now semi-trusted input, and renders need a queue so one person can't peg the box. |
| Workbench UI as a "later" milestone | **Cut entirely** | Telegram already is the workbench: drag-and-drop, previews, buttons, a scrubber built from callbacks. Building a Flask page would be rebuilding, worse, what the bot gives free. |
| `/clip` Claude Code skill | Dropped | The bot is the front door. The CLI remains the complete product underneath it. |

Unchanged: the recipe format, the fixed-pipeline-per-recipe engine design, the $0 constraint,
GIF-as-first-class, and every mitigation the v1 judges flagged. The engine is the part of v1
that was right, and it survives intact.

## Why buttons are enough (the load-bearing argument)

Dropping the LLM sounds like dropping the ease win. It isn't, for a specific reason: **v1
already concluded that natural language was the wrong tool for the fine-tuning loop.** Read
v1's own risk list — *"agent round-trips are slow for fine positioning → proxy renders +
variant grids replace 'nudge it left' ping-pong."* The plan had already decided that the
actual iteration mechanic would be **look at a grid and point at one**. The LLM was left doing
only the first step: turning one opening sentence into a starting recipe.

Telegram does that first step better than prose does, because the option space is small and
enumerable. There are eight recipes, not eight thousand. A menu of eight named buttons with
example thumbnails is *faster and more discoverable* than typing a sentence and hoping the
parse lands. So:

- **Starting a clip** — a menu, not a sentence. `/new` → pick a recipe from a keyboard.
- **Refining a clip** — buttons that mutate one field, and a 3×3 grid to pick from when the
  field is aesthetic rather than numeric. Exactly the mechanic v1 wanted.
- **Saying something the buttons can't express** — the printed CLI command is the escape
  hatch, available under every preview via `[⌘ Show CLI]`.

What is genuinely lost: you can't say *"make it feel more frantic"* and have three fields move
at once. That's a real loss, and the honest mitigation is that `[🎲 Reroll]` explores that same
space by accident, faster than you could have typed the sentence.

## Design principles

1. **$0 per render, by construction.** No generation API, and now no inference API either.
   The pipeline is ffmpeg 6.1.1, ffprobe, Pillow 12.3.0 and gifsicle — all verified present on
   this machine. The only network traffic Clipwright ever makes is to `api.telegram.org`.
2. **Recipes are the format; buttons are sugar.** Every clip is a small TOML file. Telegram,
   the CLI, and anything later are just ways of writing the same file. A button press is a
   one-line reviewable diff.
3. **Outputs carry their source.** Every export writes a sidecar `.recipe.toml` beside the
   file *and* logs the sent file's `file_unique_id` to a local ledger. Reply to any GIF the
   bot ever sent with `/remix` and it reopens, fully editable. This works even though Telegram
   strips and re-encodes everything, because the ledger — not the file — is the memory.
4. **GIF is a first-class citizen.** Two-pass palettegen/paletteuse, perfect-loop finding,
   boomerang, and platform size budgets — now including a `telegram` preset.
5. **The CLI is the complete product.** `clipwrightd` is a client of the engine, not the other
   way round. Everything works over SSH with the bot switched off.
6. **No dead ends.** *(New, and the one that replaces the LLM.)* Every message the bot sends
   carries the buttons for its legal next moves. You should never need to know a command to
   get to the next step — commands are the accelerator, buttons are the floor. Discoverability
   is a hard requirement, not a nicety, precisely because nothing is there to interpret a
   guess.

## The interface: knob specs compile to keyboards

The bot must never contain a hand-written keyboard for a specific recipe — that's how you end
up with eight recipes and eight divergent UIs, and it's how adding a recipe becomes a Python
task. Instead, **each recipe declares its knobs, and one generic renderer turns knobs into
keyboards.** Four knob types cover the whole cookbook:

| Type | Renders as | Example |
|---|---|---|
| `enum` | one button per value, wrapped into rows | colour, loop mode, font |
| `step` | a `[− Size +]` row that increments in place | caption size, fps, speed |
| `range` | in/out scrubber: `[◀◀ ◀ in ▶ ▶▶]` over the clip | trim points |
| `text` | a `force_reply` prompt; your reply becomes the value | caption text |

Adding a recipe to the cookbook is therefore **writing one TOML file and one pipeline
function — zero bot code.** That is the property that lets the cookbook grow to twenty recipes
without the bot becoming a swamp.

**Callback budget.** `callback_data` is capped at 64 bytes, which is nowhere near enough for a
recipe. So callbacks carry *indices only* — `c/<session6>/<knob_idx>/<value_idx>` — and the
session token resolves to the live recipe in SQLite. No user data ever round-trips through a
button.

## Feature set

### Core engine (unchanged from v1)
- `clipwright cook <recipe> <input> [--text ... --from ... --to ...]` → emits GIF + MP4 with a
  size report, and prints the exact command it ran so every render is reproducible.
- Starter cookbook (~8 recipes): reaction-GIF cutter, top/bottom Impact-style caption card
  (Pillow-rendered — far better typography than ffmpeg `drawtext`), boomerang, speed ramp,
  before/after wipe, subtitle burn from `.srt`, Ken Burns pan over a photo, freeze-frame ending.
- **Generative recipes** (no input footage needed): kinetic typography, emoji-physics particle
  scenes, seamless gradient loops using phase-aligned periodic noise so every loop closes
  perfectly. These matter *more* in the Telegram edition: they're the recipes you can start
  from a phone in a queue, with nothing to upload.
- Each recipe compiles to a *fixed, known* ffmpeg pipeline — not a general filtergraph
  composer. Every invocation is an argv list; `shell=True` appears nowhere in the codebase.
  This was a correctness mitigation in v1; with friends able to send arbitrary caption text,
  it is now a security control.
- `clipwright doctor` — preflight that checks ffmpeg's libass support, gifsicle, Pillow's font
  paths and Noto Color Emoji, printing versions. Five minutes that prevent day-one confusion.

### GIF intelligence (unchanged from v1)
- **Size-budget solver**: recipes declare `fits = "telegram"` (or `discord`, `slack`,
  `whatsapp-sticker`, `shorts-9x16`, or a raw MB number); the engine searches
  fps/scale/color-count at proxy resolution and only encodes at full quality once, with an
  optional `gifsicle -O3 --lossy` squeeze.
- **Perfect-loop finder**: frame-similarity scan near the cut points nudges the out-point to
  the best-matching frame; when footage has no near-repeating frames it degrades *explicitly*
  to boomerang or crossfade rather than shipping a visibly seamed "seamless" loop.

### The Telegram layer
- **Long polling**, no webhook, no inbound ports, no public hosting — the same constraint
  claudelink was built under, and `getWebhookInfo` confirms the bot is currently webhook-free
  and ready to poll.
- **One morphing preview per session.** The first render is sent with `sendAnimation`;
  every subsequent tweak `editMessageMedia`s that same message. The chat shows one clip that
  keeps improving, not a scroll of rejects. This also keeps us clear of the 1-message-per-second
  per-chat rate limit for free.
- **Preview vs export are different artifacts.** Telegram transcodes animations to H.264, so
  the autoplaying preview is an MP4 by design (small, fast, instant). `[⬇ Export]` sends the
  real palette-optimized `.gif` as a *document*, plus its `.recipe.toml` sidecar.
- **Reply-to-remix.** Every sent file's `file_unique_id` is logged against its recipe. Reply
  `/remix` to a GIF from any point in history and the session reopens with its knobs live.
- **Progress that doesn't lie.** `sendChatAction("upload_video")` heartbeats while a render
  runs; a queued job says it's queued and where in line it is.

### The reroll loop
`[🎲 Grid]` renders nine seeded 240p variants (palette / motion / font / loop-point differing)
as one numbered contact-sheet PNG with a 3×3 keyboard beneath it. Tap a number to adopt that
variant, then keep going — mutate a single "gene" and reroll again. This is the mechanic that
does the work natural language would have.

## What a session looks like

```
you            [sends paper_airplane.mp4, 5.0 MB]

@clipwright    Got it — 12.4s, 1080×1920, 30fps.
               Pick a recipe:
               [ 🔁 Loop ] [ 💬 Caption ] [ ↔ Boomerang ]
               [ ⚡ Speed ] [ 🎞 Ken Burns ] [ … more ]

you            [taps 💬 Caption]

@clipwright    [preview.mp4 autoplaying, 2.1s loop]
               caption-loop · 0:02.0–0:04.5 · loop nudged +4f · 1.8 MB
               Send me the caption text ↩
               [− Size +] [🎨 Colour] [⬆ Top/Bottom]
               [◀◀ in ▶▶] [◀◀ out ▶▶] [🎲 Grid]
               [↩ Undo] [⌘ Show CLI] [⬇ Export]

you            my sprint velocity

@clipwright    [same message, preview swapped in place]
               ↑ buttons unchanged

you            [taps 🎨 Colour] → [🟨 Yellow] → [🎲 Grid]

@clipwright    [grid.png — 9 numbered variants]
               [1][2][3]
               [4][5][6]
               [7][8][9]

you            [taps 6]

@clipwright    [preview swapped to variant 6 in place]

you            [taps ⬇ Export]

@clipwright    [sprint-velocity.gif — document, 1.8 MB, fits telegram ✓]
               [sprint-velocity.recipe.toml — document]
               Re-run anytime:
               clipwright cook caption-loop assets/paper_airplane.mp4 \
                 --text "my sprint velocity" --color ffdd00 --seed 6 --fits telegram
               [🔁 Again with new text] [📌 Save as template]
```

Note what never happened: no sentence was parsed, and nothing was guessed.

## Concretely: an instance, and a definition

**The recipe instance** — what a session *is*, written on every export (unchanged from v1):

```toml
# clips/<user_id>/sprint-velocity/recipe.toml
recipe  = "caption-loop"
input   = "assets/paper_airplane.mp4"   # referenced by path + content hash for re-linking
from    = "0:02.0"
to      = "0:04.5"                      # loop finder may nudge this ±10 frames
loop    = "seamless"                    # seamless | boomerang | crossfade | none
fits    = "telegram"
seed    = 6

[caption]
text  = "my sprint velocity"
style = "impact-outline"
color = "#ffdd00"
pos   = "bottom"
```

**The recipe definition** — new in v2; what generates the keyboard above:

```toml
# cookbook/caption-loop.toml
name     = "caption-loop"
blurb    = "Trim a clip, loop it, slap a caption on."
emoji    = "💬"
pipeline = "caption_loop"        # names a Python function, not a composable filtergraph

[[knob]]
key   = "caption.text"
label = "Text"
type  = "text"
max   = 60

[[knob]]
key   = "caption.size"
label = "Size"
type  = "step"
min   = 24
max   = 160
step  = 8

[[knob]]
key    = "caption.color"
label  = "Colour"
type   = "enum"
values = ["#ffffff", "#ffdd00", "#ff4444", "#44ff88"]
labels = ["⬜ White", "🟨 Yellow", "🟥 Red", "🟩 Green"]

[[knob]]
key    = "loop"
label  = "Loop"
type   = "enum"
values = ["seamless", "boomerang", "crossfade", "none"]

[[knob]]
key   = "trim"
label = "Trim"
type  = "range"
```

## Process model

```
clipwright/                 the engine — pure, importable, knows nothing about Telegram
├── cook.py                 recipe → pipeline dispatch
├── pipelines/              one fixed ffmpeg/Pillow pipeline per recipe type
├── budget.py               size solver + platform presets
├── loopfind.py             frame-similarity loop nudger
└── cli.py                  `clipwright cook | remix | doctor | grid`

clipwrightd/                the bot — a client of the engine
├── poll.py                 getUpdates loop, offset persistence, pidfile lock
├── keyboards.py            knob spec → InlineKeyboardMarkup  (the generic renderer)
├── session.py              SQLite: sessions, undo stack, file ledger, quotas
├── queue.py                one render worker, per-user concurrency 1
└── api.py                  stdlib urllib Bot API client + multipart upload
```

**Stdlib only**, matching claudelink: `urllib.request` against the Bot API, `sqlite3` for
state, `tomllib` for recipes, `argparse` for the CLI. No `python-telegram-bot`, no root, no
systemd system unit. State in `~/.clipwright/`: `bot.env` (0600), `state.db`, `offset`,
`daemon.log`, `daemon.pid`.

The one dependency that isn't stdlib is **Pillow**, which is already installed (12.3.0) and
does the caption typography that `drawtext` does badly — it earns its place. v1 proposed Typer
for the CLI; `argparse` is used instead, so the whole thing stays `pip`-free beyond what's
already on the machine.

**Repo location.** New repo at `claude/clipwright/`, with `claude/videos/` indexed as the
owner's asset library — `generate_video.py` and the existing MP4s stay exactly where they are
and nothing depends on them.

## Access control

The bot renders files on your VM, so the boundary is real.

- **Allowlist by Telegram user ID**, read from `~/.clipwright/bot.env`. The owner ID is
  distinguished from friend IDs. Anyone not on the list gets **silence** — logged, never
  replied to, so the bot can't be enumerated by strangers who find the username.
- **Friends are semi-trusted.** Per-user workspaces at `clips/<user_id>/`; friends see only
  their own sessions and their own uploads. The owner's `videos/` asset library is
  owner-only.
- **Every upload is validated by ffprobe before anything touches it** — duration cap,
  resolution cap, rejects anything that doesn't probe as video. Output paths are derived from
  session IDs, never from user-supplied filenames.
- **One render worker, per-user concurrency of 1**, a global queue depth cap, and a per-user
  daily render quota. A friend can queue, not monopolize.
- **Caption text never reaches a shell.** Pillow draws it; argv lists carry it; `shell=True`
  is absent from the codebase.

**On the token itself:** `8108699991:AAG…` was pasted in plaintext, so it now exists in a
chat log. It belongs in `~/.clipwright/bot.env` at mode 0600 and in `.gitignore` — and since
anyone holding it can drive the bot, **rotating it via BotFather (`/revoke`) before this goes
live is the right call.** I'll wire the code to read from the env file, so rotating is a
one-line edit whenever you do it.

## Milestones

1. **Day 1 — the thin slice.** `clipwright cook` (`argparse` + `tomllib`) with three pipelines:
   gifify with two-pass palette, Pillow caption card, boomerang. Plus `clipwright doctor`.
   Then `clipwrightd` at its most skeletal: pidfile, long poll, allowlist, `/start`, `/help`
   — send a video, get a GIF back, no buttons yet. *Success test: send
   `paper_airplane.mp4` from your phone and get a GIF back in under ten seconds.*
2. **Weekend 1 — the button loop.** The knob-spec → keyboard generator and all four knob
   types. SQLite sessions with an undo stack. `editMessageMedia` in-place preview swapping.
   `[⌘ Show CLI]`. Size-budget solver with the `telegram` and `discord` presets. Export as
   document + sidecar. *This is the milestone where it stops being a converter and starts
   being a foundry.*
3. **Weekend 2 — intelligence and rerolls.** Perfect-loop finder with explicit degrade.
   `gifsicle -O3 --lossy` squeeze pass. `[🎲 Grid]` contact sheets with the 3×3 numbered
   keyboard and single-gene mutation. The `file_unique_id` ledger and reply-`/remix`.
4. **Weekend 3 — create from nothing.** Kinetic typography and phase-aligned gradient loops —
   the recipes that need no upload and work from a phone in a queue. Emoji physics after a
   sprite-extraction spike, not before. Owner-only `/lib` asset browser over `videos/`.
5. **Later.** Template distillation (`[📌 Save as template]` turning a session into a
   parameterized recipe with `{{slots}}`), breeding two recipes' genes into an offspring grid,
   a fun-filter shelf (deep-fry, VHS), and batch renders (one recipe × ten labels → one media
   group). If the 20 MB inbound cap ever actually bites, a local Bot API server lifts it.

## Risks and mitigations

**Carried over from v1 — all still live:**

- **GIF metadata is unreliable** and Telegram re-encodes and strips everything → the sidecar
  `.recipe.toml` plus the local `file_unique_id` ledger are the canonical memory; metadata
  embedding is best-effort via `gifsicle --comment` and never relied on.
- **General filtergraph composition is a quoting minefield** → fixed pipeline per recipe type,
  argv lists only. Now a security control, not just a correctness one.
- **Size-solver re-encodes could take minutes** → search at proxy resolution, final encode once.
- **Loop finder can't find loops in non-repeating footage** → explicit degrade to boomerang or
  crossfade, never a seamed loop sold as seamless.
- **Noto Color Emoji is a bitmap font Pillow handles awkwardly** → emoji physics stays a
  milestone-4 feature behind a sprite-extraction spike, never a day-1 dependency.
- **Environment assumptions** → `clipwright doctor` checks libass and font paths in milestone 1.

**New, and specific to Telegram:**

- **20 MB inbound cap.** `getFile` will not fetch anything larger, full stop — so a 4K phone
  video simply cannot be pulled. Mitigation: check `file_size` on arrival and reply with a
  clear "trim it and resend" rather than a failed download. (`paper_airplane.mp4` at 5 MB is
  comfortably inside; a local Bot API server removes the limit later if it becomes a real
  problem.)
- **50 MB outbound cap** → the size-budget solver already guarantees exports land far below it.
- **`callback_data` is 1–64 bytes** → callbacks carry indices and a session token only; the
  recipe lives in SQLite.
- **Rate limits: ~1 message/second per chat, ~30/second globally, 20/minute in groups** →
  edit-in-place is the default interaction, which stays under the limit by construction and
  keeps the chat clean as a side effect.
- **Only one poller may hold the token.** A second `getUpdates` on the same token conflicts and
  updates start vanishing → pidfile lock, and the daemon refuses to start twice.
- **Privacy mode is ON for this bot** (`can_read_all_group_messages: false`, confirmed via
  `getMe`) → in a group it only sees `/commands` and replies to its own messages. Fine for the
  DM-first design; if you ever want free-form group use, that's a BotFather toggle, and the
  "no dead ends" principle keeps working either way.
- **Telegram transcodes animations to H.264** → the preview is an MP4 on purpose; the real
  `.gif` only survives as a document, which is exactly what `[⬇ Export]` sends.
- **Renders block the poll loop** → renders run on a worker thread behind a queue; the poller's
  only job is to accept updates and answer callbacks fast.
- **No LLM means no one to interpret a wrong guess** → `setMyCommands` for autocomplete, a
  `/help` card with example output, and principle 6: every message ends with its legal next
  moves.

## Explicitly out of scope

- **Any paid API** — no generation API, and now no inference API either. `generate_video.py`
  stays in the repo as a separate optional tool; no Clipwright feature may depend on it.
- **Webhooks and public hosting** — long polling only, no inbound ports, runs behind the VM's
  DHCP lease forever.
- **Natural-language parsing** — deliberately, not for now. If it ever returns, it enters as a
  *writer of recipe files* alongside the buttons, never as a layer the buttons depend on.
- **Cloud anything else** — no accounts, no telemetry, no uploads beyond `api.telegram.org`.
- **The Workbench UI** — cut. Telegram is the workbench.
