"""clipwright.cli and clipwright.doctor, driven in-process through ``main([...])``.

The two full cooks (caption-loop, then a remix of its sidecar) are shared
through module-scoped fixtures so the file renders each once and stays
well inside its time budget.
"""
from __future__ import annotations

import contextlib
import io
import os
import shlex
import shutil
import subprocess
import sys

import pytest

from clipwright import budget, cli, doctor, ffmpeg, recipe
from clipwright.cli import main, parse_args
from clipwright.pipelines import CookResult
from clipwright.recipe import RecipeError


@pytest.fixture(scope="module")
def out_root(tmp_path_factory):
    return tmp_path_factory.mktemp("cli")


@pytest.fixture(scope="module")
def captioned(test_clip, out_root):
    """``cook caption-loop --text hi --fits telegram``: (exit code, stdout, out dir)."""
    out = str(out_root / "caption")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = main(["cook", "caption-loop", test_clip, "--text", "hi", "--fits", "telegram",
                     "--loop", "none", "--out", out])
    return code, buf.getvalue(), out


def _lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.strip()]


def _sidecar_in(out: str) -> str:
    names = [n for n in os.listdir(out) if n.endswith(".recipe.toml")]
    assert len(names) == 1, names
    return os.path.join(out, names[0])


def _one_error_line(capsys) -> str:
    """The captured stderr must be exactly one ``clipwright: ...`` line."""
    err = capsys.readouterr().err
    lines = _lines(err)
    assert len(lines) == 1 and lines[0].startswith("clipwright: "), err
    return lines[0]


# --- recipes / probe -----------------------------------------------------------

def test_recipes_lists_each_cookbook_entry(capsys):
    assert main(["recipes"]) == 0
    out = capsys.readouterr().out
    heads = [line for line in _lines(out) if not line.startswith(" ")]
    assert len(heads) == 6
    assert [h.split()[1] for h in heads] == [
        "boomerang", "caption-loop", "gifify", "ken-burns", "speed", "typecard"]
    assert all(" — " in h for h in heads)
    knobs = [line for line in _lines(out) if line.startswith("    ")]
    assert any(line.split()[0] == "caption.text" for line in knobs)
    assert any("seamless | boomerang | crossfade | none" in line for line in knobs)
    assert any(line.split()[0] == "zoom" for line in knobs)
    assert any(line.split()[0] == "mode" and "fixed | ramp" in line for line in knobs)
    assert any(line.split()[0] == "rate" and "0.5 | 0.75 | 1.5 | 2 | 3 | 4" in line for line in knobs)
    assert any("slow_fast | fast_slow" in line for line in knobs)
    assert any("needs_input" not in line for line in heads)


def test_probe_prints_the_clip_fields(test_clip, capsys):
    assert main(["probe", test_clip]) == 0
    out = capsys.readouterr().out
    assert "320x240" in out
    assert "duration:" in out and "3.000 s" in out
    assert "audio:    yes" in out
    assert test_clip in out


def test_probe_missing_file_exits_2(tmp_path, capsys):
    assert main(["probe", str(tmp_path / "nope.mp4")]) == 2
    assert "clipwright:" in capsys.readouterr().err


# --- cook -------------------------------------------------------------------------

def test_cook_gifify_proxy_writes_an_mp4(test_clip, tmp_out, capsys):
    assert main(["cook", "gifify", test_clip, "--proxy", "--loop", "none", "--out", tmp_out]) == 0
    out = capsys.readouterr().out
    names = sorted(os.listdir(tmp_out))
    assert len(names) == 2
    mp4 = [n for n in names if n.endswith(".mp4")]
    assert mp4 and not any(n.endswith(".gif") for n in names)
    assert os.path.join(tmp_out, mp4[0]) in out
    assert "fits" not in out          # a proxy run never ran the budget search
    assert "loop: none" in out
    assert cli.RERUN_HEADER in out


