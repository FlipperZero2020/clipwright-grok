# Clipwright

Two things live in this repo:

1. **The Clipwright engine and its Telegram daemon** — the $0-per-render GIF/video foundry
   that [CLIPWRIGHT_PLAN.md](CLIPWRIGHT_PLAN.md) describes (ffmpeg + Pillow, no LLM in the
   render loop). **This now exists as code.** Milestone 1 is done (`clipwright cook` with the
   gifify, caption-loop and boomerang pipelines, two-pass palette GIFs, the Pillow caption
   card, `clipwright doctor`), and so are the keyboard, session, Bot API and daemon parts of
   Milestone 2 (the button loop, in-place preview swapping, export with sidecar, `/remix`).
   Not built yet: the `[🎲 Grid]` contact sheet, the crossfade loop, and the generative
   recipes. [docs/ENGINE_CONTRACT.md](docs/ENGINE_CONTRACT.md) is the binding module
   contract; the plan is the intent. ([CLIPWRIGHT_PLAN_v1_claude-code-skill.md](CLIPWRIGHT_PLAN_v1_claude-code-skill.md)
   is the superseded v1.)
2. **A working render pipeline that already ships GIFs** — the gag-GIF series about a real
   group chat's coworkers, built from their own verbatim quotes. Standalone scripts, one per
   episode, sharing three visual "kits". Documented further down.

## Clipwright engine

Python 3.12, stdlib + Pillow, and three system binaries: `ffmpeg`, `ffprobe`, `gifsicle`.
Every media call is an argv list (`shell=True` appears nowhere); caption text is drawn by
Pillow into a PNG and composited with `overlay`, so it never enters a filtergraph string.

### Install

```bash
pip install -e .                 # gives you `clipwright` and `clipwrightd` on PATH
python3 -m clipwright doctor     # or run from the repo root without installing
```

`doctor` checks the toolchain before the first render and exits 1 if a required check fails.
It runs even where Pillow or tomllib is missing — that is what it is for — and reports them
as failed rows (`cook` on such a machine exits 2 with one line pointing back at `doctor`).
The `(optional)` rows never fail the run: libass and Impact only change looks (captions fall
back to Liberation Sans Bold), Noto Color Emoji is needed only by the milestone-4 emoji
recipes, and `bot.env` only by the daemon (its row reports path and mode; nothing from the
file is ever printed). The state dir is resolved exactly the way `clipwrightd` resolves it —
`--home DIR`, then `$CLIPWRIGHT_HOME`, then a `CLIPWRIGHT_HOME=` line in
`~/.clipwright/bot.env` (the only line consulted), then `~/.clipwright` — so
`clipwright doctor --home DIR` checks the directory the daemon will actually write to:

```
✓ ffmpeg           6.1.1-3ubuntu5
✓ ffprobe          6.1.1-3ubuntu5
✓ gifsicle         1.94
✓ libx264          built in
✓ libass           built in  (optional)
✓ palette filters  palettegen, paletteuse
✓ Pillow           12.3.0
✓ tomllib          stdlib, Python 3.12.3
✓ caption font     /usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf (Impact absent; captions use this fallback)
✓ fonts dir        /usr/share/fonts readable
✓ emoji font       /usr/share/fonts/truetype/noto/NotoColorEmoji.ttf  (optional)
✓ state dir        /home/tom/.clipwright will be created
✗ bot.env          /home/tom/.clipwright/bot.env absent (the daemon needs it; the CLI does not)  (optional)
✓ cookbook         3 recipes: boomerang, caption-loop, gifify
all required checks passed
```

### Cook

```bash
# any clip -> a looping GIF that fits Telegram's 8 MB, plus an MP4 preview
clipwright cook gifify clip.mp4

# trim, loop, and burn an Impact-style caption (a palette colour: bare hex or #rrggbb, any case)
clipwright cook caption-loop clip.mp4 --text "my sprint velocity" --color ffdd00 --from 0:02.0 --to 0:04.5

# forward, then backward, forever
clipwright cook boomerang clip.mp4 --from 0:00.5 --to 2
```

Outputs land in `./clips/` (`--out DIR` to change) under a stem the engine chooses —
`<recipe>-<caption slug>-<6 hex>` — never the input filename. `--proxy` renders only a small
MP4 preview (≤ 240 px, fast) and skips the size-budget search. A render works on at most
**15 s** of source: a longer clip, or a `--from`/`--to` range longer than that, is cut to 15 s
from the in-point and the output says so. Every run prints one line per output (the `fits`
verdict sits on the GIF only — it is the file the budget applies to; the MP4 is a preview),
what the loop finder decided, and the command that reproduces it. This is the second
command above, verbatim:

