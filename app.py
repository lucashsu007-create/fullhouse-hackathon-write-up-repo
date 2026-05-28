"""
app.py — Streamlit dashboard for backtest.py (self-contained orchestration).

Run from the directory that contains this file and backtest.py:

    pip install streamlit
    streamlit run app.py

This version does NOT depend on backtest.py exposing run_eval()/run_ab().
It imports the building blocks that backtest.py defines —
    make_ids, build_decide_map, run_match_inproc, summarize
— and the eval / paired multi-hero orchestration lives in analysis.py (a pure,
streamlit-free module that app.py wires to the harness via analysis.bind_harness).
That mirrors backtest.py's own cmd_eval / cmd_ab logic, match-for-match
(generalising the A-vs-B path to 2–7 heroes), while also returning the richer
result shape this dashboard's Results tab needs (per-match realized + EV deltas,
per-match placement, per-bot crash/timing counts) and threading a per-match
progress callback. Keeping that logic in analysis.py makes it unit-testable with
no engine, eval7, or streamlit present (see test_step2.py).

Two correctness fixes over the previous revision:
  1. Duplicate seats are preserved. A preset like
     sizing_sweep = [min_ball, min_ball, overbet_polar, overbet_polar, ...]
     used to collapse to one of each because the field was routed through a
     st.multiselect (which is set-like). Now a chosen preset feeds its bot_ids
     list straight into the job, duplicates intact, so a 6-bot padded preset
     really seats 6 + the hero. Manual field building (no preset) still uses the
     multiselect, where de-duplication is harmless.
  2. Seat-count guard. The engine asserts 2..9 players, so hero + opponents must
     be <= 9. Oversized fields (e.g. a 10-bot preset) are rejected up front with
     a clear message instead of an opaque AssertionError buried in a result.

The dashboard creates three folders next to itself on first run:
    bots/      bot library  (one subdir per bot, each containing bot.py)
    presets/   named opponent tables (JSON files)
    results/   one JSON per completed backtest run

The engine repo (the one containing engine/game.py) must be importable: run
this from inside the repo, or set the repo path in the sidebar.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import secrets
import sys
import time
import traceback
from pathlib import Path

import streamlit as st

# matplotlib is used only by the per-match delta histogram on Tab 3.
# If it's not installed, the rest of the dashboard still works.
try:
    import matplotlib
    matplotlib.use("Agg")  # headless backend; we hand figures to st.pyplot
    import matplotlib.pyplot as plt
    _HAS_MPL = True
except Exception:
    _HAS_MPL = False


# Engine seat cap (PokerEngine asserts 2 <= players <= MAX_PLAYERS, MAX=9).
MAX_SEATS = 9


# ---------------------------------------------------------------------------
# Paths / session state
# ---------------------------------------------------------------------------

APP_DIR = Path(__file__).parent.resolve()


def _pick_default_bots_dir() -> Path:
    """Choose the bots directory automatically on startup. Tries the user's
    organised WSL project layout first (either via Windows UNC if the dashboard
    is launched from PowerShell/cmd, or via the native Linux path if it's
    launched from inside WSL itself). Falls back to ./bots next to app.py if
    neither is reachable — e.g. running on a different machine."""
    candidates = [
        Path("/home/lucas/projects/fullhouse-hackathon-write-up-repo/bots/custom"),
        Path(r"\\wsl.localhost\Ubuntu\home\lucas\projects"
             r"\fullhouse-hackathon-write-up-repo\bots\custom"),
        APP_DIR / "bots",
    ]
    for c in candidates:
        try:
            if c.is_dir():
                return c
        except (OSError, ValueError):
            continue
    return APP_DIR / "bots"


DEFAULT_BOTS_DIR = _pick_default_bots_dir()
DEFAULT_HEROES_DIR = APP_DIR / "hero_bots"   # uploaded / test bots live here
DEFAULT_PRESETS_DIR = APP_DIR / "presets"
DEFAULT_RESULTS_DIR = APP_DIR / "results"


def _bootstrap_presets(presets_dir) -> None:
    """One-time seed of opponent tables. No-op if presets_dir already has any
    *.json — never overwrites user-defined presets. All lineups are <= 8 bots
    so hero + field fits the engine's 9-seat cap."""
    p = Path(presets_dir)
    if p.exists() and any(p.glob("*.json")):
        return
    p.mkdir(parents=True, exist_ok=True)
    bootstrap = {
        # Diagnostics — each isolates one leak. Padded to 6 by duplication so
        # the table stays full and the probe hits hard.
        "sizing_sweep": ["min_ball", "min_ball", "overbet_polar",
                         "overbet_polar", "check_raiser", "check_raiser"],
        "aggression":   ["balanced_lag", "multi_barrel", "squeezer",
                         "polar_3bettor", "balanced_lag", "multi_barrel"],
        "balanced":     ["competent_tag", "gto_balanced", "cfr_trained",
                         "competent_tag", "gto_balanced", "cfr_trained"],
        "passive":      ["calling_station", "nit_folder", "limper",
                         "mc_pot_odds", "calling_station", "limper"],
        # One adaptive seat only — duplicates would split the read across
        # independent trackers and slow convergence.
        "adaptive":     ["adaptive_exploit", "competent_tag", "balanced_tag",
                         "calling_station", "nit_folder", "balanced_lag"],
        # Composites — natural sizes.
        "real_field":   ["competent_tag", "polar_3bettor", "multi_barrel",
                         "balanced_lag"],
        "gauntlet":     ["competent_tag", "balanced_lag", "multi_barrel",
                         "gto_balanced", "check_raiser", "calling_station"],
        # Legacy / stress, kept for result continuity.
        "mixed_7":       ["simple_tag", "balanced_tag", "trap_tag", "nit_folder",
                          "calling_station", "mc_pot_odds", "perma_jam"],
        "perma_heavy":   ["perma_jam"] * 5 + ["simple_tag"],
        "station_heavy": ["calling_station"] * 5 + ["simple_tag"],
        "folder_heavy":  ["nit_folder"] * 5 + ["simple_tag"],
    }
    now_iso = dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"
    for name, bots in bootstrap.items():
        (p / f"{name}.json").write_text(json.dumps({
            "name": name,
            "created_at": now_iso,
            "bot_ids": list(bots),
            "_bootstrap": True,
        }, indent=2))


def _init_state() -> None:
    ss = st.session_state
    ss.setdefault("repo_path", "")
    ss.setdefault("bots_dir", str(DEFAULT_BOTS_DIR))
    ss.setdefault("heroes_dir", str(DEFAULT_HEROES_DIR))
    ss.setdefault("presets_dir", str(DEFAULT_PRESETS_DIR))
    ss.setdefault("results_dir", str(DEFAULT_RESULTS_DIR))
    ss.setdefault("queue", [])           # list of job dicts
    _bootstrap_presets(ss["presets_dir"])


_init_state()
st.set_page_config(page_title="Fullhouse Backtest Dashboard", layout="wide")


# ---------------------------------------------------------------------------
# Sidebar — paths + engine repo
# ---------------------------------------------------------------------------

with st.sidebar:
    st.header("Settings")
    st.session_state.repo_path = st.text_input(
        "Engine repo path",
        value=st.session_state.repo_path,
        help="Directory containing engine/game.py. Leave blank if this dashboard "
             "lives inside the repo — backtest.py finds it automatically.",
    )
    st.session_state.heroes_dir = st.text_input(
        "Hero bots dir",
        value=st.session_state.heroes_dir,
        help="Where your uploaded / test bots live (the bots you're evaluating). "
             "Kept separate from the opponent pool below.",
    )
    st.session_state.bots_dir = st.text_input(
        "Opponent bots dir",
        value=st.session_state.bots_dir,
        help="The field / preset opponents. Looks for <dir>/<botname>/bot.py "
             "for each bot. Auto-detected on startup; override here if needed.",
    )
    st.session_state.presets_dir = st.text_input("Presets dir", value=st.session_state.presets_dir)
    st.session_state.results_dir = st.text_input("Results dir", value=st.session_state.results_dir)

    for _p in (st.session_state.heroes_dir,
               st.session_state.bots_dir,
               st.session_state.presets_dir,
               st.session_state.results_dir):
        Path(_p).mkdir(parents=True, exist_ok=True)

    def _count_bots(d):
        dp = Path(d)
        if not dp.is_dir():
            return None
        return sum(1 for s in dp.iterdir() if s.is_dir() and (s / "bot.py").is_file()) \
            + sum(1 for f in dp.glob("*.py") if f.name != "__init__.py")

    _nh = _count_bots(st.session_state.heroes_dir)
    _no = _count_bots(st.session_state.bots_dir)
    st.caption(f"Hero bots: {_nh if _nh is not None else '⚠ unreachable'}  ·  "
               f"Opponent bots: {_no if _no is not None else '⚠ unreachable'}")

    st.caption(f"Working dir: `{APP_DIR}`")


# ---------------------------------------------------------------------------
# Import backtest (after the sidebar so the user can set repo_path first)
# ---------------------------------------------------------------------------

_REQUIRED_PRIMITIVES = ("make_ids", "build_decide_map", "run_match_inproc", "summarize")