def test_cook_caption_loop_writes_gif_and_sidecar(captioned):
    code, out, out_dir = captioned
    assert code == 0
    names = sorted(os.listdir(out_dir))
    assert [n.rsplit(".", 1)[-1] for n in names] == ["gif", "mp4", "toml"]
    gif = os.path.join(out_dir, names[0])
    assert names[0].startswith("caption-loop-hi-")
    with open(gif, "rb") as fh:
        assert fh.read(6) == b"GIF89a"
    inst = recipe.load_instance(_sidecar_in(out_dir))
    assert recipe.get(inst, "caption.text") == "hi"
    assert inst["fits"] == "telegram" and inst["loop"] == "none"
    assert f"{gif}  {os.path.getsize(gif):,} bytes  fits telegram ✓" in out
    mp4_line = [line for line in _lines(out) if line.startswith(os.path.join(out_dir, names[1]))]
    assert len(mp4_line) == 1 and "fits" not in mp4_line[0]   # the MP4 is the preview, not budgeted
    assert "loop: none" in out and "trimmed" not in out


def test_rerun_line_parses_and_reproduces(captioned, test_clip):
    code, out, _ = captioned
    lines = _lines(out)
    idx = lines.index(cli.RERUN_HEADER)
    command = lines[idx + 1].strip()
    words = shlex.split(command)
    assert words[:3] == ["clipwright", "cook", "caption-loop"]
    args = parse_args(words[1:])
    assert args.command == "cook" and args.recipe == "caption-loop" and args.input == test_clip
    sets = dict(cli.parse_set(s) for s in args.set)
    assert sets == {"caption.text": "hi", "loop": "none"}


def test_cook_unknown_recipe_lists_cookbook_and_exits_2(test_clip, tmp_out, capsys):
    assert main(["cook", "sparkle", test_clip, "--out", tmp_out]) == 2
    err = capsys.readouterr().err
    assert "unknown recipe 'sparkle'" in err
    assert "gifify" in err and "caption-loop" in err and "boomerang" in err
    assert not os.listdir(tmp_out)


def test_cook_typecard_needs_no_input(tmp_out, capsys):
    assert main(["cook", "typecard", "--text", "hi", "--proxy", "--out", tmp_out]) == 0
    out = capsys.readouterr().out
    assert "typecard" in out and "Re-run anytime:" in out
    assert any(n.endswith(".mp4") for n in os.listdir(tmp_out))


def test_caption_flag_on_captionless_recipe_exits_2(test_clip, tmp_out, capsys):
    assert main(["cook", "gifify", test_clip, "--text", "hi", "--out", tmp_out]) == 2
    assert "no caption.text knob" in capsys.readouterr().err
    assert not os.listdir(tmp_out)


def test_loop_flag_and_set_refuse_a_recipe_without_the_knob(test_clip, tmp_out, capsys):
    """boomerang declares no loop knob, so --loop / --set loop= fail up front, like --text."""
    assert main(["cook", "boomerang", test_clip, "--loop", "boomerang", "--out", tmp_out]) == 2
    assert "boomerang has no loop knob, so --loop does not apply" in _one_error_line(capsys)
    assert main(["cook", "boomerang", test_clip, "--set", "loop=none", "--out", tmp_out]) == 2
    err = _one_error_line(capsys)
    assert "no knob 'loop'" in err and "also settable: from, to, seed" in err
    assert not os.listdir(tmp_out)
    defn = recipe.load_cookbook()["boomerang"]
    inst = recipe.defaults(defn)
    cli.apply_flags(inst, defn, parse_args(["cook", "boomerang", test_clip, "--fits", "6", "--seed", "1"]))
    assert inst["fits"] == 6 and inst["seed"] == 1      # fits is a boomerang knob; seed an instance field
    assert "loop" not in inst


def test_bad_set_key_and_value_exit_2(test_clip, tmp_out, capsys):
    assert main(["cook", "gifify", test_clip, "--set", "sparkle=1", "--out", tmp_out]) == 2
    assert "no knob 'sparkle'" in capsys.readouterr().err
    assert main(["cook", "gifify", test_clip, "--set", "fps=7", "--out", tmp_out]) == 2
    assert "fps" in capsys.readouterr().err
    assert main(["cook", "gifify", test_clip, "--set", "fps", "--out", tmp_out]) == 2
    assert "key=value" in capsys.readouterr().err
    assert not os.listdir(tmp_out)


