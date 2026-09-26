# Clipwright engine — module contract

This is the build contract for the code under `clipwright/` (engine) and
`clipwrightd/` (Telegram daemon) in the **clipwright-grok** fork — the tree
Grok Bot runs. The engine's product name stays Clipwright. It turns
CLIPWRIGHT_PLAN.md into concrete module boundaries so pieces can be built
and tested independently. Where this file and the plan disagree, this file
wins for code; the plan wins for intent.

Hard rules (from the plan, now enforced in code):

- Python 3.12, **stdlib + Pillow only**. No python-telegram-bot, no typer.
- Every ffmpeg/ffprobe/gifsicle call is an **argv list**. `shell=True` never
  appears anywhere. User-supplied text never enters a filtergraph string —
  captions are drawn by Pillow into a PNG and composited with `overlay`.
- Output paths derive from a `stem` the engine chooses, never from a
  user-supplied filename. Input paths are validated (`os.path.isfile`) before
  use.
- Every render is reproducible: `CookResult.argv_log` holds every argv run,
  and `cook.cli_command(instance)` prints the equivalent CLI line.
- $0 per render: the only network the engine touches is none; the daemon
  talks to `api.telegram.org` and, for `/gif <words>`, Wikimedia Commons
  (timeouts, content-type and byte-cap; injectable so tests never open a
  socket). The local typecard fallback needs no network.

## Recipe instance (what a session *is*)

A plain nested `dict`, loaded from / written to TOML:

```toml
recipe = "caption-loop"
input  = "assets/paper_airplane.mp4"   # absent for generative recipes
from   = "0:02.0"                      # optional; "M:SS.s" or plain seconds
to     = "0:04.5"
loop   = "seamless"                    # seamless | boomerang | crossfade | none
fits   = "telegram"                    # preset name or a number of MB
seed   = 6

[caption]
text  = "my sprint velocity"
color = "#ffdd00"
pos   = "bottom"
size  = 64
```