def _import_backtest():
    if st.session_state.repo_path:
        rp = os.path.abspath(st.session_state.repo_path)
        if rp not in sys.path:
            sys.path.insert(0, rp)
    if str(APP_DIR) not in sys.path:
        sys.path.insert(0, str(APP_DIR))
    try:
        import backtest as bt  # noqa: WPS433  intentional lazy import
    except Exception as exc:
        return exc
    missing = [n for n in _REQUIRED_PRIMITIVES if not hasattr(bt, n)]
    if missing:
        return RuntimeError(
            "backtest.py imported but is missing required primitives: "
            + ", ".join(missing)
            + ". This dashboard drives backtest.py through those functions; "
              "make sure you're pointing at the harness that defines them."
        )
    return bt


_bt = _import_backtest()
if isinstance(_bt, Exception):
    st.title("Fullhouse Backtest Dashboard")
    st.error(
        "Couldn't import `backtest.py` / `engine.game`, or it lacks the needed "
        "primitives.\n\n"
        f"`{type(_bt).__name__}: {_bt}`\n\n"
        "Fix: set the **Engine repo path** in the sidebar, or place this file "
        "next to `backtest.py` inside the fullhouse engine repo."
    )
    st.stop()
bt = _bt

# Wire the pure orchestration / IO / schema module to the harness. analysis.py
# imports no streamlit and never hard-imports backtest, so it stays unit-testable
# in isolation; here we inject the harness primitives + engine constants into it.
import analysis  # noqa: E402  (after the late backtest import, by design)
analysis.bind_harness(bt)


# ---------------------------------------------------------------------------
# Bot library
# ---------------------------------------------------------------------------

# Characters disallowed in a bot id / folder name.
_INVALID_NAME_CHARS = r"\/:*?\"<>| "


def _name_problem(name: str) -> str | None:
    """Reason the name is unusable, or None if it's fine."""
    if not name:
        return "empty name"
    if any(c in name for c in _INVALID_NAME_CHARS):
        return "spaces or path separators not allowed"
    return None


def list_bots(bots_dir: str) -> list[dict]:
    """Return [{id, path}] sorted by id. Looks for:
       - bots_dir/<name>/bot.py  (preferred layout)
       - bots_dir/<name>.py      (flat layout, also accepted)
    """
    base = Path(bots_dir)
    if not base.is_dir():
        return []
    out = {}
    for sub in base.iterdir():
        if sub.is_dir() and (sub / "bot.py").is_file():
            out[sub.name] = str((sub / "bot.py").resolve())
    for f in base.glob("*.py"):
        if f.name == "__init__.py":
            continue
        out.setdefault(f.stem, str(f.resolve()))
    return [{"id": k, "path": out[k]} for k in sorted(out)]


def save_uploaded_bot(bots_dir: str, name: str, file_bytes: bytes) -> Path:
    target_dir = Path(bots_dir) / name
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "bot.py"
    target.write_bytes(file_bytes)
    return target


def delete_bot(bots_dir: str, bot_id: str) -> None:
    """Remove the bot's folder (or its flat .py file)."""
    base = Path(bots_dir)
    sub = base / bot_id
    if sub.is_dir() and (sub / "bot.py").is_file():
        for f in sub.iterdir():
            f.unlink()
        sub.rmdir()
        return
    flat = base / f"{bot_id}.py"
    if flat.is_file():
        flat.unlink()


def render_bot_library(bots_dir: str, *, kind: str, key_prefix: str,
                       empty_hint: str) -> list[dict]:
    """Render a bot library panel (list + delete + multi-file uploader) for one
    pool. `kind` is a human label ('hero'/'opponent'); `key_prefix` keeps widget
    keys and the cross-rerun message slot unique per pool. Returns the bot list."""
    bots = list_bots(bots_dir)

    # Surface the outcome of the previous save across the list-refresh rerun.
    _msg = st.session_state.pop(f"_upload_msg_{key_prefix}", None)
    if _msg:
        if _msg.get("saved"):
            st.success("Saved: " + ", ".join(f"`{n}`" for n in _msg["saved"]))
        if _msg.get("skipped"):
            st.warning("Skipped — " + "; ".join(_msg["skipped"]))

    if not bots:
        st.info(empty_hint)
    else:
        st.write(f"{len(bots)} {kind} bot(s):")
        for b in bots:
            c1, c2, c3 = st.columns([3, 6, 1])
            c1.markdown(f"**{b['id']}**")
            c2.caption(b["path"])
            if c3.button("✕", key=f"del_{key_prefix}_{b['id']}",
                         help=f"Delete this {kind} bot"):
                delete_bot(bots_dir, b["id"])
                st.rerun()

    st.markdown("---")
    st.markdown(f"**Upload {kind} bots**")
    st.caption("Drag and drop one or more `.py` files (or click to browse). "
               "Each bot is named after its filename; the override below "
               "applies only when you upload a single file.")

    up_files = st.file_uploader(
        "bot.py file(s)",
        type="py",
        key=f"upload_files_{key_prefix}",
        accept_multiple_files=True,
    )
    single = len(up_files) == 1
    up_name = st.text_input(
        "Name override (single file only)",
        key=f"upload_name_{key_prefix}",
        placeholder="e.g. shark, nit, my_hero_v3",
        disabled=not single,
        help="Leave blank to use the filename. Ignored when several files "
             "are uploaded at once.",
    )
    if st.button("Save to library", key=f"save_bot_{key_prefix}"):
        if not up_files:
            st.error("Choose at least one .py file.")
        else:
            saved, skipped = [], []
            override = (up_name or "").strip()
            for uf in up_files:
                name = (override if (single and override)
                        else Path(uf.name).stem).strip()
                problem = _name_problem(name)
                if problem:
                    skipped.append(f"{uf.name} ({problem})")
                    continue
                if (Path(bots_dir) / name).exists():
                    skipped.append(f"{uf.name} → `{name}` already exists")
                    continue
                save_uploaded_bot(bots_dir, name, uf.read())
                saved.append(name)
            st.session_state[f"_upload_msg_{key_prefix}"] = {
                "saved": saved, "skipped": skipped,
            }
            st.rerun()

    return bots


# ---------------------------------------------------------------------------
# Presets (named opponent tables)
# ---------------------------------------------------------------------------

def list_presets(presets_dir: str) -> list[dict]:
    base = Path(presets_dir)
    if not base.is_dir():
        return []
    out = []
    for f in sorted(base.glob("*.json")):
        try:
            data = json.loads(f.read_text())
            out.append({
                "name": f.stem,
                "path": str(f),
                "bot_ids": list(data.get("bot_ids") or []),
                "created_at": data.get("created_at"),
            })
        except Exception:
            pass
    return out


def save_preset(presets_dir: str, name: str, bot_ids: list[str]) -> Path:
    p = Path(presets_dir) / f"{name}.json"
    payload = {
        "name": name,
        "created_at": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "bot_ids": list(bot_ids),
    }
    p.write_text(json.dumps(payload, indent=2))
    return p


def delete_preset(presets_dir: str, name: str) -> None:
    p = Path(presets_dir) / f"{name}.json"
    if p.is_file():
        p.unlink()


def resolve_preset_field(preset: dict, bot_ids: list[str],
                         excluded_ids: set) -> tuple[list[str], list[str]]:
    """Filter a preset's raw bot_ids to ones that exist and aren't a hero —
    PRESERVING ORDER AND DUPLICATES. Returns (kept, dropped)."""
    kept, dropped = [], []
    for b in (preset.get("bot_ids") or []):
        if b in bot_ids and b not in excluded_ids:
            kept.append(b)
        else:
            dropped.append(b)
    return kept, dropped


# ---------------------------------------------------------------------------
# Jobs / queue
# ---------------------------------------------------------------------------

def _new_job_id() -> str:
    return (dt.datetime.utcnow().strftime("%Y%m%dT%H%M%S")
            + "_" + secrets.token_hex(3))


def _now_iso() -> str:
    return dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _base_job(mode: str, label: str, preset_name: str | None, params: dict) -> dict:
    return {
        "job_id": _new_job_id(),
        "label": label,
        "mode": mode,                  # "eval" | "ab"
        "status": "pending",           # pending | running | done | error
        "preset_name": preset_name,
        "params": dict(params),
        "created_at": _now_iso(),
        "started_at": None,
        "completed_at": None,
        "elapsed_s": None,
        "result_path": None,
        "error": None,
    }


def make_eval_job(label, hero, opponents, preset_name, params) -> dict:
    if not label:
        label = f"{hero['id']} vs {len(opponents)} opp(s)"
    job = _base_job("eval", label, preset_name, params)
    job["hero"] = hero
    job["opponents"] = opponents
    return job


def make_ab_job(label, heroes, baseline, opponents, preset_name, params) -> dict:
    """Paired comparison of 2..7 heroes against a shared field. `heroes` is a
    list of {id, path}; `baseline` is the id everyone else is measured against."""
    if not label:
        ids = ", ".join(h["id"] for h in heroes)
        label = (f"{len(heroes)}-way: {ids} (base {baseline}) "
                 f"on {len(opponents)} field")
    job = _base_job("ab", label, preset_name, params)
    job["heroes"] = heroes
    job["baseline"] = baseline
    job["opponents"] = opponents
    return job


