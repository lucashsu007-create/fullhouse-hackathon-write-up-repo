"""
app.py — Streamlit dashboard for backtest.py (self-contained orchestration).

Run from the directory that contains this file and backtest.py:

    pip install streamlit
    streamlit run app.py

This version does NOT depend on backtest.py exposing run_eval()/run_ab().
It imports the building blocks that backtest.py defines —
    make_ids, build_decide_map, run_match_inproc, summarize
— and performs the eval / paired-AB orchestration locally (see _run_eval /
_run_ab below). That mirrors backtest.py's own cmd_eval / cmd_ab logic exactly,
match-for-match, while also returning the richer result shape this dashboard's
Results tab needs (per-match deltas, per-bot crash/timing counts) and threading
a per-match progress callback.

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
import random
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
    st.session_state.bots_dir = st.text_input(
        "Bots dir",
        value=st.session_state.bots_dir,
        help="Looks for <dir>/<botname>/bot.py for each bot. "
             "Auto-detected on startup; override here if needed.",
    )
    st.session_state.presets_dir = st.text_input("Presets dir", value=st.session_state.presets_dir)
    st.session_state.results_dir = st.text_input("Results dir", value=st.session_state.results_dir)

    for _p in (st.session_state.bots_dir,
               st.session_state.presets_dir,
               st.session_state.results_dir):
        Path(_p).mkdir(parents=True, exist_ok=True)

    _bd = Path(st.session_state.bots_dir)
    if _bd.is_dir():
        _n_bots = sum(1 for s in _bd.iterdir() if s.is_dir() and (s / "bot.py").is_file())
        st.caption(f"Bots dir reachable · {_n_bots} bot(s) detected")
    else:
        st.caption("⚠ Bots dir not reachable from this process.")

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


# ---------------------------------------------------------------------------
# Local eval / AB orchestration (mirrors backtest.cmd_eval / cmd_ab exactly,
# but returns the richer dashboard result shape and reports per-match progress)
# ---------------------------------------------------------------------------

def _hero_label(prefix: str, path: str) -> str:
    """Same labelling backtest.cmd_ab uses: file stem, or parent dir when the
    file is the conventional bot.py."""
    pp = Path(path)
    name = pp.stem
    if name in ("bot",):
        name = pp.parent.name or name
    return prefix + name


def _accumulate(dst_err, dst_tim, res):
    """Fold one match's per-bot errors/timing into running totals."""
    for bid, c in (res.get("errors") or {}).items():
        dst_err[bid] = dst_err.get(bid, 0) + c
    for bid, tinfo in (res.get("timing") or {}).items():
        agg = dst_tim.setdefault(bid, {"max": 0.0, "slow": 0})
        agg["max"] = max(agg["max"], (tinfo or {}).get("max", 0.0))
        agg["slow"] += (tinfo or {}).get("slow", 0)


def _run_eval(hero_path, opponent_paths, matches, hands, seed_base,
              budget, fold_on_timeout, reload, rotate_seats,
              progress_callback=None) -> dict:
    """One hero vs a field of opponents. opponent_paths may contain duplicate
    paths — make_ids gives each a distinct id (foo, foo_2, …) and build_decide_map
    loads each under a unique module name, so duplicates seat independently with
    independent state. Mirrors backtest.cmd_eval's seating/seeding."""
    paths = [hero_path] + list(opponent_paths)
    ids = bt.make_ids(paths)
    hero_id = ids[0]
    id_to_path = dict(zip(ids, paths))

    per_bot = {bid: [] for bid in ids}
    errors_total: dict = {}
    timing_total: dict = {}

    cached = None if reload else bt.build_decide_map(id_to_path)

    t0 = time.time()
    for i in range(matches):
        seed = seed_base + i
        order = ids[:]
        if rotate_seats:
            k = i % len(order)
            order = order[k:] + order[:k]
        else:
            random.Random(seed * 7919 + 1).shuffle(order)

        dm = (bt.build_decide_map({b: id_to_path[b] for b in order})
              if reload else {b: cached[b] for b in order})

        res = bt.run_match_inproc(dm, hands, seed, budget=budget,
                                  fold_on_timeout=fold_on_timeout)
        for bid, d in res["chip_delta"].items():
            per_bot[bid].append(d)
        _accumulate(errors_total, timing_total, res)

        if progress_callback:
            progress_callback(i + 1, matches)

    elapsed = time.time() - t0
    stats = {bid: bt.summarize(per_bot[bid], hands) for bid in ids}
    return {
        "elapsed_s": round(elapsed, 2),
        "hero_id": hero_id,
        "stats": stats,
        "per_match_deltas": per_bot,
        "errors": errors_total,
        "timing": timing_total,
    }