```
clips/caption-loop-my-sprint-velocity-adb1e8.gif  377,727 bytes  fits telegram ✓
clips/caption-loop-my-sprint-velocity-adb1e8.mp4  54,113 bytes
clips/caption-loop-my-sprint-velocity-adb1e8.recipe.toml  191 bytes
loop: seamless, seam 6.15, out-point moved -0.6 s
Re-run anytime:
  clipwright cook caption-loop clip.mp4 --from 0:02.0 --to 0:04.5 --set 'caption.text=my sprint velocity' --set 'caption.color=#ffdd00'
```

`--text/--color/--size/--pos` are sugar for the `caption.*` knobs and `--loop/--fits` for
the `loop`/`fits` knobs; each refuses a recipe that does not declare that knob (`boomerang`
has no `loop`: its motion is the loop). `--from/--to/--seed` set the top-level keys every
recipe has; `--set key=value` (repeatable) sets any knob and is applied last. Values coerce
to int/float/true/false where they look like one (a `#` prefix always stays a string; a text
knob always stays text). `--color` takes one of the recipe's palette values, as `#rrggbb` or
bare `rrggbb` in either case. `--fits` takes a preset (`telegram`, `discord`, `slack`,
`whatsapp-sticker`, `shorts-9x16`) or a number of MB. The cookbook, as
`python3 -m clipwright recipes` prints it:

```
🪃 boomerang — Forward, then backward, forever.
    fps            step   8..30 by 2  (default 15)
    width          step   240..640 by 40  (default 480)
    fits           enum   telegram | discord | slack  (default "telegram")
    trim           range  nudges --from/--to by 0.1 s
💬 caption-loop — Trim a clip, loop it, slap a caption on.
    caption.text   text   up to 60 chars  (default "")
    caption.size   step   24..160 by 8  (default 64)
    caption.color  enum   #ffffff | #ffdd00 | #ff4444 | #44ff88  (default "#ffffff")
    caption.pos    enum   top | bottom  (default "bottom")
    loop           enum   seamless | boomerang | crossfade | none  (default "seamless")
    fits           enum   telegram | discord | slack  (default "telegram")
    trim           range  nudges --from/--to by 0.1 s
🎞️ gifify — Turn a clip into a looping GIF that fits where you're posting it.
    fps            step   8..30 by 2  (default 15)
    width          step   240..640 by 40  (default 480)
    colors         enum   64 | 128 | 256  (default 256)
    loop           enum   seamless | boomerang | crossfade | none  (default "seamless")
    fits           enum   telegram | discord | slack  (default "telegram")
    trim           range  nudges --from/--to by 0.1 s
```

`loop = "seamless"` runs the perfect-loop finder (frame-similarity scan that nudges the
out-point to the best-matching frame) and degrades explicitly to a boomerang when the footage
has no near-repeating frames; the `loop:` line says which happened — `loop: seamless, seam
6.15, out-point moved -0.6 s` above, or `loop: boomerang (degraded), seam 41.2` when it gave
up. `crossfade` currently renders straight and says so (`loop: none (crossfade not
implemented)`). A capped segment adds `trimmed to 15 s (renders cap at 15 s; --from/--to
picks the segment)`. `clipwright probe clip.mp4` shows what ffprobe sees (size, duration,
fps, frames, codec, audio).

### Sidecars and remix

Every cook writes `<stem>.recipe.toml` beside its outputs — the whole recipe instance, the
same TOML the Telegram sessions edit:

```toml
recipe = "caption-loop"
loop = "seamless"
fits = "telegram"
input = "clip.mp4"
from = "0:02.0"
to = "0:04.5"

[caption]
text = "my sprint velocity"
size = 64
color = "#ffdd00"
pos = "bottom"
```

`clipwright remix clips/<stem>.recipe.toml --set caption.color=#ff4444` reopens that
instance, applies the change, and cooks it again under a new stem (any knob change gives a
new hash, so nothing is overwritten). A relative `input` is looked up from the working
directory first and then beside the sidecar. Both `cook` and `remix` exit 2 with one line on
stderr for an unknown recipe (listing the cookbook), a bad knob, a missing input, a sidecar
key the recipe does not declare (`[caption] style = "nope"`, or `fps = 0` in a caption-loop
sidecar), a value the validator accepts but the clip cannot satisfy (`from = "5"` on a 3 s
clip), or an ffmpeg or gifsicle failure — never a traceback.

## Telegram daemon

`clipwrightd` is a client of the engine — the same `cook`, driven by buttons. It is stdlib
only (`urllib` against `api.telegram.org`, `sqlite3` for state, one worker thread for
renders) and its only network traffic is the Bot API.

### Configure

Create `~/.clipwright/bot.env` (or `$CLIPWRIGHT_HOME/bot.env`) with mode **0600** — the
daemon warns loudly on any other mode:

```
# the token from BotFather
CLIPWRIGHT_BOT_TOKEN=123456789:AA...
# your Telegram user id
CLIPWRIGHT_OWNER_ID=111111111
# optional comma list; everyone else gets silence
CLIPWRIGHT_FRIEND_IDS=222222222,333333333
# optional: relocate the state dir (uploads, renders, sqlite, pidfile, log);
# $CLIPWRIGHT_HOME in the process environment wins over this line, --home over both
CLIPWRIGHT_HOME=/home/tom/.clipwright
# optional limits, shown at their defaults:
# inbound video size gate (Telegram's own getFile cap)
CLIPWRIGHT_MAX_UPLOAD_BYTES=20000000
# probe gates: longest and widest/tallest clip accepted
CLIPWRIGHT_MAX_DURATION_S=60
CLIPWRIGHT_MAX_DIM=1920
# exports per user per day, and render jobs queued at once
CLIPWRIGHT_PER_DAY_QUOTA=200
CLIPWRIGHT_QUEUE_DEPTH=8
# uploads + renders kept on disk per user, and how many days an idle session lives
CLIPWRIGHT_MAX_USER_BYTES=1000000000
CLIPWRIGHT_RETENTION_DAYS=14
```

Parsing is deliberately minimal: whole-line `#` comments only (an inline `# note` after a
value becomes part of the value), an optional `export ` prefix is dropped, and one pair of
surrounding `"` or `'` quotes is stripped from a value. Nothing else: no interpolation, no
escapes.

```bash
chmod 600 ~/.clipwright/bot.env
python3 -m clipwrightd            # or `clipwrightd` once installed; --once handles one batch
```

**Rotate the token first.** The plan pasted a bot token in plaintext (`8108699991:AAG…`), so
it exists in a chat log; anyone holding it can drive the bot. Revoke it via BotFather
(`/revoke`) and put the new one in `bot.env` before the first run. The daemon takes a
pidfile lock in the state dir because a second poller on the same token makes updates vanish.

### What works today

- Send a video (or animation, or a `video/*` document) → it is size-gated (≤ 20 MB, Telegram's
  own `getFile` cap), downloaded, probed (≤ 60 s, ≤ 1920 px), and answered with a proxy MP4
  preview of the `gifify` recipe plus an inline keyboard of its knobs; the keyboard's top row
  switches to `caption-loop` or `boomerang` without re-uploading.
- Every button press edits the recipe by one field and re-renders the preview **in place**
  (`editMessageMedia`); `↩ Undo` pops the session's undo stack; `✎` buttons ask for text
  with a force-reply prompt; `⌘ Show CLI` prints the reproducing command.
- `⬇ Export` cooks the real GIF under its size budget and sends it as a document with its
  `.recipe.toml` sidecar; the sent file's `file_unique_id` is ledgered.
- Reply `/remix` to a GIF the bot sent you and the session reopens with its knobs live (your
  own exports, or any export if you are the owner).
- `/start`, `/help`, `/recipes`; allowlist (owner + friends), per-user concurrency of 1, a
  queue depth cap, and a per-day export quota.

### What does not, yet

- **Grid** — the `[🎲 Grid]` button answers with a toast; 3×3 contact sheets and single-gene
  mutation are not built.
- **Crossfade loops** — render straight, labelled as such.
- **Generative recipes** (kinetic typography, gradient loops, emoji physics) — the cookbook
  has only the three clip recipes; `needs_input = false` is wired but nothing uses it.
- **Local Bot API server** — the 20 MB inbound cap stands; oversize uploads get a "trim it
  and resend" reply rather than a download.

## Layout