def run_job(job, results_dir, progress_callback=None) -> None:
    """Execute one job. Always writes a result JSON, even on failure. The actual
    orchestration + result IO live in analysis.py (pure, streamlit-free)."""
    job["status"] = "running"
    job["started_at"] = _now_iso()
    try:
        params = job["params"]
        if job["mode"] == "eval":
            result = analysis._run_eval(
                hero_path=job["hero"]["path"],
                opponent_paths=[o["path"] for o in job["opponents"]],
                matches=params["matches"],
                hands=params["hands"],
                seed_base=params["seed_base"],
                budget=params["budget"],
                fold_on_timeout=params["fold_on_timeout"],
                reload=params["reload"],
                rotate_seats=params["rotate_seats"],
                progress_callback=progress_callback,
            )
        elif job["mode"] == "ab":
            result = analysis._run_multi(
                hero_specs=job["heroes"],
                baseline_id=job["baseline"],
                field_paths=[o["path"] for o in job["opponents"]],
                matches=params["matches"],
                hands=params["hands"],
                seed_base=params["seed_base"],
                budget=params["budget"],
                fold_on_timeout=params["fold_on_timeout"],
                progress_callback=progress_callback,
            )
        else:
            raise ValueError(f"Unknown job mode: {job['mode']!r}")

        job["completed_at"] = _now_iso()
        job["elapsed_s"] = result["elapsed_s"]
        out_path = analysis.write_result_json(job, results_dir, result, None)
        job["result_path"] = str(out_path)
        job["status"] = "done"
    except Exception as exc:
        err = {
            "type": type(exc).__name__,
            "message": str(exc),
            "traceback": traceback.format_exc(),
        }
        job["completed_at"] = _now_iso()
        job["error"] = err
        out_path = analysis.write_result_json(job, results_dir, None, err)
        job["result_path"] = str(out_path)
        job["status"] = "error"


def _fmt_dur(seconds) -> str:
    if seconds is None or seconds < 0 or seconds != seconds:  # NaN guard
        return "?"
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        m, s = divmod(seconds, 60)
        return f"{m}m {s:02d}s"
    h, rem = divmod(seconds, 3600)
    m, _ = divmod(rem, 60)
    return f"{h}h {m:02d}m"


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

st.title("Fullhouse Backtest Dashboard")
st.caption("Drives `backtest.py` in-process. Sequential run queue, one JSON per run.")

tab_setup, tab_run, tab_results = st.tabs(
    ["1 · Bots & Presets", "2 · Configure & Queue", "3 · Results"]
)


# ---------------------------------------------------------------------------
# Tab 1 — Bots & Presets
# ---------------------------------------------------------------------------

with tab_setup:
    col_bots, col_presets = st.columns(2)

    # ----- Hero & opponent libraries -----
    with col_bots:
        st.subheader("Hero bots")
        st.caption("Your uploaded / test bots — the ones you evaluate. "
                   "Selected as heroes in Tab 2.")
        hero_bots = render_bot_library(
            st.session_state.heroes_dir,
            kind="hero",
            key_prefix="hero",
            empty_hint=(f"No hero bots in `{st.session_state.heroes_dir}` yet. "
                        "Upload below — this is where your test bots live."),
        )

        st.markdown("---")
        st.subheader("Opponent bots")
        st.caption("The field the heroes are tested against. Presets are built "
                   "from these.")
        opp_bots = render_bot_library(
            st.session_state.bots_dir,
            kind="opponent",
            key_prefix="opp",
            empty_hint=(f"No opponent bots in `{st.session_state.bots_dir}` yet. "
                        "Upload below, or drop bot folders in directly."),
        )

    # ----- Presets -----
    with col_presets:
        st.subheader("Opponent presets")
        presets = list_presets(st.session_state.presets_dir)
        if not presets:
            st.info("No presets saved yet.")
        else:
            st.write(f"{len(presets)} preset(s):")
            for p in presets:
                seats = len(p["bot_ids"]) + 1
                flag = "  ⚠ too big" if seats > MAX_SEATS else ""
                c1, c2, c3 = st.columns([3, 6, 1])
                c1.markdown(f"**{p['name']}**  \n`{seats} seats{flag}`")
                c2.caption(", ".join(p["bot_ids"]) or "(empty)")
                if c3.button("✕", key=f"del_preset_{p['name']}", help="Delete preset"):
                    delete_preset(st.session_state.presets_dir, p["name"])
                    st.rerun()

        st.markdown("---")
        st.markdown("**Save a new preset**")
        st.caption("Presets are opponent tables — members are opponent bots. "
                   "Duplicates are allowed and preserved as separate seats; add a "
                   "bot twice to pad a table.")
        bot_ids = [b["id"] for b in opp_bots]
        new_preset_name = st.text_input(
            "Preset name",
            key="new_preset_name",
            placeholder="e.g. mixed, trappy, nit",
        )
        # multiselect can't express duplicates, so offer an explicit "repeat"
        # control for padding a probe lineup.
        new_preset_bots = st.multiselect(
            "Bots in this preset (unique members)",
            options=bot_ids,
            key="new_preset_bots",
        )
        repeat_n = st.number_input(
            "Repeat each selected bot ×N (1 = no padding)",
            min_value=1, max_value=8, value=1, step=1, key="new_preset_repeat",
            help="e.g. pick min_ball + overbet_polar + check_raiser and ×2 to "
                 "build the padded sizing_sweep lineup (6 seats).",
        )
        if st.button("Save preset", key="save_preset"):
            name = (new_preset_name or "").strip()
            expanded = [b for b in new_preset_bots for _ in range(int(repeat_n))]
            if not name:
                st.error("Give the preset a name.")
            elif any(c in name for c in r"\/:*?\"<>| "):
                st.error("Preset name can't contain spaces or path separators.")
            elif not new_preset_bots:
                st.error("Pick at least one bot.")
            elif len(expanded) + 1 > MAX_SEATS:
                st.error(f"{len(expanded)} bots + hero = {len(expanded)+1} seats "
                         f"exceeds the engine max of {MAX_SEATS}. Trim the lineup.")
            else:
                save_preset(st.session_state.presets_dir, name, expanded)
                st.success(f"Saved preset `{name}` ({len(expanded)} seats of bots).")
                st.rerun()


# ---------------------------------------------------------------------------
# Tab 2 — Configure & Queue
# ---------------------------------------------------------------------------