def _run_ab(a_path, b_path, field_paths, matches, hands, seed_base,
            budget, fold_on_timeout, progress_callback=None) -> dict:
    """Paired A-vs-B on identical seeds, identical seat, identical (freshly
    loaded) opponents. Mirrors backtest.cmd_ab. field_paths may contain
    duplicates; they seat independently."""
    field_ids = bt.make_ids(list(field_paths))
    field_map = dict(zip(field_ids, field_paths))
    a_id = _hero_label("A:", a_path)
    b_id = _hero_label("B:", b_path)
    if a_id == b_id:
        a_id, b_id = a_id + "#1", b_id + "#2"
    n_seats = len(field_ids) + 1

    a_deltas, b_deltas, paired = [], [], []
    errors_total: dict = {}
    timing_total: dict = {}

    t0 = time.time()
    for i in range(matches):
        seed = seed_base + i
        hero_idx = i % n_seats  # same seat for A and B at this seed

        def seated(hid, hpath):
            order = field_ids[:]
            order.insert(hero_idx, hid)
            paths = {bid: (hpath if bid == hid else field_map[bid])
                     for bid in order}
            dm = bt.build_decide_map({b: paths[b] for b in order})
            return bt.run_match_inproc(dm, hands, seed, budget=budget,
                                       fold_on_timeout=fold_on_timeout)

        ra = seated(a_id, a_path)
        rb = seated(b_id, b_path)
        _accumulate(errors_total, timing_total, ra)
        _accumulate(errors_total, timing_total, rb)

        a_deltas.append(ra["chip_delta"][a_id])
        b_deltas.append(rb["chip_delta"][b_id])
        paired.append(a_deltas[-1] - b_deltas[-1])

        if progress_callback:
            progress_callback(i + 1, matches)

    elapsed = time.time() - t0
    sa = bt.summarize(a_deltas, hands)
    sb = bt.summarize(b_deltas, hands)
    sd = bt.summarize(paired, hands)
    t_stat = (sd["mean_delta"] / sd["stderr"]) if sd["stderr"] else 0.0
    return {
        "elapsed_s": round(elapsed, 2),
        "stats": {"A": sa, "B": sb, "A_minus_B": sd},
        "t_stat": t_stat,
        "per_match_deltas": {"A": a_deltas, "B": b_deltas, "A_minus_B": paired},
        "errors": errors_total,
        "timing": timing_total,
    }


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


def make_ab_job(label, hero_a, hero_b, opponents, preset_name, params) -> dict:
    if not label:
        label = f"A:{hero_a['id']} vs B:{hero_b['id']} on {len(opponents)} field"
    job = _base_job("ab", label, preset_name, params)
    job["hero_a"] = hero_a
    job["hero_b"] = hero_b
    job["opponents"] = opponents
    return job