Knob keys are dotted (`caption.text`). `recipe.get(inst, "caption.text")` and
`recipe.set_(inst, "caption.text", v)` do the nesting. An instance may carry
only the instance fields (`recipe`/`input`/`from`/`to`/`seed`) and the knobs
its recipe declares — `cook` rejects any other key (a `[caption] style`
line, say: the caption style is `caption`'s fixed default, not a knob), so
`loop` and `fits` above are legal because caption-loop declares them.

## Recipe definition (cookbook/*.toml)

```toml
name     = "caption-loop"
blurb    = "Trim a clip, loop it, slap a caption on."
emoji    = "💬"
pipeline = "caption_loop"      # key in clipwright.pipelines.PIPELINES
needs_input = true             # false for generative recipes

[[knob]]
key = "caption.text"; label = "Text"; type = "text"; max = 60; default = ""
[[knob]]
key = "caption.size"; label = "Size"; type = "step"; min = 24; max = 160; step = 8; default = 64
[[knob]]
key = "caption.color"; label = "Colour"; type = "enum"
values = ["#ffffff", "#ffdd00", "#ff4444", "#44ff88"]
labels = ["⬜ White", "🟨 Yellow", "🟥 Red", "🟩 Green"]; default = "#ffffff"
[[knob]]
key = "loop"; label = "Loop"; type = "enum"; values = ["seamless","boomerang","crossfade","none"]; default = "seamless"
[[knob]]
key = "trim"; label = "Trim"; type = "range"; step_s = 0.1   # edits from/to
```

Knob types: `enum` (values, optional labels), `step` (min, max, step),
`text` (max), `range` (step_s; operates on the instance's `from`/`to`).
Every knob has `default` except `range`.

## `clipwright/recipe.py`

```python
class RecipeError(ValueError): ...
@dataclass class Knob: key, label, type, default=None, values=None, labels=None, min=None, max=None, step=None, max_len=None, step_s=0.1
@dataclass class RecipeDef: name, blurb, emoji, pipeline, needs_input, knobs: list[Knob]
def load_cookbook(dir: str|None = None) -> dict[str, RecipeDef]   # default: clipwright/cookbook/
def load_instance(path) -> dict
def loads_instance(text) -> dict
def dumps_toml(d: dict) -> str        # writer for the subset we use: str/int/float/bool/list-of-scalars, one-level tables
def dump_instance(d, path) -> None
def get(d, dotted, default=None); def set_(d, dotted, value) -> None
def defaults(defn) -> dict            # instance with every knob at its default, recipe=name
def validate(inst, defn) -> list[str] # [] when valid; checks types, enum membership, step bounds, text length, from<to
def parse_time(s) -> float            # "0:02.5" | "2.5" | 2.5 ; raises RecipeError
def fmt_time(seconds) -> str          # "0:02.5"
def apply_knob(inst, defn, knob_idx, value_idx) -> dict   # returns a NEW dict (undo-friendly)
```
`apply_knob` semantics — this is what a button press does:
- enum: `value_idx` indexes `values`.
- step: `value_idx` 0 = decrement, 1 = increment (clamped to min/max).
- range: `value_idx` 0..3 = in −, in +, out −, out +, by `step_s` seconds (in < out always).
- text: not applicable to a callback (the daemon uses force_reply); `set_` directly.

## `clipwright/ffmpeg.py`

```python
class FFmpegError(RuntimeError)      # carries argv and stderr tail
def run(argv: list[str], *, log: list|None = None, timeout=600) -> subprocess.CompletedProcess
@dataclass class Probe: path, duration, width, height, fps, nb_frames, vcodec, has_audio, size_bytes, rotation=0, still=False
def probe(path) -> Probe             # ffprobe -print_format json; raises FFmpegError if not video/still
def is_still(probe) -> bool
def even(n) -> int
def gif_argv(src, out, *, fps, width, colors, from_s=None, to_s=None, overlay_png=None, overlay_pos="bottom", reverse_append=False, dither="bayer") -> list[str]
def mp4_argv(src, out, *, fps, width, from_s=None, to_s=None, overlay_png=None, overlay_pos="bottom", reverse_append=False) -> list[str]
def still_hold_argv(src, out, *, width, fps, duration_s) -> list[str]   # -loop 1 hold of a still
def kenburns_argv(src, out, *, width, height, fps, duration_s, zoom=1.4) -> list[str]  # zoompan; numbers only
def first_frame_argv(src, out) -> list[str]
def image_seq_argv(pattern, out, *, fps) -> list[str]   # frame_%04d.png → H.264
def gifsicle_argv(src, out, *, colors=None, lossy=None, optimize=3) -> list[str]
```
GIF encode is palettegen/paletteuse in one command via `split` (that is the
"two-pass" technique; one process, no palette temp file). `-loop 0` always.
`reverse_append=True` yields the boomerang (`split[a][b];[b]reverse[r];[a][r]concat`).
Trim uses `-ss`/`-to` as input options, positioned before `-i`. Overlay uses
`[0:v][1:v]overlay=0:main_h-overlay_h` (bottom) or `overlay=0:0` (top).

## `clipwright/caption.py`

```python
def render_caption(text, width, *, size=64, color="#ffffff", style="impact-outline", font_path=None, pad=12) -> PIL.Image (RGBA, width x band_h)
def write_caption_overlay(text, width, path, **kw) -> str
def pick_font(bold=True) -> str      # first of: Impact, Anton, Liberation Sans Bold, DejaVu Sans Bold
```
Word-wraps to `width - 2*pad`, black stroke outline; the band is transparent.

## `clipwright/budget.py`

```python
PRESETS = {"telegram": 8_000_000, "discord": 10_000_000, "slack": 5_000_000, "whatsapp-sticker": 500_000, "shorts-9x16": 15_000_000}
def budget_bytes(fits) -> int|None   # preset name, "6", "6MB", 6 -> bytes; None means unbounded
@dataclass class Candidate: width, fps, colors
def ladder(probe, *, max_width=480) -> list[Candidate]   # best-first
def solve(encode: Callable[[Candidate], str], candidates, budget, *, max_attempts=4, log=None, cost=None) -> tuple[Candidate, str, int]
    # cost(cand) -> relative size estimate lets solve() jump straight to the rung predicted to fit; ladder() is uncapped
    # encode(cand) -> path; returns (candidate, path, size). Try best-first; stop at first fit.
def squeeze(path, out, *, lossy=None, colors=None, log=None) -> int   # gifsicle -O3, returns bytes; argv appended to log
```

## `clipwright/loopfind.py`

```python
def frame_diff(a, b) -> float                      # mean abs diff of two same-size grayscale PIL images, 0..255
def best_loop(frames: list[Image], *, window=10) -> tuple[int, int, float]   # (in_idx, out_idx, score)
def degrade(score, *, threshold=18.0) -> str       # "seamless" if score <= threshold else "boomerang"
def sample_frames(path, from_s, to_s, *, fps=10, width=64) -> list[Image]    # ffmpeg rawvideo gray, via ffmpeg.run
```

## Pipelines — `clipwright/pipelines/`

```python
@dataclass class CookContext: workdir, out_dir, stem, proxy: bool = False, log: list = field(default_factory=list)
@dataclass class CookResult: gif: str|None, mp4: str|None, report: dict, argv_log: list[list[str]]
def run(inst: dict, ctx: CookContext) -> CookResult     # one per module: gifify.py, caption_loop.py, boomerang.py, ken_burns.py, typecard.py
PIPELINES = {"gifify": ..., "caption_loop": ..., "boomerang": ..., "ken_burns": ..., "typecard": ...}
```
Proxy mode renders a small (≤ 240 px wide) MP4 fast for previews and skips the
GIF budget search. Full mode: build the ladder, `budget.solve`, then `squeeze`,
and also write an MP4 preview. Both modes work on at most `SEGMENT_CAP_S`
(15 s) of source from the in-point. `report` includes `proxy`, `bytes`, `fits`
(`None` in proxy mode or with `fits = "none"`), `budget`, `width`, `fps`,
`colors`, `duration`, `from`/`to` (as rendered), `capped` (the segment was
cut to the cap), `loop` (the honest label: `seamless`, `boomerang
(degraded)`, `none (crossfade not implemented)` ...), and `loop_score` plus
`loop_nudge_frames` whenever the loop finder ran.

## `clipwright/cook.py`

```python
def cook(inst, *, out_dir, stem=None, proxy=False, cookbook=None, public_input=None) -> CookResult
    # public_input replaces `input` in the sidecar and report["cli"] (the daemon passes the basename so server paths never leak)
    # validate → pipeline → write <stem>.recipe.toml sidecar → return
def cli_command(inst, *, cookbook=None) -> str   # shlex-joined `clipwright cook ...` that reproduces inst
def stem_for(inst) -> str           # slug from recipe + caption text/seed, filesystem-safe, never the input filename
```

## `clipwright/doctor.py` / `clipwright/cli.py`

`doctor.run_checks(home=None) -> list[Check(name, ok, required, detail)]` never
raises (a missing or non-executable binary, an unreadable cookbook file, and a
missing Pillow or tomllib are failed checks — `doctor` and `cli` import the
engine lazily so they load on the machine they diagnose); `doctor.report(home=None)`
prints the table and returns 1 if any required check fails; `doctor.main(argv)`
parses `--home DIR` and calls it. `home` resolves like `clipwrightd`'s: `--home`,
then `$CLIPWRIGHT_HOME`, then a `CLIPWRIGHT_HOME=` line in the default
`bot.env`, then `~/.clipwright` (only that line is consulted; a `bot.env` the
daemon could not read either, non-UTF-8 included, means the default). Optional
checks: libass, Impact, Noto Color Emoji, and `bot.env` (present, mode 0600;
nothing from it is ever printed).

`cook`/`remix` print one line per output — the `fits` mark only on the GIF and
only from `report["fits"]` — then `loop: <report["loop"]>` (with seam score and
out-point nudge), `trimmed to 15 s ...` when `report["capped"]`, then the
`Re-run anytime:` line. Every `ValueError`/`RuntimeError`/`OSError` the engine
raises (`RecipeError` and `FFmpegError` included) becomes one stderr line and
exit 2, as does an `ImportError` for a missing Pillow or tomllib (pointing at
`doctor`). `--from/--to/--seed` set instance fields every recipe has;
`--text/--color/--size/--pos/--loop/--fits` are sugar for the `caption.*`,
`loop` and `fits` knobs and refuse a recipe that does not declare that knob
(`boomerang` has no `loop`). `--color` takes a palette value as `#rrggbb` or
bare `rrggbb`, any case. CLI (argparse, `python3 -m clipwright` or the
`clipwright` console script from pyproject):

```
clipwright cook <recipe> [input] [--text T] [--from T] [--to T] [--loop L] [--fits F] [--seed N]
                [--color C] [--size N] [--pos top|bottom] [--set key=value ...] [--out DIR] [--proxy]
clipwright remix <file.recipe.toml> [--set key=value ...] [--out DIR]
clipwright recipes
clipwright probe <input>
clipwright doctor [--home DIR]
```

## Daemon — `clipwrightd/`

- `keyboards.py`: `encode_cb(session6, knob_idx, value_idx) -> "c/<s>/<k>/<v>"` (asserted ≤ 64 bytes), `encode_text_cb(session6, knob_idx) -> "t/<s>/<k>"` (a text knob's force_reply prompt), `encode_action_cb(session6, action) -> "a/<s>/<action>"` for `ACTIONS = ["undo","cli","export","grid"]`, `encode_noop_cb(session6) -> "n/<s>"` (a label button the daemon acknowledges and ignores), `decode_cb(s) -> Callback(kind, session, ...)` for all four, `build_keyboard(defn, inst, session6) -> list[list[{"text","callback_data"}]]` following the plan's type→row rules (enum: one button per value wrapped 4/row, marking the current one with `•`; step: `[− Label +]` with the current value; range: `[◀◀ ◀ in ▶ ▶▶]`-style rows for in and out; text: a `✎ Label` button that the daemon answers with force_reply). Last row = actions. Pure, no I/O.
- `session.py`: sqlite via stdlib. `Store(path)`: `create_session(user_id, chat_id, recipe, origin_message_id=None) -> token6` (`origin_message_id` is the group message the session answers, None in a DM; it lives in the nullable `sessions.origin_message_id` column — added to an older file on open by `_MIGRATIONS`, next to `ledger.user_id` — and reads back as `Session.origin_message_id`), `get(token)`, `update(token, recipe, message_id=None)` pushes previous recipe onto the undo stack, `undo(token) -> recipe|None`, `ledger_put(file_unique_id, recipe)`, `ledger_get(file_unique_id)`, `ledger_since(since) -> [LedgerEntry]` (newest first), `quota_hit(user_id, per_day) -> bool` (increments), `note_member(user_id)` / `is_member(user_id)` (room-member DM allowlist), `note_group(chat_id)` / `list_groups()` (served chats the daemon has learned), `list_sessions() -> [Session]` (most recently updated first). Tokens are 6 chars from `secrets.token_urlsafe`.
- `api.py`: `BotAPI(token, transport=None)`; `call(method, **params)`; `upload(method, files, **params)` (multipart, stdlib); `get_updates`, `send_message`, `send_animation`, `send_document` (all three take `reply_to_message_id`, and send `allow_sending_without_reply=true` with it so a reply still lands once its target has been deleted; a DM send carries neither field), `edit_message_media`, `send_chat_action`, `answer_callback_query`, `set_my_commands`, `get_me`, `get_chat_member(chat_id, user_id)`, `get_file`, `download_file(file_path, dest, max_bytes)`. `transport(url, data, headers, timeout) -> bytes` is injectable so tests never touch the network. Raises `BotAPIError` on `ok: false`.
- `config.py`: reads `~/.clipwright/bot.env` (KEY=VALUE, whole-line `#` comments, optional `export ` prefix, one pair of surrounding quotes stripped; 0600 enforced with a warning), keys `CLIPWRIGHT_BOT_TOKEN`, `CLIPWRIGHT_OWNER_ID`, `CLIPWRIGHT_FRIEND_IDS` (comma list), `CLIPWRIGHT_GROUP_IDS` (comma list of chat ids → `Config.group_ids`; set, the only groups served; empty, every group the bot is in), optional `CLIPWRIGHT_HOME` override for the state dir (process env wins over the file; `--home` over both), and the optional numeric limits `CLIPWRIGHT_MAX_UPLOAD_BYTES`, `CLIPWRIGHT_MAX_DURATION_S`, `CLIPWRIGHT_MAX_DIM`, `CLIPWRIGHT_PER_DAY_QUOTA`, `CLIPWRIGHT_GUEST_PER_DAY_QUOTA` (kept so old `bot.env` files still load; groups no longer use it as a gate — everyone in the room is trusted), `CLIPWRIGHT_QUEUE_DEPTH`, `CLIPWRIGHT_MAX_USER_BYTES`, `CLIPWRIGHT_RETENTION_DAYS` (defaults in `Config`).
- `queue.py`: `RenderQueue(depth=8, *, inline=False)`; one worker thread (`inline=True` runs each job in the caller's thread at submit time, for tests); `submit(user_id, job: Callable[[], None]) -> position|None` (1-based place in line; None when the user already has a job or the queue is full); `has(user_id)`; `stop(timeout=5.0)`.
- `fetch.py`: Wikimedia Commons image search for a `/gif` intention. `fetch_image(query, dest_dir, *, urlopen=None) -> path` writes a hex-named file (never a web filename); timeouts, `image/*` content-type, `MAX_BYTES`; raises `FetchError` with a one-line reason. `urlopen` is injectable.
- `ops.py`: `python3 -m clipwrightd.ops {deploy REF|restart|status|health}` against a `live-src` checkout under the state dir. `deploy` fetches one remote and checks `REF` out detached (branch names prefer `<remote>/<ref>`). `origin` must be this fork (`clipwright-grok`); a `CLIPWRIGHT_PLAN` URL is refused unless `--force-remote` (name that remote `upstream`). A remote whose URL ends with `clipwright-grok` is chosen over `origin` when both exist, and the status line names it. `--remote` / `$CLIPWRIGHT_OPS_REMOTE` selects by name. Then restarts. Restart sends SIGTERM only to a pid that holds `daemon.pid`'s flock, never starts a second poller on a live lock, and does not signal a live pid that does not hold the lock. `health` exits 1 when the pidfile is missing, the recorded pid is dead, the pid does not hold the lock, or `<home>/offset`'s mtime is older than `--max-age` (default 600s). That mtime is refreshed after every successful `getUpdates`, including an empty batch; the stored integer is still the next update id. A process younger than `--max-age` that has not yet rewritten `offset` is within startup grace. No extra metric file. Git and the daemon are argv lists (`shell=True` is not used). `digest [--since HOURS] [--json PATH]` (default 16 hours) does not poll: it prints a summary and writes `<home>/digest-latest.json` from the pidfile, offset mtime, `list_sessions`, `ledger_since`, and `digest-events.jsonl`. `--json PATH` appends one JSON line. The poller appends that ring (chatter preview ~160 chars, session open, web-seed fail→typecard, render error; 48h retention; size cap; no token, no media). A room outside a non-empty `group_ids` is not recorded.
- `poll.py`: `Daemon(config, api, store, queue, cookbook, cook_fn=None, probe_fn=None, fetch_fn=None)` (engine via `cook_fn`/`probe_fn`, Commons via `fetch_fn`, all injectable); `main(argv)` takes `--home DIR` and `--once`; pidfile lock, offset persistence (rewritten again after every successful `getUpdates` so the file mtime is the last poll that returned; an empty file means no update has been seen and `load_offset` is still None), `username` from `get_me` in `run()` (inside its try; None when it fails: every `/cmd@name` then reads as another bot's, retried at most once per `USERNAME_RETRY_S`); chat routing by `_chat_kind` (`private` → DM, `group`/`supergroup`/`channel` → group, anything else dropped; a room not in a non-empty `group_ids` is dropped, logged once); `_impersonal` (a `sender_chat` in a group, a channel post with no real `from`, a `from.is_bot`, or one of `TELEGRAM_SERVICE_IDS`) is nobody in particular: `/gif` from them in a room gets `ANON_HINT`, anything else is dropped; `_actor` → `(uid, is_guest)` (the allowlist is served everywhere; any other group or channel member is trusted too, `is_guest=False`, and remembered in `room_members` so a later DM works like a friend's; a DM sender is served if allowlisted, already a room member, or `getChatMember` says they still belong to a known served group; anyone else gets allowlist silence); `/start`, `/help`, `/recipes`; **`/gif` is the front door** — seed then foundry: reply to a photo/image document → `ken-burns`; reply to a video/animation → `gifify`; reply to text or `/gif <words>` → Commons still then `ken-burns`, or local `typecard` on `FetchError`; bare `/gif` → a self-aware `typecard` (one of `BARE_CAPTIONS`); in a DM a photo or video without `/gif` still opens a session; in a group or channel only commands are acted on (bare media, chatter and other bots' `/cmd@name` are ignored; an unknown command is answered only when addressed `@us`; ignored non-command text is also a `chatter` preview on `digest-events.jsonl`, and session open / seed-fail / render error are recorded the same way — not a second poller); switching to a `needs_input` recipe without a seed is a one-line toast; everyone pays `per_day_quota` on export only; `_nag` still rate-limits identical texts per `(chat_id, user_id)` when `guest=True` (anonymous `/gif`); callback → `apply_knob` → proxy re-render → `edit_message_media`, for `sess.user_id` alone; `export` → full cook → `send_document` gif + sidecar, ledger the `file_unique_id`; `/remix` as a reply → ledger lookup → new session; everything sent in a room is a reply to the session's `origin_message_id` (or the message that asked). `ALLOWED_UPDATES` includes `channel_post`. `main()` entry via `python3 -m clipwrightd`.

## Tests

`tests/conftest.py` provides `test_clip` (session-scoped synthetic 3 s 320×240
30 fps MP4 made with `ffmpeg -f lavfi testsrc2`) and `tmp_out`. Engine tests
must run offline and finish in well under a minute total. Daemon tests use a
fake transport and never open a socket.