with tab_run:
    hero_bots = list_bots(st.session_state.heroes_dir)
    opp_bots = list_bots(st.session_state.bots_dir)
    presets = list_presets(st.session_state.presets_dir)
    hero_ids = [b["id"] for b in hero_bots]
    opp_ids = [b["id"] for b in opp_bots]
    hero_by_id = {b["id"]: b for b in hero_bots}
    opp_by_id = {b["id"]: b for b in opp_bots}

    st.subheader("New run")
    if not hero_bots and not opp_bots:
        st.warning("Add a hero bot and at least one opponent bot in Tab 1 first.")
    elif not hero_bots:
        st.warning("Add at least one **hero bot** (your test bot) in Tab 1 first.")
    elif not opp_bots:
        st.warning("Add at least one **opponent bot** in Tab 1 first.")
    else:
        # ----- Mode -----
        mode = st.radio(
            "Mode",
            options=["eval", "ab"],
            horizontal=True,
            key="cfg_mode",
            format_func=lambda m: "eval (1 hero vs field)" if m == "eval"
                                  else "ab (paired comparison of 2–7 heroes)",
            help=("eval: one hero against a field, ranked by EV. "
                  "ab: 2–7 hero variants on identical seeds + same seat per match, "
                  "each paired against a chosen baseline (paired t-stat)."),
        )

        # ----- Hero(es): chosen from the hero-bots pool -----
        if mode == "eval":
            hero_id = st.selectbox("Hero", options=hero_ids, key="cfg_hero")
            hero_choices = []
            baseline_id = None
        else:
            hero_choices = st.multiselect(
                "Heroes (2–7) — compared head-to-head on identical paired matches",
                options=hero_ids,
                key="cfg_heroes",
                help="Each hero plays its own match against the same field, in the "
                     "same seat, on the same seed. The 9-seat engine cap applies to "
                     "the field + 1 hero, so the number of heroes is unrestricted "
                     "(capped at 7 here for sanity).",
            )
            if len(hero_choices) > 7:
                st.warning("Pick at most 7 heroes. Extra selections will be ignored.")
                hero_choices = hero_choices[:7]
            baseline_id = (
                st.selectbox(
                    "Baseline (every other hero is measured against this one)",
                    options=hero_choices,
                    key="cfg_baseline",
                )
                if hero_choices else None
            )
            hero_id = None

        # Heroes and opponents are separate pools, so nothing is excluded from
        # the field on account of being a hero.
        excluded_ids: set = set()

        # ----- Field: preset (authoritative, dups preserved) OR manual -----
        preset_names = [""] + [p["name"] for p in presets]
        opps_label = "Opponents" if mode == "eval" else "Field"
        preset_choice = st.selectbox(
            f"Preset ({opps_label.lower()} source)",
            options=preset_names,
            key="cfg_preset",
            format_func=lambda s: "(none) — build manually below" if s == "" else s,
        )

        dropped_from_preset: list[str] = []
        if preset_choice:
            preset = next((p for p in presets if p["name"] == preset_choice), None)
            effective_field, dropped_from_preset = (
                resolve_preset_field(preset, opp_ids, excluded_ids)
                if preset else ([], [])
            )
            st.caption(
                f"Field is taken straight from preset **{preset_choice}** — "
                "duplicate seats are preserved. Pick “(none)” to build a field by hand."
            )
            # Show the manual picker disabled, as a read-only hint of members.
            st.multiselect(
                f"{opps_label} (from preset — read-only)",
                options=sorted(set(effective_field)),
                default=sorted(set(effective_field)),
                key="cfg_opponents_ro",
                disabled=True,
            )
        else:
            effective_field = st.multiselect(
                opps_label,
                options=opp_ids,
                key="cfg_opponents",
                help="Manual field from the opponent pool. (A multiselect can't "
                     "hold duplicates — to pad with repeats, save a preset in Tab 1 "
                     "and pick it here.)",
            )

        # Field preview + seat-count guard.
        n_seats = len(effective_field) + 1
        seat_ok = (1 <= len(effective_field)) and (n_seats <= MAX_SEATS)
        preview = ", ".join(effective_field) if effective_field else "(empty)"
        st.caption(f"Effective {opps_label.lower()}: {preview}  ·  **{n_seats} seats** "
                   f"incl. hero")
        if dropped_from_preset:
            st.caption("Dropped from preset (not found in the opponent pool): "
                       + ", ".join(sorted(set(dropped_from_preset))))
        if effective_field and n_seats > MAX_SEATS:
            st.warning(f"{n_seats} seats exceeds the engine cap of {MAX_SEATS}. "
                       f"Use at most {MAX_SEATS - 1} {opps_label.lower()}.")

        # ----- Params -----
        st.markdown("**Parameters**")
        pc1, pc2, pc3 = st.columns(3)
        matches = pc1.number_input("Matches", min_value=1, value=100, step=10, key="cfg_matches")
        hands = pc2.number_input("Hands per match", min_value=1, value=400, step=20, key="cfg_hands")
        seed_base = pc3.number_input("Seed base", value=0, step=1, key="cfg_seed")

        pc4, pc5, pc6 = st.columns(3)
        budget = pc4.number_input(
            "Per-action budget (s)", min_value=0.01, value=2.0, step=0.1,
            format="%.2f", key="cfg_budget",
        )
        fold_on_timeout = pc5.checkbox(
            "Fold on timeout", value=True, key="cfg_fot",
            help="Auto-fold actions over the budget (matches tournament rules).",
        )
        if mode == "eval":
            reload_each = pc6.checkbox(
                "Reload bots per match", value=True, key="cfg_reload",
                help="Fresh module load per match — required for stateful bots "
                     "(e.g. adaptive_exploit) to reset between matches.",
            )
            rotate = st.checkbox(
                "Deterministic seat rotation",
                value=False, key="cfg_rotate",
                help="Cycle the seating order each match instead of pseudo-random shuffle.",
            )
        else:
            pc6.caption("ab mode always reloads bots and uses paired seating.")
            reload_each = True
            rotate = False

        label = st.text_input(
            "Run label (optional)",
            key="cfg_label",
            placeholder=(f"e.g. {hero_id} vs {preset_choice or 'field'}"
                         if mode == "eval"
                         else (f"e.g. {len(hero_choices)}-way on "
                               f"{preset_choice or 'field'}")),
        )

        params = {
            "matches": int(matches),
            "hands": int(hands),
            "seed_base": int(seed_base),
            "budget": float(budget),
            "fold_on_timeout": bool(fold_on_timeout),
        }
        if mode == "eval":
            params["reload"] = bool(reload_each)
            params["rotate_seats"] = bool(rotate)

        # ----- Add single job -----
        if st.button("Add to queue", type="primary", key="add_to_queue"):
            if not effective_field:
                st.error(f"Pick at least one {'opponent' if mode == 'eval' else 'field bot'}.")
            elif n_seats > MAX_SEATS:
                st.error(f"{n_seats} seats exceeds the engine cap of {MAX_SEATS}. "
                         f"Trim to ≤{MAX_SEATS - 1} {opps_label.lower()}.")
            elif mode == "ab" and len(hero_choices) < 2:
                st.error("Pick at least 2 heroes to compare.")
            else:
                opponents = [opp_by_id[b] for b in effective_field]  # dups kept
                if mode == "eval":
                    job = make_eval_job(
                        label=label or "",
                        hero=hero_by_id[hero_id],
                        opponents=opponents,
                        preset_name=preset_choice or None,
                        params=params,
                    )
                else:
                    job = make_ab_job(
                        label=label or "",
                        heroes=[hero_by_id[h] for h in hero_choices],
                        baseline=baseline_id,
                        opponents=opponents,
                        preset_name=preset_choice or None,
                        params=params,
                    )
                st.session_state.queue.append(job)
                st.success(f"Queued: {job['label']}  (id `{job['job_id']}`).")

        # ----- Batch sweep -----
        with st.expander("Batch sweep across presets"):
            st.caption(
                "Queue one job per selected preset (duplicates preserved). Uses "
                "the current mode, hero(es), and parameters above. Each preset's "
                "bots become that job's "
                + ("opponents." if mode == "eval" else "field.")
            )
            if not presets:
                st.info("No presets saved yet. Create some in Tab 1 first.")
            else:
                sweep_choices = st.multiselect(
                    "Presets to sweep",
                    options=[p["name"] for p in presets],
                    key="sweep_presets",
                )
                sweep_label_prefix = st.text_input(
                    "Label prefix (optional)",
                    key="sweep_label_prefix",
                    placeholder="e.g. v9_vs_v8.1",
                    help="Appended with ' · <preset>' for each queued job.",
                )
                disabled = (
                    not sweep_choices
                    or (mode == "ab" and len(hero_choices) < 2)
                )
                if st.button(
                    f"Queue sweep across {len(sweep_choices)} preset(s)",
                    disabled=disabled,
                    key="queue_sweep",
                ):
                    added = 0
                    skipped_empty, skipped_big = [], []
                    for pname in sweep_choices:
                        preset = next((p for p in presets if p["name"] == pname), None)
                        if not preset:
                            continue
                        field_ids, _drop = resolve_preset_field(
                            preset, opp_ids, excluded_ids)
                        if not field_ids:
                            skipped_empty.append(pname)
                            continue
                        if len(field_ids) + 1 > MAX_SEATS:
                            skipped_big.append(pname)
                            continue
                        opps = [opp_by_id[b] for b in field_ids]  # dups kept
                        sweep_label = (f"{sweep_label_prefix} · {pname}"
                                       if sweep_label_prefix else f"sweep:{pname}")
                        if mode == "eval":
                            j = make_eval_job(
                                label=sweep_label, hero=hero_by_id[hero_id],
                                opponents=opps, preset_name=pname, params=params,
                            )
                        else:
                            j = make_ab_job(
                                label=sweep_label,
                                heroes=[hero_by_id[h] for h in hero_choices],
                                baseline=baseline_id,
                                opponents=opps, preset_name=pname, params=params,
                            )
                        st.session_state.queue.append(j)
                        added += 1
                    msg = f"Queued {added} job(s) from sweep."
                    if skipped_empty:
                        msg += (" Skipped (no opponents found in pool): "
                                + ", ".join(skipped_empty) + ".")
                    if skipped_big:
                        msg += (f" Skipped (> {MAX_SEATS} seats): "
                                + ", ".join(skipped_big) + ".")
                    st.success(msg)

    # ----- Queue table -----
    st.markdown("---")
    st.subheader("Queue")

    q = st.session_state.queue
    if not q:
        st.caption("Queue is empty.")
    else:
        rows = []
        for j in q:
            if j["mode"] == "eval":
                hero_repr = j["hero"]["id"]
            else:
                hero_repr = " vs ".join(
                    h["id"] + ("*" if h["id"] == j.get("baseline") else "")
                    for h in j["heroes"]
                ) + "  (* = baseline)"
            rows.append({
                "mode": j["mode"],
                "status": j["status"],
                "label": j["label"],
                "hero(es)": hero_repr,
                "seats": len(j["opponents"]) + 1,
                ("opponents" if j["mode"] == "eval" else "field"):
                    ", ".join(o["id"] for o in j["opponents"]),
                "preset": j["preset_name"] or "",
                "matches": j["params"]["matches"],
                "hands": j["params"]["hands"],
                "elapsed_s": j["elapsed_s"],
                "job_id": j["job_id"],
            })
        st.dataframe(rows, use_container_width=True, hide_index=True)

        qc1, qc2, qc3 = st.columns([1, 1, 1])
        pending_n = sum(1 for j in q if j["status"] == "pending")
        run_clicked = qc1.button(
            f"▶ Run all pending ({pending_n})",
            type="primary",
            disabled=pending_n == 0,
            key="run_all",
        )
        if qc2.button("Remove pending", key="clear_pending"):
            st.session_state.queue = [j for j in q if j["status"] != "pending"]
            st.rerun()
        if qc3.button("Clear entire queue", key="clear_all"):
            st.session_state.queue = []
            st.rerun()

        if run_clicked:
            pending = [j for j in q if j["status"] == "pending"]
            queue_start = time.time()
            total_matches = sum(j["params"]["matches"] for j in pending)
            qstate = {"completed_prior": 0, "current_done": 0}

            overall = st.progress(
                0.0,
                text=(f"0/{len(pending)} jobs  ·  "
                      f"0/{total_matches} matches  ·  elapsed 0s"),
            )

            for k, job in enumerate(pending):
                qstate["current_done"] = 0
                with st.status(f"Running: {job['label']}", expanded=True) as status:
                    inner = st.progress(0.0)
                    info = st.empty()
                    start = time.time()

                    def _cb(done, total,
                            _info=info, _inner=inner, _start=start,
                            _qstart=queue_start, _qstate=qstate,
                            _total_matches=total_matches,
                            _job_idx=k, _n_jobs=len(pending),
                            _overall=overall):
                        now = time.time()
                        job_elapsed = now - _start
                        job_rate = done / max(job_elapsed, 1e-9)
                        job_eta = ((total - done) / job_rate) if job_rate > 0 else None
                        _inner.progress(
                            done / total,
                            text=(f"match {done}/{total}  ·  "
                                  f"elapsed {_fmt_dur(job_elapsed)}  ·  "
                                  f"ETA {_fmt_dur(job_eta)}"),
                        )
                        _info.caption(f"{job_rate:.2f} matches/s")
                        _qstate["current_done"] = done
                        global_done = _qstate["completed_prior"] + done
                        q_elapsed = now - _qstart
                        q_rate = global_done / max(q_elapsed, 1e-9)
                        q_remaining = max(_total_matches - global_done, 0)
                        q_eta = (q_remaining / q_rate) if q_rate > 0 else None
                        _overall.progress(
                            global_done / max(_total_matches, 1),
                            text=(f"job {_job_idx+1}/{_n_jobs}  ·  "
                                  f"{global_done}/{_total_matches} matches  ·  "
                                  f"elapsed {_fmt_dur(q_elapsed)}  ·  "
                                  f"ETA {_fmt_dur(q_eta)}"),
                        )

                    run_job(job, st.session_state.results_dir, progress_callback=_cb)

                    qstate["completed_prior"] += qstate["current_done"]

                    if job["status"] == "done":
                        status.update(
                            label=(f"✓ {job['label']}  "
                                   f"({_fmt_dur(job['elapsed_s'])}, result: "
                                   f"{Path(job['result_path']).name})"),
                            state="complete",
                        )
                    else:
                        status.update(
                            label=f"✗ {job['label']} — {job['error']['message']}",
                            state="error",
                        )

            total_queue_elapsed = time.time() - queue_start
            overall.progress(
                1.0,
                text=(f"{len(pending)}/{len(pending)} jobs  ·  "
                      f"elapsed {_fmt_dur(total_queue_elapsed)}"),
            )
            st.success(f"Queue finished in {_fmt_dur(total_queue_elapsed)}.")
            st.rerun()