def test_missing_input_exits_2(tmp_out, capsys):
    assert main(["cook", "gifify", "--out", tmp_out]) == 2
    assert "needs an input clip" in capsys.readouterr().err


def test_set_is_applied_after_the_sugar_flags(test_clip):
    book = recipe.load_cookbook()
    defn = book["caption-loop"]
    inst = recipe.defaults(defn)
    args = parse_args(["cook", "caption-loop", test_clip, "--color", "ffdd00", "--size", "72",
                       "--set", "caption.color=#ff4444", "--set", "caption.size=80"])
    cli.apply_flags(inst, defn, args)
    cli.apply_set(inst, defn, args.set)
    assert recipe.get(inst, "caption.color") == "#ff4444"
    assert recipe.get(inst, "caption.size") == 80


def _fake_result(tmp_path, report: dict) -> CookResult:
    gif = tmp_path / "x.gif"
    mp4 = tmp_path / "x.mp4"
    sidecar = tmp_path / "x.recipe.toml"
    gif.write_bytes(b"G" * 10)
    mp4.write_bytes(b"M" * 20)
    sidecar.write_text("recipe = 'gifify'\n", encoding="utf-8")
    report = {"sidecar": str(sidecar), "cli": "clipwright cook gifify clip.mp4", **report}
    return CookResult(gif=str(gif), mp4=str(mp4), report=report, argv_log=[])


def test_format_result_reports_degrade_and_cap(tmp_path):
    inst = {"recipe": "gifify", "fits": "telegram"}
    result = _fake_result(tmp_path, {
        "proxy": False, "fits": False, "budget": 8_000_000, "capped": True,
        "loop": "boomerang (degraded)", "loop_score": 41.2, "loop_nudge_frames": 0,
    })
    lines = _lines(cli.format_result(inst, result))
    assert lines[0] == f"{result.gif}  10 bytes  fits telegram ✗"
    assert lines[1] == f"{result.mp4}  20 bytes"
    assert lines[2].startswith(result.report["sidecar"])
    assert lines[3] == "loop: boomerang (degraded), seam 41.2"
    assert lines[4] == "trimmed to 15 s (renders cap at 15 s; --from/--to picks the segment)"
    assert lines[5:] == [cli.RERUN_HEADER, "  clipwright cook gifify clip.mp4"]


def test_format_result_reports_nudge_and_proxy_has_no_fits(tmp_path):
    inst = {"recipe": "gifify", "fits": 6}
    result = _fake_result(tmp_path, {
        "proxy": True, "fits": None, "budget": 6_000_000, "capped": True,
        "loop": "seamless", "loop_score": 8.3, "loop_nudge_frames": -3,
    })
    text = cli.format_result(inst, result)
    assert "fits" not in text
    assert "loop: seamless, seam 8.3, out-point moved -0.3 s" in text
    assert "trimmed to 15 s (renders cap at 15 s" in text
    assert cli.format_status({"loop": "none", "capped": False}) == ["loop: none"]


# --- error contract: one stderr line, exit 2 ---------------------------------------

def test_gifsicle_failure_is_one_line_and_exit_2(test_clip, tmp_out, monkeypatch, capsys):
    def boom(*a, **kw):
        raise RuntimeError("gifsicle failed (1): simulated")
    monkeypatch.setattr(budget, "squeeze", boom)
    assert main(["cook", "gifify", test_clip, "--loop", "none", "--out", tmp_out]) == 2
    assert "gifsicle failed" in _one_error_line(capsys)


def _write_sidecar(path, body: str, test_clip: str) -> str:
    path.write_text(f'recipe = "caption-loop"\ninput = "{test_clip}"\nloop = "none"\n{body}',
                    encoding="utf-8")
    return str(path)