```
clipwright/              the engine — pure, importable, knows nothing about Telegram
  cli.py                 `clipwright cook | remix | recipes | probe | doctor`
  doctor.py              toolchain, font, state-dir and bot.env preflight (`--home`)
  cook.py                instance -> validated pipeline run -> outputs + sidecar
  recipe.py              cookbook loader, TOML writer, dotted knobs, validation, apply_knob
  pipelines/             gifify, caption_loop, boomerang (+ common plumbing)
  ffmpeg.py              argv builders and the only subprocess wrapper for media binaries
  caption.py             Pillow caption band (the only place user text becomes pixels)
  budget.py              size presets, encode ladder, best-first solver, gifsicle squeeze
  loopfind.py            frame-similarity perfect-loop finder
  cookbook/*.toml        recipe definitions
clipwrightd/             the bot — a client of the engine
  poll.py                getUpdates loop, pidfile lock, offset persistence, every handler
  keyboards.py           knob spec -> inline keyboard, 64-byte callback encoding
  session.py             SQLite: sessions, undo stack, file ledger, quotas
  queue.py               one render worker, per-user concurrency 1
  api.py                 stdlib Bot API client with multipart upload
  config.py              ~/.clipwright/bot.env reader (0600 check)
docs/ENGINE_CONTRACT.md  the binding module contract the code above is built to
tests/                   everything: engine, daemon (fake transport), kits, series ledger
tools/
  check_series.py        lint series_state.json against the GIFs on disk
  gen_gif_index.py       regenerate GIF_INDEX.md from series_state.json
  fetch_open_mic_photos.py  download the Commons photos open_mic_office.py cuts to

style_common.py          shared plumbing for the series kits: font loading, easing,
                         word-wrap, the frame-buffer -> two-pass-gifsicle GIF export
ballad_kit.py            visual style 1 — title / chat / document / dossier / counter
tabloid_kit.py           visual style 2 — a newsprint front page with a STOP PRESS slam-in
meme_kit.py              visual style 3 — real quotes stamped on api.memegen.link templates
ep*.py, <name>_NN_*.py   one script per episode: data + an emblem/drawing function,
                         imports one of the three kits above
GIF_INDEX.md             human-readable table of every episode: subject, style, topic
series_state.json        source of truth for the rotation (done vs. pending subjects)
```

Each episode script is standalone and re-runnable: `python3 ep06_aaron.py` regenerates
that episode's GIF in place. All three kits share `style_common.py` for the parts that
don't vary (word-wrap, easing curves, the adaptive-palette + `gifsicle -O2 --careful`
squeeze), so adding a fourth kit means writing only what's actually new about it. Two
scripts predate the kits and still carry private copies of that code: `tyler_01_enquirer.py`
is the page `tabloid_kit.py` was generalised from, and `justin_gif_render.py` is the
original five-scene ballad that `ballad_kit.py` was lifted from (the ep01 the kit's docstring
points at). Every later episode imports a kit; those two are the exceptions, kept as-is
because porting them changes pixels.

## Running an episode

```bash
pip install -r requirements.txt      # Pillow (rendering) + pytest (tests)
python3 ep06_aaron.py                # regenerates aaron_01_the_humidity.gif
```

Also needed, both already present on this machine:
- **`gifsicle`** (system binary, not pip) — does the final palette/size squeeze.
- **DejaVu fonts** at `/usr/share/fonts/truetype/dejavu/` — hardcoded in `style_common.py`,
  matching the rest of this VM's render tooling.

`meme_kit.py` additionally needs live network access to `api.memegen.link` at render time —
every other kit draws its own pixels and works fully offline.

`open_mic_office.py` cuts away to three Wikimedia Commons photos that are not committed
(`assets/photos/` is git-ignored). Run `python3 tools/fetch_open_mic_photos.py` once from a
fresh checkout; it downloads them under the names the script expects and prints the credits.

After adding or renaming an episode: `python3 tools/check_series.py` (lints the ledger
against the GIFs on disk) and then `python3 tools/gen_gif_index.py` (regenerates
`GIF_INDEX.md`; a rerun on an unchanged ledger is a no-op diff).

## House rules for a new episode

Taken from the care already visible in `ep06_aaron.py`'s docstring — written down here so
it isn't only implicit in one file:

- **Verbatim, first-person quotes only.** Don't paraphrase what someone said into something
  punchier.
- **Exclude anything sourced only to a third party's gossip about the subject** — a claim
  the subject hasn't confirmed themselves doesn't go in, even if it's funnier.
- **Exclude anything that's a real disclosure risk** (health, finances, immigration status,
  etc.) even when it's already circulating in the chat.
- When two details are inseparable in the source material (same quote, same message) and
  one is excluded, the other goes with it rather than being kept in isolation.

## Tests

```bash
python3 -m pytest -q
```

Runs everything — the engine (`tests/test_cli.py`, `test_cook.py`, `test_recipe.py`,
`test_ffmpeg.py`, `test_caption.py`, `test_budget.py`, `test_loopfind.py`), the daemon
(`test_daemon.py`, `test_api.py`, `test_keyboards.py`, `test_session.py`, all against a fake
transport that never opens a socket), the series kits (`test_kits.py`,
`test_style_common.py`) and the ledger (`test_series_state.py`). The engine tests render a
synthetic 3 s clip made with `ffmpeg -f lavfi testsrc2`, so they need ffmpeg and gifsicle
but no network; the whole suite finishes in well under a minute.

## A note on repo size

Every episode's GIF is committed straight into git (`du -sh .git` was already 41 MB before
this pass, no packs yet). `git-lfs` isn't installed on this machine; if the series keeps
growing at its current rate, migrating the `*.gif` history to LFS is worth doing before the
clone gets painful — not done here since it rewrites history and touches tooling beyond
this repo.