# ---------------------------------------------------------------------------
# Tab 3 — Results: stats engine + hero × preset matrix (Step 3)
# ---------------------------------------------------------------------------
# All stats / grouping / matrix logic lives in analysis.py (pure, streamlit-
# free, unit-tested). Everything below is rendering only. The matrix is the
# centerpiece: rows = non-baseline heroes, cols = probes (preset / field
# signature), each cell a paired (hero − baseline) stat recomputed live from the
# stored per-match series for the selected series + baseline + metric. The
# eval-mode inspector further down is unchanged.

# Series the user can compute the matrix / tables on. EV is the default dev
# metric (lower variance, unbiased); placement is the finals-oriented series.
_SERIES_OPTIONS = [
    ("ev", "EV-adjusted"),
    ("realized", "Realized"),
    ("placement", "Placement"),
]
_SERIES_LABEL = dict(_SERIES_OPTIONS)


def _series_available(result, series_key) -> bool:
    return bool(analysis.series_arrays(result, series_key))


def _fmt_metric(value, metric) -> str:
    """Display string for a matrix cell value under the chosen metric."""
    if value is None or (isinstance(value, float) and value != value):
        return "·"
    if metric == "bb/100":
        return f"{value:+.2f}"
    if metric == "chips":
        return f"{value:+.0f}"
    if metric == "t":
        return f"{value:+.2f}"
    if metric == "p":
        return f"{value:.3f}"
    return str(value)


def _cell_bg(value, metric, vmax) -> str:
    """Diverging background centered at 0 (matplotlib if present). For p we use a
    light sequential shade (smaller p = stronger). Empty when no matplotlib or
    no value."""
    if not _HAS_MPL or value is None or (isinstance(value, float) and value != value):
        return ""
    try:
        if metric == "p":
            # smaller p → stronger highlight (sequential, reversed)
            frac = max(0.0, min(1.0, 1.0 - float(value)))
            r, g, b, _ = matplotlib.colormaps["Purples"](0.15 + 0.6 * frac)
        else:
            if not vmax or vmax != vmax:
                return ""
            norm = max(-1.0, min(1.0, float(value) / vmax))
            # higher = better → blue end; lower = worse → red end
            r, g, b, _ = matplotlib.colormaps["RdBu"](0.5 + 0.5 * norm)
        return f"background-color: rgb({int(r*255)},{int(g*255)},{int(b*255)});"
    except Exception:
        return ""


def _text_color_for(bg_style: str) -> str:
    """Pick black/white text for contrast against an 'rgb(r,g,b)' bg style."""
    if not bg_style:
        return ""
    try:
        nums = bg_style.split("rgb(", 1)[1].split(")", 1)[0].split(",")
        r, g, b = (int(x) for x in nums)
        lum = 0.299 * r + 0.587 * g + 0.114 * b
        return "color: #000;" if lum > 140 else "color: #fff;"
    except Exception:
        return ""


def _render_matrix_html(matrix_by_probe, hero_rows, probes, baseline,
                        metric, series_key) -> str:
    """Hand-built HTML table for the hero × probe matrix with diverging color +
    Holm markers. matrix_by_probe[probe] = {hero: {'value','holm_p','significant'}}.
    Color is driven by analysis.matrix_color_value so the 'better' direction is
    always the positive (blue) end — on placement (lower rank = better) the sign
    is inverted, so the displayed Δ rank can be negative while colored 'better'."""
    # Global max-abs over the SIGN-ADJUSTED color values (== abs(value)).
    vals = []
    for probe in probes:
        for hid in hero_rows:
            c = matrix_by_probe.get(probe, {}).get(hid)
            if c and isinstance(c.get("value"), (int, float)) and c["value"] == c["value"]:
                vals.append(abs(c["value"]))
    vmax = max(vals) if vals else 0.0

    head = "".join(f"<th style='padding:4px 8px;text-align:right;'>{p}</th>"
                   for p in probes)
    rows_html = []
    for hid in hero_rows:
        tds = [f"<td style='padding:4px 8px;font-weight:600;white-space:nowrap;'>"
               f"{hid} − {baseline}</td>"]
        for probe in probes:
            c = matrix_by_probe.get(probe, {}).get(hid)
            if not c:
                tds.append("<td style='padding:4px 8px;text-align:right;color:#999;'>·</td>")
                continue
            color_val = analysis.matrix_color_value(c.get("value"), series_key)
            bg = _cell_bg(color_val, metric, vmax)
            txt = _text_color_for(bg)
            mark = " <sup>✲</sup>" if c.get("significant") else ""
            tds.append(
                f"<td style='padding:4px 8px;text-align:right;{bg}{txt}'>"
                f"{_fmt_metric(c.get('value'), metric)}{mark}</td>")
        rows_html.append("<tr>" + "".join(tds) + "</tr>")
    return (
        "<table style='border-collapse:collapse;font-size:0.9rem;'>"
        f"<thead><tr><th style='padding:4px 8px;text-align:left;'>"
        f"hero − {baseline} ({_SERIES_LABEL.get(series_key, series_key)})</th>"
        f"{head}</tr></thead><tbody>{''.join(rows_html)}</tbody></table>"
    )