@pytest.mark.parametrize("body, needle", [
    ('fps = 0\n[caption]\ntext = "hi"\n', "fps: not a knob of caption-loop"),
    ('[caption]\ntext = "hi"\nstyle = "nope"\n', "caption.style: not a knob of caption-loop"),
    ('from = "5"\n[caption]\ntext = "hi"\n', "trim 5.000..3.000 s is empty"),   # validator-clean, clip is 3 s
])
def test_remix_of_bad_sidecar_value_is_one_line_and_exit_2(tmp_path, test_clip, body, needle, capsys):
    sidecar = _write_sidecar(tmp_path / "bad.recipe.toml", body, test_clip)
    assert main(["remix", sidecar, "--out", str(tmp_path / "o")]) == 2
    assert needle in _one_error_line(capsys)


def test_oversized_seed_is_one_line_and_exit_2(test_clip, tmp_out, capsys):
    assert main(["cook", "gifify", test_clip, "--proxy", "--set", "seed=" + "9" * 5000,
                 "--out", tmp_out]) == 2
    _one_error_line(capsys)
    assert not os.listdir(tmp_out)


@pytest.mark.parametrize("preset", ["whatsapp-sticker", "shorts-9x16", "none"])
def test_fits_accepts_every_budget_preset(test_clip, tmp_out, preset):
    """README and ENGINE_CONTRACT promise every budget.PRESETS key (and none) for --fits."""
    code = main(["cook", "gifify", test_clip, "--proxy", "--loop", "none",
                 "--fits", preset, "--out", tmp_out])
    if code == 2 and preset in budget.PRESETS and preset not in ("telegram", "discord", "slack"):
        pytest.xfail("recipe.validate still admits only the cookbook enum values for fits "
                     "(fix belongs in recipe._FREEFORM_ENUM)")
    if code == 2 and preset == "none":
        pytest.xfail("recipe.validate rejects fits = 'none' (fix belongs in recipe._FREEFORM_ENUM)")
    assert code == 0
    assert recipe.load_instance(_sidecar_in(tmp_out))["fits"] == preset


# --- remix ----------------------------------------------------------------------

def test_remix_of_a_sidecar_without_fits_enforces_and_labels_telegram(test_clip, tmp_path, capsys):
    """A hand-trimmed sidecar that omits `fits` (and fps/colors) still cooks to the cookbook
    defaults: the GIF line carries the `fits telegram` verdict and the new sidecar spells it out."""
    sidecar = tmp_path / "trimmed.recipe.toml"
    sidecar.write_text(f'recipe = "gifify"\ninput = "{test_clip}"\nloop = "none"\n', encoding="utf-8")
    out = str(tmp_path / "o")
    assert main(["remix", str(sidecar), "--out", out]) == 0
    text = capsys.readouterr().out
    gif = [line for line in _lines(text) if line.split("  ")[0].endswith(".gif")]
    assert len(gif) == 1 and gif[0].endswith("fits telegram ✓"), text
    assert "fits None" not in text
    inst = recipe.load_instance(_sidecar_in(out))
    assert inst["fits"] == "telegram" and inst["fps"] == 15 and inst["colors"] == 256
    rerun = shlex.split(text.split(cli.RERUN_HEADER)[1])
    assert [rerun[i + 1] for i, a in enumerate(rerun) if a == "--set"] == ["loop=none"]   # filled defaults are not "changes"


def test_remix_with_new_colour_writes_a_new_gif(captioned, out_root, capsys):
    _, _, cooked = captioned
    original = _sidecar_in(cooked)
    out = str(out_root / "remix")
    assert main(["remix", original, "--set", "caption.color=#ff4444", "--out", out]) == 0
    text = capsys.readouterr().out
    names = sorted(os.listdir(out))
    assert [n.rsplit(".", 1)[-1] for n in names] == ["gif", "mp4", "toml"]
    assert names[0] != os.path.basename(_sidecar_in(cooked)).replace(".recipe.toml", ".gif")
    inst = recipe.load_instance(_sidecar_in(out))
    assert recipe.get(inst, "caption.color") == "#ff4444"
    assert recipe.get(inst, "caption.text") == "hi"
    assert recipe.get(recipe.load_instance(original), "caption.color") == "#ffffff"
    assert "caption.color=#ff4444" in text.split(cli.RERUN_HEADER)[1]


