# Clipwright (clipwright-grok)

This is **Grok Bot's fork** of Clipwright. The engine keeps the product name
**Clipwright**: a $0-per-render GIF/video foundry (ffmpeg + Pillow, no LLM in
the render loop). The Telegram bot and these operator docs are the fork —
**clipwright-grok** — the copy Grok Bot runs and keeps alive. They are not
the upstream [CLIPWRIGHT_PLAN.md](CLIPWRIGHT_PLAN.md) plan dump (that file is
intent; [docs/ENGINE_CONTRACT.md](docs/ENGINE_CONTRACT.md) is the contract).
The live @username is whatever you registered with BotFather. These docs say
**your bot** and do not invent a handle.

## Seed `/gif`

`/gif` obtains a seed, then opens the same recipe / knob / preview / export
foundry:

- Reply to a **photo** (or image document) → Ken Burns (`ken-burns`).
- Reply to a **video** or animation → gifify.
- Reply to a **text** message, or `/gif dog` → a Wikimedia Commons still
  (then Ken Burns), or a local typecard if the fetch fails.
- Bare `/gif` → a self-aware "missing argument" typecard. It answers; it
  does not crash or stay silent.

In a DM, a photo or video with no command opens the same keyboard. In a
group or channel, only `/gif` and the other commands start work.

## Operators (Grok Bot)

State lives in `$CLIPWRIGHT_HOME` (default `~/.clipwright`): `bot.env`,
`daemon.pid`, `offset`, `daemon.log`, `state.db`, and a `live-src/` git
checkout of this fork. The running daemon is **that checkout**, not whatever
`clipwrightd` happens to be on `PATH`. Restart and verify from here; you do
not need to read `poll.py`.

One-time, from a clone that already contains `clipwrightd/ops.py`:

```bash
mkdir -p ~/.clipwright
git clone <clipwright-grok remote> ~/.clipwright/live-src   # origin MUST be this fork
# write ~/.clipwright/bot.env mode 0600 — token and owner id, see below
python3 -m clipwrightd.ops --home ~/.clipwright deploy main
python3 -m clipwrightd.ops --home ~/.clipwright health
```

After a code change:

```bash
python3 -m clipwrightd.ops deploy <git-ref>    # fetch, detach live-src at that commit, restart, print status
python3 -m clipwrightd.ops health              # exit 0 only when the pidfile is live and getUpdates is fresh
python3 -m clipwright doctor                   # toolchain preflight; it does not talk to Telegram
```

`live-src`'s **`origin` remote must be this fork** (repo name
`clipwright-grok`). The upstream plan repo is a different history. If you
keep it, name that remote **`upstream`**, not `origin`:

```bash
git -C ~/.clipwright/live-src remote rename origin upstream   # only if origin is CLIPWRIGHT_PLAN
git -C ~/.clipwright/live-src remote add origin <clipwright-grok url>
```

`deploy REF` fetches one remote and detaches `live-src` at `REF` (a branch
name prefers `<remote>/REF`), then restarts the daemon and prints status,
including `remote: <name> <url>`. A remote whose URL ends with
`clipwright-grok` is used instead of `origin` when both exist, so a leftover
`origin` pointing at `CLIPWRIGHT_PLAN` cannot check out the old plan commit.
If the remote that would be fetched is named `CLIPWRIGHT_PLAN`, `deploy`
exits 2 unless `--force-remote`. `--remote NAME` or `$CLIPWRIGHT_OPS_REMOTE`
picks the remote by name (`CLIPWRIGHT_OPS_REMOTE=origin` forces origin, and
still refuses a plan URL without `--force-remote`). A dirty tree is refused
unless `--force`, which discards tracked edits and leaves untracked files
alone. Put `bot.env` in the state directory, not inside `live-src`.