def out_path_safe(obj):
    """Replace NaN/Inf with None so the output is valid JSON."""
    if isinstance(obj, dict):
        return {k: out_path_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [out_path_safe(v) for v in obj]
    if isinstance(obj, float):
        if obj != obj or obj in (float("inf"), float("-inf")):
            return None
    return obj


def write_result_json(job, results_dir, result, error) -> Path:
    """Persist one JSON per job (schema_version 2)."""
    base = {
        "schema_version": 2,
        "metadata": {
            "job_id": job["job_id"],
            "label": job["label"],
            "mode": job["mode"],
            "created_at": job["created_at"],
            "started_at": job["started_at"],
            "completed_at": job["completed_at"],
        },
        "mode": job["mode"],
        "opponents": job["opponents"],
        "preset_name": job["preset_name"],
        "params": job["params"],
        "elapsed_s": (result or {}).get("elapsed_s"),
        "stats": (result or {}).get("stats"),
        "per_match_deltas": (result or {}).get("per_match_deltas"),
        "errors_per_bot": (result or {}).get("errors"),
        "timing_per_bot": (result or {}).get("timing"),
        "error": error,
    }
    if job["mode"] == "eval":
        base["hero"] = job["hero"]
    elif job["mode"] == "ab":
        base["hero_a"] = job["hero_a"]
        base["hero_b"] = job["hero_b"]
        base["t_stat"] = (result or {}).get("t_stat")

    out_path = Path(results_dir) / f"{job['job_id']}.json"
    out_path.write_text(json.dumps(out_path_safe(base), indent=2))
    return out_path


def run_job(job, results_dir, progress_callback=None) -> None:
    """Execute one job. Always writes a result JSON, even on failure."""
    job["status"] = "running"
    job["started_at"] = _now_iso()
    try:
        params = job["params"]
        if job["mode"] == "eval":
            result = _run_eval(
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
            result = _run_ab(
                a_path=job["hero_a"]["path"],
                b_path=job["hero_b"]["path"],
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
        out_path = write_result_json(job, results_dir, result, None)
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
        out_path = write_result_json(job, results_dir, None, err)
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

    # ----- Bot library -----
    with col_bots:
        st.subheader("Bot library")
        bots = list_bots(st.session_state.bots_dir)
        if not bots:
            st.info(
                f"No bots in `{st.session_state.bots_dir}` yet. "
                "Upload below, or drop bot folders in directly."
            )
        else:
            st.write(f"{len(bots)} bot(s) found:")
            for b in bots:
                c1, c2, c3 = st.columns([3, 6, 1])
                c1.markdown(f"**{b['id']}**")
                c2.caption(b["path"])
                if c3.button("✕", key=f"del_bot_{b['id']}", help="Delete this bot"):
                    delete_bot(st.session_state.bots_dir, b["id"])
                    st.rerun()

        st.markdown("---")
        st.markdown("**Upload bots**")
        st.caption("Drag and drop one or more `.py` files (or click to browse). "
                   "Each bot is named after its filename; the override below "
                   "applies only when you upload a single file.")

        # Surface the outcome of the previous save across the list-refresh rerun.
        _msg = st.session_state.pop("_upload_msg", None)
        if _msg:
            if _msg.get("saved"):
                st.success("Saved: " + ", ".join(f"`{n}`" for n in _msg["saved"]))
            if _msg.get("skipped"):
                st.warning("Skipped — " + "; ".join(_msg["skipped"]))

        up_files = st.file_uploader(
            "bot.py file(s)",
            type="py",
            key="upload_files",
            accept_multiple_files=True,
        )
        single = len(up_files) == 1
        up_name = st.text_input(
            "Name override (single file only)",
            key="upload_name",
            placeholder="e.g. shark, nit, my_hero_v3",
            disabled=not single,
            help="Leave blank to use the filename. Ignored when several files "
                 "are uploaded at once.",
        )
        if st.button("Save to library", key="save_bot"):
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
                    if (Path(st.session_state.bots_dir) / name).exists():
                        skipped.append(f"{uf.name} → `{name}` already exists")
                        continue
                    save_uploaded_bot(st.session_state.bots_dir, name, uf.read())
                    saved.append(name)
                st.session_state["_upload_msg"] = {"saved": saved, "skipped": skipped}
                st.rerun()

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
        st.caption("Duplicates are allowed and are preserved as separate seats. "
                   "Add a bot twice in the box below to pad a table.")
        bot_ids = [b["id"] for b in bots]
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
    bots = list_bots(st.session_state.bots_dir)
    presets = list_presets(st.session_state.presets_dir)
    bot_ids = [b["id"] for b in bots]
    bot_by_id = {b["id"]: b for b in bots}

    st.subheader("New run")
    if not bots:
        st.warning("Add at least one bot in Tab 1 first.")
    else:
        # ----- Mode -----
        mode = st.radio(
            "Mode",
            options=["eval", "ab"],
            horizontal=True,
            key="cfg_mode",
            format_func=lambda m: "eval (1 hero vs field)" if m == "eval"
                                  else "ab (paired A vs B on same field)",
            help=("eval: one hero against a field, ranked by EV. "
                  "ab: two hero variants on identical seeds + same seat per match, "
                  "with paired t-stat on (A − B)."),
        )

        # ----- Hero(es) -----
        if mode == "eval":
            hero_id = st.selectbox("Hero", options=bot_ids, key="cfg_hero")
            hero_a_id = hero_b_id = None
            excluded_ids = {hero_id}
        else:
            ab_col1, ab_col2 = st.columns(2)
            hero_a_id = ab_col1.selectbox("Hero A", options=bot_ids, key="cfg_hero_a")
            b_options = [b for b in bot_ids if b != hero_a_id] or bot_ids
            hero_b_id = ab_col2.selectbox("Hero B", options=b_options, key="cfg_hero_b")
            hero_id = None
            excluded_ids = {hero_a_id, hero_b_id}

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
                resolve_preset_field(preset, bot_ids, excluded_ids)
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
                options=[b for b in bot_ids if b not in excluded_ids],
                key="cfg_opponents",
                help="Manual field. (A multiselect can't hold duplicates — to "
                     "pad with repeats, save a preset in Tab 1 and pick it here.)",
            )

        # Field preview + seat-count guard.
        n_seats = len(effective_field) + 1
        seat_ok = (1 <= len(effective_field)) and (n_seats <= MAX_SEATS)
        preview = ", ".join(effective_field) if effective_field else "(empty)"
        st.caption(f"Effective {opps_label.lower()}: {preview}  ·  **{n_seats} seats** "
                   f"incl. hero")
        if dropped_from_preset:
            st.caption("Dropped from preset (unknown bot, or it's a selected hero): "
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
                         else f"e.g. {hero_a_id} vs {hero_b_id} on {preset_choice or 'field'}"),
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
            elif mode == "ab" and hero_a_id == hero_b_id:
                st.error("Hero A and Hero B must be different bots.")
            else:
                opponents = [bot_by_id[b] for b in effective_field]  # dups kept
                if mode == "eval":
                    job = make_eval_job(
                        label=label or "",
                        hero=bot_by_id[hero_id],
                        opponents=opponents,
                        preset_name=preset_choice or None,
                        params=params,
                    )
                else:
                    job = make_ab_job(
                        label=label or "",
                        hero_a=bot_by_id[hero_a_id],
                        hero_b=bot_by_id[hero_b_id],
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
                    or (mode == "ab" and hero_a_id == hero_b_id)
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
                        opp_ids, _drop = resolve_preset_field(preset, bot_ids, excluded_ids)
                        if not opp_ids:
                            skipped_empty.append(pname)
                            continue
                        if len(opp_ids) + 1 > MAX_SEATS:
                            skipped_big.append(pname)
                            continue
                        opps = [bot_by_id[b] for b in opp_ids]  # dups kept
                        sweep_label = (f"{sweep_label_prefix} · {pname}"
                                       if sweep_label_prefix else f"sweep:{pname}")
                        if mode == "eval":
                            j = make_eval_job(
                                label=sweep_label, hero=bot_by_id[hero_id],
                                opponents=opps, preset_name=pname, params=params,
                            )
                        else:
                            j = make_ab_job(
                                label=sweep_label,
                                hero_a=bot_by_id[hero_a_id],
                                hero_b=bot_by_id[hero_b_id],
                                opponents=opps, preset_name=pname, params=params,
                            )
                        st.session_state.queue.append(j)
                        added += 1
                    msg = f"Queued {added} job(s) from sweep."
                    if skipped_empty:
                        msg += (" Skipped (empty after excluding heroes): "
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
                hero_repr = f"A:{j['hero_a']['id']} vs B:{j['hero_b']['id']}"
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
# Tab 3 — Results
# ---------------------------------------------------------------------------

def _normalize_legacy_result(d: dict) -> dict:
    """Translate v1 (CLI-flat) JSON files into the v2 dashboard shape. No-op on
    anything already v2-shaped or unrecognised (pass-through; the inspector
    degrades gracefully via .get())."""
    if d.get("schema_version") == 2 or "metadata" in d:
        return d

    if {"A", "B", "A_minus_B"} <= set(d.keys()):
        a = d.get("a", "A")
        b = d.get("b", "B")
        a_stats = d.get("A") or {}
        return {
            **d, "schema_version": 2, "mode": "ab",
            "metadata": {"job_id": f"legacy_{a}_{b}", "label": f"legacy: {a} vs {b}",
                         "mode": "ab", "created_at": None, "started_at": None,
                         "completed_at": None},
            "hero_a": {"id": a, "path": ""}, "hero_b": {"id": b, "path": ""},
            "opponents": [], "preset_name": None,
            "params": {"matches": a_stats.get("matches"), "hands": 400,
                       "seed_base": None, "budget": None, "fold_on_timeout": None},
            "stats": {"A": d["A"], "B": d["B"], "A_minus_B": d["A_minus_B"]},
            "t_stat": d.get("t_stat"), "elapsed_s": d.get("elapsed_s"),
            "per_match_deltas": None, "errors_per_bot": None,
            "timing_per_bot": None, "error": None, "_legacy": "ab_v1",
        }

    if isinstance(d.get("hero"), str) and isinstance(d.get("stats"), dict):
        hero_str = d["hero"]
        stats = d["stats"]
        hero_stats = stats.get(hero_str) or {}
        return {
            **d, "schema_version": 2, "mode": "eval",
            "metadata": {"job_id": f"legacy_{hero_str}", "label": f"legacy: {hero_str}",
                         "mode": "eval", "created_at": None, "started_at": None,
                         "completed_at": None},
            "hero": {"id": hero_str, "path": ""},
            "opponents": [{"id": bid, "path": ""} for bid in stats.keys() if bid != hero_str],
            "preset_name": None,
            "params": {"matches": hero_stats.get("matches"), "hands": 400,
                       "seed_base": None, "budget": None, "fold_on_timeout": None,
                       "reload": None, "rotate_seats": None},
            "stats": stats, "elapsed_s": d.get("elapsed_s"),
            "per_match_deltas": None, "errors_per_bot": None,
            "timing_per_bot": None, "error": None, "_legacy": "eval_v1",
        }

    return d


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
                d = _normalize_legacy_result(json.loads(f.read_text()))
                mode = d.get("mode") or (d.get("metadata") or {}).get("mode") or "eval"
                if mode == "eval":
                    hero_repr = (d.get("hero") or {}).get("id") or ""
                    opps_repr = ", ".join(o.get("id", "?") for o in (d.get("opponents") or []))
                    headline = None
                else:
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

        st.markdown("---")
        st.markdown("**Inspect one result**")
        selected_name = st.selectbox(
            "Result file", options=[f.name for f in files], key="result_select",
        )
        selected = next((f for f in files if f.name == selected_name), None)
        if selected:
            try:
                data = _normalize_legacy_result(json.loads(selected.read_text()))
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

                elif stats and mode == "ab":
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
                        else:
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
