---
name: gif-episode
description: Builds a new episode of the CLIPWRIGHT_PLAN gag GIF series — a satirical newsprint "tabloid" or dark 5-scene "ballad" animated GIF about someone in the user's benchod_tg or weir_tg group chats, made entirely from their own real, verbatim quotes. Use this whenever the user asks to make, build, render, or ship a GIF/meme/tabloid/"episode" about a coworker in this project, asks to "do the next one" or advance the series, or pastes a chat excerpt/screenshot and asks for a gif version of it — even if they don't say the word "skill" or name a file format. Also use when the user asks what's next in the queue, wants to check series_state.json, or references a past episode by name (e.g. "alex_01", "the memory leak one", "make a graphs one").
---

# GIF episode

This project runs an ongoing bit: short animated "front page" GIFs that roast
one real coworker at a time, built entirely out of things they actually said
in the group chat. Fifteen-plus episodes exist already — read a couple before
writing a new one. `series_state.json` at the project root is the ledger:
`done` (subject/group/style/file/topic per past episode), `pending` (subjects
queued up but not yet made), `styles`, and `rules`. Read it fresh every run —
don't assume you remember its contents from earlier in the conversation.

## The two ways in

**Queue mode** — the user gives no subject ("do the next one", "make an
episode"). Take `pending[0]`. If `pending` is empty, say so and stop; don't
invent a subject to fill it. Research the subject with the read-only graphiti
tools below.

**Ad hoc mode** — the user hands you a subject plus real material directly:
a pasted chat excerpt, a screenshot's text, a quote they typed. Use *that*
text as your verbatim source instead of a graph search. Still worth one
`search_nodes` lookup on the subject — their existing entity summary usually
has a real fact or two (a running joke, a tool they use, a past episode's
callback) that makes the new one feel continuous with the series instead of
one-off. This is how `ep10_alex.py` picked up Alex's Mac habit and his
slop-stopper bot from his prior entity summary while the "graphs?" quotes
themselves came straight from the screenshot the user pasted in.

Either way, run `scripts/plan_episode.py` first (optionally `--subject
"Name"` for ad hoc mode) — it reads `series_state.json` and the existing
`epNN_*.py` files and hands back the next script filename, the subject's
personal episode number, a gif filename prefix, and which style you should
use. Don't hand-count files or eyeball the JSON for these — that's exactly
the kind of thing a script should do so you don't get an off-by-one wrong on
a run nobody's reviewing before it ships.

If the output has a non-null `warning`, that means queue mode found the
subject already sitting in `done` despite still being in `pending` — the
ledger disagrees with itself. Stop and tell the user what you found instead
of guessing which list is right; don't render an episode on top of an
inconsistency you can't explain.

## Research (read-only, always)

Only these graphiti tools are permitted here, and the project's own
`.claude/settings.local.json` enforces it: `search_memory_facts`,
`search_nodes`, `get_episodes`, `get_entity_edge`, `get_episode_entities`,
`get_status`. Never call `add_memory`, `add_triplet`, `delete_entity_edge`,
`delete_episode`, `clear_graph`, `build_communities`, or `summarize_saga` —
this skill observes the graph, it never writes to it, even to "helpfully"
log the episode you just made back into it.

`get_status` is a health check, not a query. `get_episodes` /
`get_entity_edge` / `get_episode_entities` are for provenance — pull them
when a fact looks off and you want to see exactly which source episode it
came from before you trust it, the way the project's rules ask you to.

## The rules that hold even though nothing checks your work before it ships

This skill runs start to finish without a pause for approval — draft,
render, deliver, update the ledger, done. That's *why* these matter: nobody
is going to catch a bad call before a real person sees themselves in a GIF.

1. **Headlines and pull-quotes are verbatim, always.** From the graph in
   queue mode, from what the user pasted in ad hoc mode. Don't sharpen a
   quote to make the joke land better — the real wording is funnier anyway,
   and an invented line attributed to a real person is a different kind of
   problem than a clumsy one.
2. **Cross-check before you trust a fact.** Graphiti's extraction isn't
   perfect. If `search_memory_facts` surfaces something that would be the
   whole joke, look it up again a second way (`search_nodes` on the entity,
   or `get_episode_entities` on its source) before it goes in the headline.
3. **Some true things aren't for this.** Confidential business data, health,
   immigration status, anything that reads as misconduct — skip it even if
   it's the funniest line in the search results, and extend the same caution
   to close cousins of those categories (drug use, a serious personal-safety
   incident) even when the rules don't spell them out by name. When you skip
   something, name what and why in a one-line docstring note at the top of
   the script — several existing episode scripts do this; grep the project
   for `Deliberately excludes` to see the tone. The note lives in the script,
   never as a softened or coded reference inside the episode itself.
4. **Don't name a third party the graph itself doesn't name.** If a quote
   drags in someone unnamed, keep them unnamed too, or cut the quote.
5. **Alternate style.** `plan_episode.py`'s `recommended_style` already
   checked the last entry in `done` — use it unless the user explicitly asks
   for the other kit by name.
6. **Nothing safe, nothing shipped.** If a subject's material is too thin or
   too sensitive to make a decent, safe episode out of, stop and tell the
   user instead of stretching four words into a headline or reaching for
   rule 3's excluded categories to fill the gap.

## Writing the episode script

Both kits already exist and do the actual drawing — this skill's job is
supplying good content to them, not reimplementing rendering. Read
`tabloid_kit.py` or `ballad_kit.py` (whichever `plan_episode.py` recommended)
for the class you're instantiating. Then read one or two *recent* episode
scripts as worked examples before writing a new one, rather than assuming
any specific filename still exists — check `series_state.json`'s `done`
list (last few entries, filtered to the style you're using) or `ls epNN_*.py`
sorted by number, and open whichever are newest. The series keeps growing,
so a name from today's run will eventually be as stale as any other — the
point is "read something recent," not "read this exact file." If you're
doing ballad, notice that `title()`/`counter()` take an `emblem` callback: a
small subject-relevant doodle the episode script draws itself with raw PIL
calls (a camper for someone's off-grid setup, a broken key icon for a lost
API key) — look at how an existing one is built before inventing your own.

A few things worth knowing that aren't obvious from reading one example:

- Tabloid's masthead (and ballad's title/tagline) should be a fresh pun fit
  to *this* subject and topic — "THE MEMORY LEAK", "THE UNITY GAZETTE", "THE
  RAYMOND REGISTER" are one-offs, not a template with the name swapped in.
- Tabloid's `strips` and `starburst_lines`, and ballad's `dossier` rows, are
  where you can spend a couple of the subject's other real, established
  facts (from their entity summary) as texture around the main quote —
  that's what made one past episode's "still 'mostly' a Mac guy" footer line
  land as a callback instead of filler.
- Keep quote strips short — `tabloid_kit.py`'s layout wraps to two lines and
  silently drops the overflow, so trim a long verbatim quote at a natural
  clause boundary rather than letting the renderer cut it wherever.
- If a kicker or dateline includes episode numbering (some past episodes do,
  e.g. ballad's `kicker="weir_tg · NO. 4"`), use `plan_episode.py`'s
  `global_episode_number`. Past episodes used at least three different
  counters here inconsistently (global, personal, per-group) — there's no
  real convention to match, so this skill picks one and sticks to it.
  Numbering is also entirely optional; plenty of episodes skip it.
- Ballad's `chat()` scene is usually just the subject's own lines — that's
  the norm across most existing episodes. It's fine to include one line from
  another named speaker when the joke genuinely doesn't work without the
  setup (e.g. someone else's message is what the subject is reacting to),
  but keep speaker labels accurate to who actually said each line. If a
  quote's source is unnamed (an unnamed message, an anonymous aside), don't
  invent a name for the `who` field per rule 4 — use a plain description of
  the source instead, like `"group chat"`.

Save the script as `<project_root>/<next_script_name>` from `plan_episode.py`'s
output.

## Rendering and delivering

At the bottom of the script, call `.save(raw_path, final_path)` — `raw_path`
is a throwaway intermediate frame dump, `final_path` is the real output.
Point `raw_path` at *this session's own* scratchpad directory (check your
system prompt for the current path — don't reuse a path from an old episode
script, those are stale sessions' scratchpad dirs and won't exist anymore).
Point `final_path` at `<project_root>/<gif_prefix><slug>.gif`, using the
`gif_prefix` from `plan_episode.py` plus a slug that echoes the masthead you
chose (e.g. prefix `alex_03_` + masthead "THE MEMORY LEAK" → `alex_03_memory_leak.gif`).

Run the script with `python3`. If it errors, fix the script and rerun — don't
hand-edit the gif. Once it succeeds:

1. Send the finished `.gif` to the user as a file.
2. Update `series_state.json`: queue mode moves the subject from `pending`
   into a new `done` entry; ad hoc mode just appends the new `done` entry.
   Match the existing entry shape exactly: `subject`, `group`, `style`,
   `file`, `topic` (a short, punchy description of what the episode's about
   — see existing entries for the tone).
3. Tell the user what you made in a sentence or two — subject, angle, style
   — the way you'd caption it if you were the one posting it.