`restart` sends SIGTERM to the pid that holds `daemon.pid`, waits until the
lock is free (up to a few minutes, so an in-flight render can finish), then
starts `python3 -m clipwrightd --home $CLIPWRIGHT_HOME`
with that checkout on `PYTHONPATH`. It will not signal a live pid that does
not hold the flock, and it will not start a second poller while the lock is
held. SIGTERM is the daemon's Ctrl-C path: an in-flight render is allowed
to finish. Startup stderr goes to `daemon.out`; the daemon's own log stays
`daemon.log`.

`status` prints the home, the live-src commit, and the health line, and
always exits 0. `health` exits 1 when the pidfile is missing, the recorded
pid is dead, the pid is alive but does not hold the lock, or `getUpdates`
has not returned within `--max-age` seconds (default 600).

`health` uses state the daemon already writes. `daemon.pid` is the flock.
`<home>/offset` stores the next update id, and the daemon rewrites it after
every successful `getUpdates` (the integer does not change on an empty
batch) so the file's mtime is the last poll that returned. There is no
separate heartbeat file. A process that just started gets one `--max-age`
window to finish its first poll. A long poll waits up to 50 seconds and
errors back off up to 60 seconds, so the default 600 seconds means stuck.

Morning Brief asks Grok Bot for a “morning digest”. Grok runs `python3 -m clipwrightd.ops digest` (default `--since 16`, `--home` the same state dir) and does not start a second `getUpdates` poller. The command prints a short summary and writes `$CLIPWRIGHT_HOME/digest-latest.json` (`--json PATH` also appends one JSON line, for example `digest.jsonl`). It includes pidfile and offset age, open `/gif` sessions (token, user, chat, recipe, caption or seed text, created and updated), ledger exports in the window, and digest events: ignored group, supergroup, and channel text (a ~160-character preview, kept about 48 hours in `digest-events.jsonl`), `/gif` session opens, web-seed failures that fell back to a typecard, and render errors. No bot token and no media bytes. **Privacy mode must stay off** in BotFather, or the poller never receives the chatter to record. `CLIPWRIGHT_GROUP_IDS`, when set, scopes which rooms are recorded; a room outside that list is dropped before any digest event is written.

Once installed, the same commands are `clipwright-ops`. `--home DIR` matches
`clipwrightd --home`.

Two things live in the tree besides the ops layer:

1. **The Clipwright engine and `clipwrightd`.** Milestone 1 is `clipwright cook`
   (gifify, caption-loop, boomerang, two-pass palette GIFs, the Pillow caption
   card, `clipwright doctor`). Milestone 2 is the button loop, in-place preview
   swapping, export with sidecar, and `/remix`. `typecard` and `ken-burns`
   shipped with the `/gif` seed front door. Not built yet: the `[🎲 Grid]`
   contact sheet, the crossfade loop, and the remaining generative recipes
   (emoji physics, gradient loops).
   ([CLIPWRIGHT_PLAN_v1_claude-code-skill.md](CLIPWRIGHT_PLAN_v1_claude-code-skill.md)
   is the superseded v1.)
2. **The gag-GIF episode scripts** — standalone renders about a real group
   chat, built from verbatim quotes. This fork does not change them. Documented
   further down.

## Clipwright engine

Python 3.12, stdlib + Pillow, and three system binaries: `ffmpeg`, `ffprobe`, `gifsicle`.
Every media call is an argv list (`shell=True` appears nowhere); caption text is drawn by
Pillow into a PNG and composited with `overlay`, so it never enters a filtergraph string.

### Install

```bash
pip install -e .                 # `clipwright`, `clipwrightd`, and `clipwright-ops` on PATH
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
✓ cookbook         5 recipes: boomerang, caption-loop, gifify, ken-burns, typecard
all required checks passed
```

### Cook