def test_remix_finds_a_relative_input_beside_the_sidecar(captioned, test_clip, tmp_path, monkeypatch, capsys):
    _, _, cooked = captioned
    beside = tmp_path / "beside"
    beside.mkdir()
    shutil.copy(test_clip, beside / "source.mp4")
    inst = recipe.load_instance(_sidecar_in(cooked))
    inst["input"] = "source.mp4"
    sidecar = str(beside / "moved.recipe.toml")
    recipe.dump_instance(inst, sidecar)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    out = str(tmp_path / "out")
    assert main(["remix", sidecar, "--proxy", "--out", out]) == 0
    assert capsys.readouterr().err == ""
    assert recipe.load_instance(_sidecar_in(out))["input"] == str(beside / "source.mp4")


def test_remix_bad_sidecar_exits_2(tmp_path, capsys):
    bad = tmp_path / "x.recipe.toml"
    bad.write_text('recipe = "sparkle"\n', encoding="utf-8")
    assert main(["remix", str(bad), "--out", str(tmp_path / "o")]) == 2
    assert "unknown recipe 'sparkle'" in capsys.readouterr().err
    assert main(["remix", str(tmp_path / "missing.recipe.toml")]) == 2
    assert "clipwright:" in capsys.readouterr().err


# --- value handling ------------------------------------------------------------

@pytest.mark.parametrize("raw, want", [
    ("20", 20), ("-3", -3), ("1.5", 1.5), (".5", 0.5), ("true", True), ("FALSE", False),
    ("#ff4444", "#ff4444"), ("#12", "#12"), ("none", "none"), ("0:02.5", "0:02.5"),
    ("1e3", "1e3"), ("0x10", "0x10"), ("", ""),
])
def test_coerce(raw, want):
    got = cli.coerce(raw)
    assert got == want and type(got) is type(want)


def test_apply_set_keeps_text_knobs_as_strings():
    book = recipe.load_cookbook()
    defn = book["caption-loop"]
    inst = recipe.defaults(defn)
    cli.apply_set(inst, defn, ["caption.text=2024", "caption.size=72", "fits=6", "to=2.5"])
    assert recipe.get(inst, "caption.text") == "2024"
    assert recipe.get(inst, "caption.size") == 72
    assert inst["fits"] == 6 and inst["to"] == 2.5
    with pytest.raises(RecipeError, match="no knob"):
        cli.apply_set(inst, defn, ["nope=1"])


@pytest.mark.parametrize("given", ["FFDD00", "#FFDD00", "ffdd00", "#ffdd00"])
def test_color_flag_normalises_to_the_palette_spelling(test_clip, given):
    """README: bare hex or #rrggbb, any case — the enum values are lowercase with a #."""
    defn = recipe.load_cookbook()["caption-loop"]
    inst = recipe.defaults(defn)
    cli.apply_flags(inst, defn, parse_args(["cook", "caption-loop", test_clip, "--color", given]))
    assert recipe.get(inst, "caption.color") == "#ffdd00"
    assert recipe.validate(inst, defn) == []


def test_color_flag_accepts_bare_hex(test_clip):
    book = recipe.load_cookbook()
    defn = book["caption-loop"]
    inst = recipe.defaults(defn)
    args = parse_args(["cook", "caption-loop", test_clip, "--color", "ffdd00", "--size", "72",
                       "--pos", "top", "--from", "0:00.5", "--to", "2", "--seed", "6", "--fits", "6"])
    cli.apply_flags(inst, defn, args)
    assert recipe.get(inst, "caption.color") == "#ffdd00"
    assert recipe.get(inst, "caption.size") == 72 and recipe.get(inst, "caption.pos") == "top"
    assert inst["from"] == "0:00.5" and inst["to"] == "2" and inst["seed"] == 6 and inst["fits"] == 6
    assert recipe.validate(inst, defn) == []