def _render_comparison_matrix(all_results):
    """The Step-3 centerpiece: comparison-set selector → series / baseline /
    metric toggles → live hero × probe paired matrix + collapsed absolute
    companion. `all_results` is a list of normalized result dicts."""
    groups = analysis.group_results(all_results)
    if not groups:
        st.info("No paired (ab) results with a hero set yet — run a 2–7 hero "
                "comparison to populate the matrix.")
        return

    # Default to the most-recent set; selector switches sets.
    default_key = analysis.most_recent_set_key(all_results)
    keys = list(groups.keys())
    key_labels = {k: ", ".join(k) for k in keys}
    default_idx = keys.index(default_key) if default_key in keys else 0
    chosen_label = st.selectbox(
        "Comparison set (heroes)",
        options=[key_labels[k] for k in keys],
        index=default_idx,
        key="matrix_set",
    )
    chosen_key = keys[[key_labels[k] for k in keys].index(chosen_label)]
    grp = groups[chosen_key]
    heroes = grp["heroes"]
    probes = list(grp["probes"].keys())

    c1, c2, c3 = st.columns([2, 2, 3])
    # Series toggle — restrict to series actually present in this set's winners.
    winners = [b["winner"] for b in grp["probes"].values()]
    avail = [(k, lbl) for k, lbl in _SERIES_OPTIONS
             if any(_series_available(w, k) for w in winners)]
    if not avail:
        avail = [("realized", "Realized")]
    series_key = c1.radio(
        "Series", options=[k for k, _ in avail],
        format_func=lambda k: _SERIES_LABEL.get(k, k),
        index=0, key="matrix_series", horizontal=True,
    )
    baseline = c2.selectbox(
        "Baseline (paired against)", options=heroes,
        index=0, key="matrix_baseline",
    )
    # Metric set depends on the series: bb/100 is meaningless on ranks, so the
    # placement series offers chips (= mean Δ rank) / t / p only. Per-series key
    # keeps the radio from holding a now-hidden metric across a series switch.
    metric_opts = analysis.metrics_for_series(series_key)
    metric = c3.radio(
        "Metric", options=metric_opts,
        index=0, key=f"matrix_metric_{series_key}", horizontal=True,
    )

    hero_rows = [h for h in heroes if h != baseline]
    if not hero_rows:
        st.caption("Only the baseline is in this set — nothing to compare.")
        return

    # Build each probe column from its newest result, Holm across the WHOLE
    # matrix (every (hero, probe) cell shown).
    matrix_by_probe = {}
    raw_ps, cell_index = [], []
    field, _lbl = analysis.MATRIX_METRICS[metric]
    for probe in probes:
        winner = grp["probes"][probe]["winner"]
        col = {}
        for hid in hero_rows:
            cell = analysis.matrix_cell(winner, hid, baseline, series_key)
            if cell is None:
                continue
            col[hid] = {"value": cell.get(field), "raw_p": cell.get("p_value"),
                        "cell": cell}
            raw_ps.append(cell.get("p_value"))
            cell_index.append((probe, hid))
        matrix_by_probe[probe] = col
    clean = [p if isinstance(p, (int, float)) and p == p else 1.0 for p in raw_ps]
    holm = analysis.holm_adjust(clean)
    for (probe, hid), hp in zip(cell_index, holm):
        matrix_by_probe[probe][hid]["holm_p"] = hp
        matrix_by_probe[probe][hid]["significant"] = (
            isinstance(hp, (int, float)) and hp < 0.05)

    captions = []
    for probe in probes:
        captions.append(f"{probe} ({grp['probes'][probe]['n_runs']} run"
                        f"{'s' if grp['probes'][probe]['n_runs'] != 1 else ''})")
    st.caption("Probes (newest run per probe used): " + "  ·  ".join(captions))

    if _HAS_MPL:
        st.markdown(
            _render_matrix_html(matrix_by_probe, hero_rows, probes, baseline,
                                metric, series_key),
            unsafe_allow_html=True,
        )
        _color_note = ("color inverted so green/blue = better (lower rank); "
                       if series_key == "placement"
                       else "diverging color centered at 0; ")
        st.caption("Cell = paired (hero − baseline) "
                   f"{analysis.matrix_value_label(series_key, metric)} on the "
                   f"{_SERIES_LABEL.get(series_key, series_key)} series, "
                   f"recomputed live. {_color_note}"
                   "✲ = Holm-significant at 5% across the matrix.")
    else:
        # No matplotlib → plain dataframe (no color), Holm marker inline.
        df_rows = []
        for hid in hero_rows:
            row = {"hero − baseline": f"{hid} − {baseline}"}
            for probe in probes:
                c = matrix_by_probe.get(probe, {}).get(hid)
                txt = _fmt_metric(c.get("value") if c else None, metric)
                if c and c.get("significant"):
                    txt += " ✲"
                row[probe] = txt
            df_rows.append(row)
        st.dataframe(df_rows, use_container_width=True, hide_index=True)
        st.caption("Install matplotlib for diverging color: `pip install matplotlib`. "
                   "✲ = Holm-significant at 5%.")

    # Collapsed absolute companion: all heroes (incl. baseline) × probes.
    with st.expander("Absolute matrix (all heroes incl. baseline)", expanded=False):
        abs_metric = "mean_bb_per_100" if metric in ("bb/100", "t", "p") else "mean_chips"
        abs_label = "mean bb/100" if abs_metric == "mean_bb_per_100" else "mean chips"
        abs_rows = []
        for hid in heroes:
            row = {"hero": hid + ("  ←base" if hid == baseline else "")}
            for probe in probes:
                winner = grp["probes"][probe]["winner"]
                am = analysis.absolute_matrix(winner, [hid], series_key).get(hid)
                if not am:
                    row[probe] = "·"
                else:
                    v = am[abs_metric]
                    row[probe] = (f"{v:+.2f}" if abs_metric == "mean_bb_per_100"
                                  else f"{v:+.0f}")
            abs_rows.append(row)
        st.caption(f"Absolute {abs_label} on the "
                   f"{_SERIES_LABEL.get(series_key, series_key)} series "
                   "(not paired — for context only).")
        st.dataframe(abs_rows, use_container_width=True, hide_index=True)

    # --- Matrix export ---------------------------------------------------
    set_tag = "_".join(chosen_key)
    try:
        st.download_button(
            "⤓ Matrix CSV",
            data=analysis.matrix_csv(grp, baseline, series_key, metric),
            file_name=f"matrix_{set_tag}_{series_key}_{metric.replace('/', '')}.csv",
            mime="text/csv", key=f"dl_matrix_{set_tag}_{series_key}_{metric}",
        )
    except Exception as exc:                       # pragma: no cover - UI guard
        st.caption(f"(matrix CSV unavailable: {exc})")

    st.caption("⚠ Pairing is valid only within a column (probe), never across "
               "probes. Effect size + CI beat t alone (t inflates with N); Holm "
               "guards the multiple comparisons shown here.")

    # --- Placement view (primary placement surface) ----------------------
    if any(analysis.series_arrays(grp["probes"][p]["winner"], "placement")
           for p in probes):
        st.markdown("#### Placement view")
        st.caption("Finals-oriented: mean finish rank (lower = better), P(1st) = "
                   "sole-win rate, P(top-k) from each probe's stored ranks.")
        max_seats = 0
        for probe in probes:
            for hid in heroes:
                vals = analysis.series_arrays(grp["probes"][probe]["winner"],
                                              "placement").get(hid) or []
                if vals:
                    max_seats = max(max_seats, int(max(vals)))
        ks = tuple(k for k in (1, 2, 3) if k <= max(max_seats, 1))
        for probe in probes:
            winner = grp["probes"][probe]["winner"]
            agg = analysis.placement_aggregates(winner, heroes, ks=ks)
            prows = []
            for hid in heroes:
                a = agg.get(hid)
                if not a or not a.get("n"):
                    continue
                row = {"hero": hid + ("  ←base" if hid == baseline else ""),
                       "mean rank": round(a["mean_rank"], 3),
                       "P(1st)": round(a["p_first"], 3)}
                for k in ks:
                    if k > 1:
                        row[f"P(top-{k})"] = round(a["p_top"][k], 3)
                prows.append(row)
            prows.sort(key=lambda r: r["mean rank"])
            st.markdown(f"**{probe}**")
            st.dataframe(prows, use_container_width=True, hide_index=True)

    # --- Drill-down: one probe → its result → pairwise + gap + histogram --
    st.markdown("#### Drill-down (one probe)")
    probe_sel = st.selectbox(
        "Probe to drill into", options=probes, index=0,
        key=f"drill_probe_{set_tag}",
    )
    winner = grp["probes"][probe_sel]["winner"]
    d_series_avail = [(k, lbl) for k, lbl in _SERIES_OPTIONS
                      if _series_available(winner, k)] or [("realized", "Realized")]
    dc1, dc2 = st.columns([2, 2])
    d_series = dc1.radio(
        "Series", options=[k for k, _ in d_series_avail],
        format_func=lambda k: _SERIES_LABEL.get(k, k), index=0,
        key=f"drill_series_{set_tag}", horizontal=True,
    )
    pairing_mode = dc2.radio(
        "Pairing", options=["vs baseline", "full pairwise"], index=0,
        key=f"drill_pairmode_{set_tag}", horizontal=True,
    )
    drill_baseline = baseline if pairing_mode == "vs baseline" else None

    rows = analysis.pairwise_table(winner, d_series, baseline=drill_baseline)
    if rows:
        is_placement = (d_series == "placement")
        chip_label = "mean Δ rank" if is_placement else "mean Δ chips"
        table = []
        for r in rows:
            entry = {
                "pair": r["pair"],
                "n": r["n"],
                chip_label: round(r["mean_chips"], 3),
            }
            if not is_placement:
                entry["mean Δ bb/100"] = round(r["mean_bb_per_100"], 3)
            entry.update({
                "stderr": round(r["stderr"], 2),
                "t": round(r["t"], 3),
                "df": r["df"],
                "p": round(r["p_value"], 4),
                "Holm p": round(r["holm_p"], 4),
                "95% CI": f"[{r['ci_low']:+.0f}, {r['ci_high']:+.0f}]",
                "median Δ": round(r["median_chips"], 3),
                "Wilcoxon p": round(r["wilcoxon_p"], 4),
                "sig@Holm5%": "✲" if r.get("significant") else "",
            })
            table.append(entry)
        st.dataframe(table, use_container_width=True, hide_index=True)
        if is_placement:
            st.caption("Placement pairwise: mean Δ rank < 0 ⇒ the hero finishes "
                       "ahead of the other. bb/100 omitted (meaningless on ranks).")
        else:
            st.caption("Paired (cand − base) on identical seeds/seats. Holm across "
                       "the rows shown; CI is mean ± t·stderr.")

        # Pairwise CSV + raw per-match CSV downloads for this probe.
        ec1, ec2 = st.columns([1, 1])
        ec1.download_button(
            "⤓ Pairwise CSV",
            data=analysis.pairwise_csv(winner, d_series, baseline=drill_baseline),
            file_name=f"pairwise_{set_tag}_{probe_sel}_{d_series}.csv",
            mime="text/csv", key=f"dl_pw_{set_tag}_{probe_sel}_{d_series}",
        )
        ec2.download_button(
            "⤓ Raw per-match CSV",
            data=analysis.raw_per_match_csv(winner),
            file_name=f"rawmatches_{set_tag}_{probe_sel}.csv",
            mime="text/csv", key=f"dl_raw_{set_tag}_{probe_sel}",
        )

    # Realized − EV gap (luck indicator) for this probe's heroes.
    if (analysis.series_arrays(winner, "realized")
            and analysis.series_arrays(winner, "ev")):
        gap_rows = []
        for hid in analysis.hero_ids_of(winner):
            mr, me, gap = analysis.realized_ev_gap(winner, hid)
            if mr != mr:
                continue
            gap_rows.append({
                "hero": hid,
                "mean realized": round(mr, 1),
                "mean EV": round(me, 1),
                "realized − EV (luck)": round(gap, 1),
            })
        if gap_rows:
            with st.expander("Realized − EV gap (per-hero luck)", expanded=False):
                st.dataframe(gap_rows, use_container_width=True, hide_index=True)

    # Series-aware per-match histogram for this probe.
    if not _HAS_MPL:
        st.caption("Install matplotlib for the per-match histogram: "
                   "`pip install matplotlib`")
    else:
        hc1, hc2 = st.columns([2, 2])
        hist_hero = hc1.selectbox(
            "Histogram hero", options=heroes, index=0,
            key=f"hist_hero_{set_tag}",
        )
        hist_mode = hc2.radio(
            "Values", options=["paired (vs baseline)", "absolute"], index=0,
            key=f"hist_mode_{set_tag}", horizontal=True,
        )
        paired = hist_mode.startswith("paired") and hist_hero != baseline
        deltas = analysis.select_series_array(
            winner, d_series, hist_hero,
            baseline_id=baseline, paired=paired)
        if deltas:
            mean_v = sum(deltas) / len(deltas)
            fig, ax = plt.subplots(figsize=(8, 3))
            ax.hist(deltas, bins=min(40, max(10, len(deltas) // 5)),
                    alpha=0.75, edgecolor="black", linewidth=0.4)
            ax.axvline(0, color="black", linewidth=1, linestyle="-", alpha=0.5)
            ax.axvline(mean_v, color="crimson", linewidth=1.5, linestyle="--",
                       label=f"mean = {mean_v:+.2f}")
            unit = ("rank" if d_series == "placement" else "chip Δ")
            kind = (f"{hist_hero} − {baseline}" if paired else hist_hero)
            ax.set_xlabel(f"{kind} {unit}  "
                          f"({_SERIES_LABEL.get(d_series, d_series)}, n={len(deltas)})")
            ax.set_ylabel("matches")
            ax.set_title(f"{kind} — {_SERIES_LABEL.get(d_series, d_series)}")
            ax.legend(loc="upper right", fontsize=9)
            ax.grid(True, alpha=0.3)
            fig.tight_layout()
            st.pyplot(fig)
            plt.close(fig)
        else:
            st.caption("No data for this series/hero.")

    st.caption("⚠ Pairing is valid only within a column (probe), never across "
               "probes. Effect size + CI beat t alone (t inflates with N); Holm "
               "guards the multiple comparisons shown here.")


with tab_results:
    st.subheader("Results")
    results_dir = Path(st.session_state.results_dir)
    files = sorted(results_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)

    if not files:
        st.info(f"No result files in `{results_dir}` yet.")
    else:
        st.caption(f"{len(files)} result file(s) in `{results_dir}`")

        index_rows = []
        for f in files:
            try:
                d = analysis.normalize_result(json.loads(f.read_text()))
                mode = d.get("mode") or (d.get("metadata") or {}).get("mode") or "eval"
                if mode == "eval":
                    hero_repr = (d.get("hero") or {}).get("id") or ""
                    opps_repr = ", ".join(o.get("id", "?") for o in (d.get("opponents") or []))
                    headline = None
                elif d.get("heroes"):                       # new multi-hero shape
                    base = d.get("baseline") or "?"
                    ids = [h.get("id", "?") for h in d["heroes"]]
                    hero_repr = " vs ".join(
                        (i + "*" if i == base else i) for i in ids
                    )
                    opps_repr = ", ".join(o.get("id", "?") for o in (d.get("opponents") or []))
                    vb = d.get("vs_baseline") or {}
                    best = max(
                        ((hid, (v or {}).get("t_stat"))
                         for hid, v in vb.items()
                         if isinstance((v or {}).get("t_stat"), (int, float))),
                        key=lambda kv: abs(kv[1]), default=None,
                    )
                    headline = (f"{best[0]} t={best[1]:+.2f} vs {base}"
                                if best else f"base {base}")
                else:                                       # legacy 2-hero ab
                    a = (d.get("hero_a") or {}).get("id") or "?"
                    b = (d.get("hero_b") or {}).get("id") or "?"
                    hero_repr = f"A:{a} vs B:{b}"
                    opps_repr = ", ".join(o.get("id", "?") for o in (d.get("opponents") or []))
                    t = d.get("t_stat")
                    headline = (f"t={t:+.2f}" if isinstance(t, (int, float)) else None)
                index_rows.append({
                    "file": f.name,
                    "mode": mode,
                    "legacy": d.get("_legacy") or "",
                    "completed_at": (d.get("metadata") or {}).get("completed_at"),
                    "label": (d.get("metadata") or {}).get("label"),
                    "hero(es)": hero_repr,
                    ("opponents" if mode == "eval" else "field"): opps_repr,
                    "preset": d.get("preset_name") or "",
                    "matches": (d.get("params") or {}).get("matches"),
                    "hands": (d.get("params") or {}).get("hands"),
                    "headline": headline or "",
                    "elapsed_s": d.get("elapsed_s"),
                    "error": bool(d.get("error")),
                })
            except Exception as exc:
                index_rows.append({
                    "file": f.name, "mode": "?", "completed_at": None,
                    "label": f"(unreadable: {exc})", "hero(es)": None,
                    "opponents": None, "preset": "", "matches": None,
                    "hands": None, "headline": "", "elapsed_s": None, "error": True,
                })
        st.dataframe(index_rows, use_container_width=True, hide_index=True)

        # --- Step-3 centerpiece: hero × preset comparison matrix --------------
        # Load + normalize every result once; group into comparison sets inside
        # analysis.group_results. Failures to parse are skipped silently here
        # (they already show as errors in the index table above).
        st.markdown("---")
        st.markdown("### Hero × preset matrix")
        _all_results = []
        for f in files:
            try:
                _all_results.append(analysis.normalize_result(json.loads(f.read_text())))
            except Exception:
                continue
        _render_comparison_matrix(_all_results)

        st.markdown("---")
        st.markdown("**Inspect one result**")
        selected_name = st.selectbox(
            "Result file", options=[f.name for f in files], key="result_select",
        )
        selected = next((f for f in files if f.name == selected_name), None)
        if selected:
            try:
                data = analysis.normalize_result(json.loads(selected.read_text()))
            except Exception as exc:
                st.error(f"Could not parse {selected.name}: {exc}")
                data = None

            if data:
                mode = data.get("mode") or (data.get("metadata") or {}).get("mode") or "eval"

                meta_col, dl_col = st.columns([4, 1])
                meta_payload = {
                    "metadata": data.get("metadata"),
                    "opponents": data.get("opponents"),
                    "preset_name": data.get("preset_name"),
                    "params": data.get("params"),
                    "elapsed_s": data.get("elapsed_s"),
                }
                if mode == "eval":
                    meta_payload["hero"] = data.get("hero")
                elif data.get("heroes"):
                    meta_payload["heroes"] = data.get("heroes")
                    meta_payload["baseline"] = data.get("baseline")
                else:
                    meta_payload["hero_a"] = data.get("hero_a")
                    meta_payload["hero_b"] = data.get("hero_b")
                    meta_payload["t_stat"] = data.get("t_stat")
                meta_col.json(meta_payload)
                dl_col.download_button(
                    "⤓ Download JSON",
                    data=selected.read_bytes(),
                    file_name=selected.name,
                    mime="application/json",
                    key=f"dl_{selected.name}",
                )

                if data.get("error"):
                    st.error(f"Run errored: {data['error'].get('message')}")
                    st.code(data["error"].get("traceback") or "", language="text")

                stats = data.get("stats")
                if stats and mode == "eval":
                    hero_id = (data.get("hero") or {}).get("id")
                    rows = []
                    for bid, s in stats.items():
                        rows.append({
                            "bot": bid + ("  ← hero" if bid == hero_id else ""),
                            "matches": s.get("matches"),
                            "mean Δ/match": round(s.get("mean_delta") or 0.0, 1),
                            "95% CI low": round(s.get("ci_low") or 0.0, 1),
                            "95% CI high": round(s.get("ci_high") or 0.0, 1),
                            "bb/100": round(s.get("bb_per_100") or 0.0, 2),
                            "win%": round((s.get("win_rate") or 0.0) * 100, 1),
                        })
                    rows.sort(key=lambda r: -r["mean Δ/match"])
                    st.markdown("**Per-bot stats**")
                    st.dataframe(rows, use_container_width=True, hide_index=True)

                elif stats and mode == "ab" and data.get("heroes"):
                    # New multi-hero paired comparison (2..7 heroes).
                    baseline = data.get("baseline")
                    vs_baseline = data.get("vs_baseline") or {}

                    # 1) Absolute per-hero table.
                    abs_rows = []
                    for hid, s in stats.items():
                        s = s or {}
                        abs_rows.append({
                            "hero": hid + ("  ← baseline" if hid == baseline else ""),
                            "matches": s.get("matches"),
                            "mean Δ/match": round(s.get("mean_delta") or 0.0, 1),
                            "95% CI low": round(s.get("ci_low") or 0.0, 1),
                            "95% CI high": round(s.get("ci_high") or 0.0, 1),
                            "bb/100": round(s.get("bb_per_100") or 0.0, 2),
                            "win%": round((s.get("win_rate") or 0.0) * 100, 1),
                        })
                    abs_rows.sort(key=lambda r: -r["mean Δ/match"])
                    st.markdown("**Per-hero stats (absolute, vs the field)**")
                    st.dataframe(abs_rows, use_container_width=True, hide_index=True)

                    # 2) Paired difference vs baseline, with verdict per hero.
                    st.markdown(f"**Paired difference vs baseline `{baseline}`**")
                    diff_rows = []
                    for hid, v in vs_baseline.items():
                        s = (v or {}).get("stats") or {}
                        t = (v or {}).get("t_stat")
                        ci_low, ci_high = s.get("ci_low"), s.get("ci_high")
                        if ci_low is not None and ci_low > 0:
                            verdict = f"beats {baseline}"
                        elif ci_high is not None and ci_high < 0:
                            verdict = f"loses to {baseline}"
                        else:
                            verdict = "indistinguishable"
                        sig = ("✓" if isinstance(t, (int, float)) and abs(t) >= 1.96
                               else "")
                        diff_rows.append({
                            "hero − baseline": hid,
                            "mean Δ/match": round(s.get("mean_delta") or 0.0, 1),
                            "95% CI low": round(ci_low or 0.0, 1),
                            "95% CI high": round(ci_high or 0.0, 1),
                            "t-stat": round(t, 2) if isinstance(t, (int, float)) else None,
                            "sig@95%": sig,
                            "verdict": verdict,
                        })
                    diff_rows.sort(key=lambda r: -(r["mean Δ/match"]))
                    if diff_rows:
                        st.dataframe(diff_rows, use_container_width=True, hide_index=True)
                        st.caption("Each row is the paired (hero − baseline) chip Δ on "
                                   "identical seeds/seats. CI strictly above 0 ⇒ the "
                                   "hero beats the baseline; ✓ marks |t| ≥ 1.96.")
                    else:
                        st.caption("No non-baseline heroes to compare.")

                elif stats and mode == "ab":
                    # Legacy 2-hero shape (hero_a / hero_b, stats A/B/A_minus_B).
                    a_id = (data.get("hero_a") or {}).get("id") or "A"
                    b_id = (data.get("hero_b") or {}).get("id") or "B"
                    ab_rows = []
                    for row_label, key in (
                        (f"A: {a_id}", "A"),
                        (f"B: {b_id}", "B"),
                        ("A − B (paired)", "A_minus_B"),
                    ):
                        s = stats.get(key) or {}
                        ab_rows.append({
                            "variant": row_label,
                            "matches": s.get("matches"),
                            "mean Δ/match": round(s.get("mean_delta") or 0.0, 1),
                            "95% CI low": round(s.get("ci_low") or 0.0, 1),
                            "95% CI high": round(s.get("ci_high") or 0.0, 1),
                            "bb/100": round(s.get("bb_per_100") or 0.0, 2),
                            "win%": round((s.get("win_rate") or 0.0) * 100, 1),
                        })
                    st.markdown("**A vs B (paired)**")
                    st.dataframe(ab_rows, use_container_width=True, hide_index=True)

                    t_stat = data.get("t_stat")
                    diff = stats.get("A_minus_B") or {}
                    ci_low, ci_high = diff.get("ci_low"), diff.get("ci_high")
                    mean_d = diff.get("mean_delta")
                    significance = ("significant at ~95%"
                                    if t_stat is not None and abs(t_stat) >= 1.96
                                    else "not significant at ~95%")
                    if ci_low is not None and ci_low > 0:
                        verdict = "**A beats B** — 95% CI on (A − B) strictly above 0."
                    elif ci_high is not None and ci_high < 0:
                        verdict = "**B beats A** — 95% CI on (A − B) strictly below 0."
                    else:
                        verdict = ("**Indistinguishable** — 95% CI on (A − B) "
                                   "straddles 0. Run more matches.")
                    st.markdown(
                        f"paired t-stat: `{(t_stat or 0.0):+.2f}` ({significance})  ·  "
                        f"mean (A−B)/match: `{(mean_d or 0.0):+.0f}`  ·  {verdict}"
                    )

                # --- Timing / crashes -----------------------------------------
                if stats:
                    errs = data.get("errors_per_bot") or {}
                    timing = data.get("timing_per_bot") or {}
                    has_bad_timing = any((t or {}).get("slow", 0) for t in timing.values())
                    if any(errs.values()) or has_bad_timing:
                        st.markdown("**Timing / crashes**")
                        ids = sorted(set(list(errs.keys()) + list(timing.keys())))
                        timing_rows = []
                        for bid in ids:
                            timing_rows.append({
                                "bot": bid,
                                "crashes (auto-folded)": errs.get(bid, 0),
                                "max decide(s)": round((timing.get(bid) or {}).get("max") or 0.0, 3),
                                "over budget": (timing.get(bid) or {}).get("slow", 0),
                            })
                        st.dataframe(timing_rows, use_container_width=True, hide_index=True)

                # --- Per-match delta histogram --------------------------------
                pmd = data.get("per_match_deltas") or {}
                paired_pmd = data.get("per_match_paired") or {}
                if pmd:
                    st.markdown("**Per-match delta**")
                    if not _HAS_MPL:
                        st.caption("Install matplotlib to see the histogram: "
                                   "`pip install matplotlib`")
                    else:
                        if mode == "eval":
                            hero_id = (data.get("hero") or {}).get("id")
                            deltas = pmd.get(hero_id) or []
                            hist_title = f"Hero ({hero_id}) chip Δ per match"
                            xlabel = "chip delta per match"
                        elif data.get("heroes"):
                            # Let the user pick any hero's absolute series or any
                            # paired (hero − baseline) series.
                            baseline = data.get("baseline")
                            opts = [f"{hid} (absolute)" for hid in pmd.keys()]
                            opts += [f"{hid} − {baseline} (paired)"
                                     for hid in paired_pmd.keys()]
                            default_idx = (len(pmd) if paired_pmd else 0)
                            choice = st.selectbox(
                                "Series to histogram",
                                options=opts,
                                index=min(default_idx, len(opts) - 1) if opts else 0,
                                key=f"hist_series_{selected.name}",
                            )
                            if choice.endswith("(paired)"):
                                hid = choice.split(" − ", 1)[0]
                                deltas = paired_pmd.get(hid) or []
                                hist_title = f"Paired ({hid} − {baseline}) chip Δ per match"
                                xlabel = f"{hid} − {baseline} chip delta (paired)"
                            else:
                                hid = choice.rsplit(" (absolute)", 1)[0]
                                deltas = pmd.get(hid) or []
                                hist_title = f"{hid} chip Δ per match (absolute)"
                                xlabel = "chip delta per match"
                        else:
                            # Legacy 2-hero shape.
                            deltas = pmd.get("A_minus_B") or []
                            hist_title = "Paired (A − B) chip Δ per match"
                            xlabel = "A − B chip delta per match (paired)"

                        if deltas:
                            mean_v = sum(deltas) / len(deltas)
                            fig, ax = plt.subplots(figsize=(8, 3))
                            ax.hist(deltas, bins=min(40, max(10, len(deltas) // 5)),
                                    alpha=0.75, edgecolor="black", linewidth=0.4)
                            ax.axvline(0, color="black", linewidth=1,
                                       linestyle="-", alpha=0.5, label="0")
                            ax.axvline(mean_v, color="crimson", linewidth=1.5,
                                       linestyle="--", label=f"mean = {mean_v:+.0f}")
                            ax.set_xlabel(f"{xlabel}  (n={len(deltas)})")
                            ax.set_ylabel("matches")
                            ax.set_title(hist_title)
                            ax.legend(loc="upper right", fontsize=9)
                            ax.grid(True, alpha=0.3)
                            fig.tight_layout()
                            st.pyplot(fig)
                            plt.close(fig)
                        else:
                            st.caption("No per-match deltas in this result.")