```bash
# any clip -> a looping GIF that fits Telegram's 8 MB, plus an MP4 preview
clipwright cook gifify clip.mp4

# trim, loop, and burn an Impact-style caption (a palette colour: bare hex or #rrggbb, any case)
clipwright cook caption-loop clip.mp4 --text "my sprint velocity" --color ffdd00 --from 0:02.0 --to 0:04.5

# a still: Ken Burns zoom, optional caption
clipwright cook ken-burns photo.jpg --text "dog"

# words alone — no input file (kinetic caption card)
clipwright cook typecard --text "MISSING ARGUMENT"
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
📷 ken-burns — Ken Burns a still: slow zoom, optional caption.
    caption.text   text   up to 60 chars  (default "")
    zoom           enum   1.2 | 1.4 | 1.6 | 1.9  (default 1.4)
    duration       step   2..6 by 1  (default 3)
    …
🃏 typecard — A kinetic caption card from words alone.
    caption.text   text   up to 80 chars  (default "")
    bg             enum   #121220 | …  (default "#121220")
    duration       step   2..6 by 1  (default 3)
    …
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

`clipwrightd` is Grok Bot's client of the engine — the same `cook`, driven by
buttons, speaking as the clipwright-grok fork. It is stdlib only (`urllib`
against `api.telegram.org` and, for `/gif <words>`, Wikimedia Commons;
`sqlite3` for state, one worker thread for renders). Offline still works when
the user already supplied media or when the local typecard fallback can run.
Start, restart, and verify it with `python3 -m clipwrightd.ops` (see
[Operators](#operators-grok-bot) above).

### Configure

Create `~/.clipwright/bot.env` (or `$CLIPWRIGHT_HOME/bot.env`) with mode **0600** — the
daemon warns loudly on any other mode:

```
# the token from BotFather
CLIPWRIGHT_BOT_TOKEN=123456789:AA...
# your Telegram user id
CLIPWRIGHT_OWNER_ID=111111111
# optional extra DM allowlist (group members are remembered automatically;
# everyone else still gets silence in a DM)
CLIPWRIGHT_FRIEND_IDS=222222222,333333333
# optional comma list of group chat ids (negative); set, the bot works only in those groups
# and ignores any other it is added to; unset, it works in every group it is in. Either way,
# once it is in the groups you want, BotFather's /setjoingroups → Disable stops further adds
CLIPWRIGHT_GROUP_IDS=-1001234567890
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
# renders per day for a group member who is neither owner nor friend (legacy;
# groups no longer use this as a gate — everyone in the room is trusted)
CLIPWRIGHT_GUEST_PER_DAY_QUOTA=10
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
python3 -m clipwrightd            # foreground; or use clipwrightd.ops deploy/restart, which daemonizes
```

`bot.env` stays in the state directory. `clipwrightd.ops deploy` checks out
code under `live-src/` and does not read the token.

**Rotate the token first.** The plan pasted a bot token in plaintext (`8108699991:AAG…`), so
it exists in a chat log; anyone holding it can drive the bot. Revoke it via BotFather
(`/revoke`) and put the new one in `bot.env` before the first run. The daemon takes a
pidfile lock in the state dir because a second poller on the same token makes updates vanish.

### What works today

- `/gif` is the front door. It obtains a **seed**, then opens the same recipe / knob /
  preview / export foundry:
  - Reply to a **photo** (or image document) → Ken Burns preview you can tune.
  - Reply to a **video** / animation → gifify preview (same as before).
  - Reply to a **text** message, or `/gif dog` → Wikimedia Commons still (then Ken Burns),
    or a local typecard if the fetch fails.
  - Bare `/gif` → a self-aware "missing argument" typecard; it does not crash or stay silent.
- Send a photo or video in a DM (no `/gif` needed) and get the same keyboard.
- Every button press edits the recipe by one field and re-renders the preview **in place**
  (`editMessageMedia`); `↩ Undo` pops the session's undo stack; `✎` buttons ask for text
  with a force-reply prompt; `⌘ Show CLI` prints the reproducing command.
- `⬇ Export` cooks the real GIF under its size budget and sends it as a document with its
  `.recipe.toml` sidecar; the sent file's `file_unique_id` is ledgered.
- Reply `/remix` to a GIF the bot sent you and the session reopens with its knobs live (your
  own exports, or any export if you are the owner).
- In a group **or channel** (Test2 the same as weir and bencho), only `/gif` (and the other commands) start work: bare videos and chatter are
  ignored without a word, other bots' commands are left alone unless addressed `@<bot>`,
  and everything the bot sends there is a reply to the message that asked. **Everyone in
  the room is trusted** (the owner put the bot there on purpose) — no guest-quota friction.
  Chatter is not a job, but it does remember the sender: **once the bot has seen you
  in a group, you can DM it** the same way friends do (photo or video, no `/gif` needed).
  A lurker who never spoke is checked with `getChatMember` against groups the bot knows.
- Buttons and text prompts belong to the user who opened the session: another member's press
  gets a "someone else's session" toast, and only the owner's reply *to the prompt* is taken
  as the answer — ordinary chatter from them is left alone even when the bot's privacy mode
  is off.
- Anonymous admins, members posting as a channel and Telegram's service accounts all share one
  sender id, so they are not served: `/gif` from them gets a one-line "send it as yourself",
  everything else is ignored.
- `/start`, `/help`, `/recipes`; DM allowlist (owner + friends + anyone seen in a served group);
  groups **and channels** open to every member
  (every group the bot is in, or only those in `CLIPWRIGHT_GROUP_IDS`); per-user concurrency
  of 1, a queue depth cap, and a per-day export quota.

### What does not, yet

- **Grid** — the `[🎲 Grid]` button answers with a toast; 3×3 contact sheets and single-gene
  mutation are not built.
- **Crossfade loops** — render straight, labelled as such.
- **More generative recipes** (emoji physics, gradient loops) — `typecard` is the first
  `needs_input = false` recipe; the rest are still unbuilt.
- **Local Bot API server** — the 20 MB inbound cap stands; oversize uploads get a "trim it
  and resend" reply rather than a download.

## Layout

```
clipwright/              the engine — pure, importable, knows nothing about Telegram
  cli.py                 `clipwright cook | remix | recipes | probe | doctor`
  doctor.py              toolchain, font, state-dir and bot.env preflight (`--home`)
  cook.py                instance -> validated pipeline run -> outputs + sidecar
  recipe.py              cookbook loader, TOML writer, dotted knobs, validation, apply_knob
  pipelines/             gifify, caption_loop, boomerang, ken_burns, typecard
  ffmpeg.py              argv builders and the only subprocess wrapper for media binaries
  caption.py             Pillow caption band (the only place user text becomes pixels)
  budget.py              size presets, encode ladder, best-first solver, gifsicle squeeze
  loopfind.py            frame-similarity perfect-loop finder
  cookbook/*.toml        recipe definitions (gifify, caption-loop, boomerang, ken-burns, typecard)
clipwrightd/             the bot — a client of the engine
  poll.py                getUpdates loop, pidfile lock, offset persistence, every handler
  ops.py                 live-src deploy/restart/status/health, and `ops digest`
  fetch.py               Wikimedia Commons still search for /gif <words> (injectable; mocked in tests)
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
(`test_daemon.py`, `test_api.py`, `test_keyboards.py`, `test_session.py`, `test_fetch.py`,
all against a fake transport that never opens a socket), the series kits (`test_kits.py`,
`test_style_common.py`) and the ledger (`test_series_state.py`). The engine tests render a
synthetic 3 s clip made with `ffmpeg -f lavfi testsrc2`, so they need ffmpeg and gifsicle
but no network; the whole suite finishes in well under a minute.

## A note on repo size

Every episode's GIF is committed straight into git (`du -sh .git` was already 41 MB before
this pass, no packs yet). `git-lfs` isn't installed on this machine; if the series keeps
growing at its current rate, migrating the `*.gif` history to LFS is worth doing before the
clone gets painful — not done here since it rewrites history and touches tooling beyond
this repo.