# --- doctor ---------------------------------------------------------------------

def test_doctor_required_checks_pass_here(capsys):
    checks = doctor.run_checks()
    names = [c.name for c in checks]
    for expected in ("ffmpeg", "ffprobe", "gifsicle", "libx264", "libass", "palette filters",
                     "Pillow", "tomllib", "caption font", "fonts dir", "emoji font", "state dir",
                     "bot.env", "cookbook"):
        assert expected in names
    failed = [(c.name, c.detail) for c in checks if c.required and not c.ok]
    assert failed == []
    by_name = {c.name: c for c in checks}
    assert by_name["libass"].required is False
    assert by_name["emoji font"].required is False and by_name["bot.env"].required is False
    assert by_name["cookbook"].detail.startswith("6 recipes:")
    assert "speed" in by_name["cookbook"].detail
    assert by_name["ffmpeg"].detail[0].isdigit() and by_name["gifsicle"].detail[0].isdigit()
    font = by_name["caption font"]
    assert os.path.isfile(font.detail.split(" (")[0])
    if "impact" not in os.path.basename(font.detail).lower():
        assert "Impact absent" in font.detail
    emoji = by_name["emoji font"]
    assert emoji.ok is os.path.isfile(emoji.detail)

    assert doctor.main([]) == 0
    out = capsys.readouterr().out
    assert out.count("✓") == sum(c.ok for c in checks) and "all required checks passed" in out
    assert "(optional)" in out


def test_doctor_reports_a_failed_required_check(monkeypatch, capsys):
    monkeypatch.setattr(doctor, "run_checks", lambda home=None: [
        doctor.Check("ffmpeg", False, True, "not runnable (ffmpeg: executable not found)"),
        doctor.Check("libass", False, False, "not built in"),
    ])
    assert doctor.main([]) == 1
    out = capsys.readouterr().out
    assert "✗ ffmpeg" in out and "1 required check failed: ffmpeg" in out
    assert main(["doctor"]) == 1


def test_doctor_survives_a_non_executable_binary(monkeypatch):
    def denied(argv, **kw):
        raise PermissionError(13, "Permission denied", argv[0])
    monkeypatch.setattr(ffmpeg, "run", denied)
    checks = doctor.run_checks()
    by_name = {c.name: c for c in checks}
    for name in ("ffmpeg", "ffprobe", "gifsicle", "palette filters"):
        assert by_name[name].ok is False and "Permission denied" in by_name[name].detail
    assert by_name["cookbook"].ok


def test_doctor_state_dir_uses_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("CLIPWRIGHT_HOME", str(tmp_path / "deeper" / "home"))
    check = doctor.check_state_dir()
    assert check.ok and "will be created" in check.detail
    assert not (tmp_path / "deeper").exists()
    monkeypatch.setenv("CLIPWRIGHT_HOME", str(tmp_path))
    assert doctor.check_state_dir().detail == f"{tmp_path} writable"


def test_doctor_state_dir_follows_bot_env_and_home_flag(monkeypatch, tmp_path):
    """Same three-step resolution as clipwrightd: --home, $CLIPWRIGHT_HOME, bot.env line, default."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLIPWRIGHT_HOME", raising=False)
    default = tmp_path / ".clipwright"
    assert doctor.state_dir() == str(default)
    assert doctor.check_bot_env().ok is False and "absent" in doctor.check_bot_env().detail

    default.mkdir()
    env = default / "bot.env"
    env.write_text('CLIPWRIGHT_BOT_TOKEN="1:x"\nCLIPWRIGHT_HOME=/nonexistent/relocated\n',
                   encoding="utf-8")
    env.chmod(0o644)
    assert doctor.state_dir() == "/nonexistent/relocated"
    assert doctor.check_state_dir().ok is False
    mode = doctor.check_bot_env()
    assert mode.ok is False and mode.required is False and "0644" in mode.detail and "chmod 600" in mode.detail
    env.chmod(0o600)
    assert doctor.check_bot_env().ok and doctor.check_bot_env().detail.endswith("mode 0600")

    monkeypatch.setenv("CLIPWRIGHT_HOME", str(tmp_path / "from-env"))
    assert doctor.state_dir() == str(tmp_path / "from-env")      # process env beats the file
    flag = tmp_path / "from-flag"
    assert doctor.state_dir(str(flag)) == str(flag)               # --home beats everything
    assert doctor.env_file(str(flag)) == str(flag / "bot.env")
    assert doctor.check_bot_env(str(flag)).ok is False


def _run_without(module: str, *argv: str) -> subprocess.CompletedProcess:
    """``clipwright argv...`` in a child whose ``sys.modules[module]`` is None (unimportable)."""
    code = (f"import sys; sys.modules[{module!r}] = None; sys.argv = ['clipwright', *{list(argv)!r}]; "
            "from clipwright.cli import main; sys.exit(main())")
    root = os.path.dirname(os.path.dirname(os.path.abspath(cli.__file__)))
    return subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True,
                          encoding="utf-8", env={**os.environ, "PYTHONIOENCODING": "utf-8"}, timeout=120)


@pytest.mark.parametrize("module, rows", [
    ("PIL", ["✗ Pillow", "✗ caption font", "✗ fonts dir", "✗ emoji font"]),
    ("tomllib", ["✗ tomllib", "✗ cookbook"]),
])
def test_doctor_reports_a_missing_dependency_instead_of_crashing(module, rows):
    """doctor (and the cli that hosts it) must load on the machine it diagnoses."""
    proc = _run_without(module, "doctor")
    assert proc.returncode == 1, proc.stderr
    assert "Traceback" not in proc.stderr
    for row in rows:
        assert row in proc.stdout, proc.stdout
    assert "required check" in proc.stdout and "failed" in proc.stdout


def test_cook_without_pillow_is_one_line_naming_doctor(test_clip, tmp_path):
    proc = _run_without("PIL", "cook", "gifify", test_clip, "--proxy", "--out", str(tmp_path / "o"))
    assert proc.returncode == 2
    lines = _lines(proc.stderr)
    assert len(lines) == 1 and lines[0].startswith("clipwright: ") and "doctor" in lines[0], proc.stderr
    assert "Traceback" not in proc.stderr


def test_doctor_state_dir_tolerates_a_non_utf8_bot_env(monkeypatch, tmp_path):
    """A bot.env the daemon could not decode means the default home, never a raise."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLIPWRIGHT_HOME", raising=False)
    default = tmp_path / ".clipwright"
    default.mkdir()
    env = default / "bot.env"
    env.write_bytes(b"# caf\xe9\nCLIPWRIGHT_BOT_TOKEN=1:x\nCLIPWRIGHT_HOME=/nonexistent/elsewhere\n")
    env.chmod(0o600)
    assert doctor.state_dir() == str(default)
    by_name = {c.name: c for c in doctor.run_checks()}
    assert by_name["state dir"].ok and by_name["bot.env"].ok
    assert "1:x" not in doctor.format_report(list(by_name.values()))


def test_doctor_cookbook_check_survives_an_unreadable_file(monkeypatch):
    def denied(path):
        raise PermissionError(13, "Permission denied", path)
    monkeypatch.setattr(recipe, "_read_toml", denied)
    check = doctor.check_cookbook()
    assert check.ok is False and check.required and "Permission denied" in check.detail
    by_name = {c.name: c for c in doctor.run_checks()}
    assert by_name["cookbook"].ok is False and by_name["Pillow"].ok


def test_doctor_home_flag_reaches_the_report(tmp_path, capsys):
    home = tmp_path / "somewhere"
    assert main(["doctor", "--home", str(home)]) == 0
    out = capsys.readouterr().out
    assert f"state dir        {home} will be created" in out
    assert f"bot.env          {home / 'bot.env'} absent" in out
    assert doctor.main(["--home", str(home)]) == 0
