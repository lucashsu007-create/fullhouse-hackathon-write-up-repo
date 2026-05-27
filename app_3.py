"""
app.py — Streamlit dashboard wrapping backtest.py's eval mode.

Run from the directory that contains this file and backtest.py:

    pip install streamlit
    streamlit run app.py

The dashboard creates three folders next to itself on first run:
    bots/      bot library  (one subdir per bot, each containing bot.py)
    presets/   named opponent tables (JSON files)
    results/   one JSON per completed backtest run

You can also point any of those at custom paths in the sidebar.

The engine repo (the one containing `engine/game.py`) must be importable.
Either run this app from inside the repo, or set the repo path in the sidebar.
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


# ---------------------------------------------------------------------------
# Paths / session state
# ---------------------------------------------------------------------------

APP_DIR = Path(__file__).parent.resolve()
DEFAULT_BOTS_DIR = APP_DIR / "bots"
DEFAULT_PRESETS_DIR = APP_DIR / "presets"
DEFAULT_RESULTS_DIR = APP_DIR / "results"


def _bootstrap_presets(presets_dir) -> None:
    """One-time seed of common opponent tables. No-op if presets_dir already
    has any *.json — never overwrites user-defined presets."""
    p = Path(presets_dir)
    if p.exists() and any(p.glob("*.json")):
        return
    p.mkdir(parents=True, exist_ok=True)
    bootstrap = {
        "mixed_7":     ["simple_tag", "balanced_tag", "trap_tag", "nit_folder",
                        "calling_station", "mc_pot_odds", "perma_jam"],
        "real_field":  ["competent_tag", "polar_3bettor", "multi_barrel"],
        "extended_10": ["simple_tag", "balanced_tag", "trap_tag", "nit_folder",
                        "calling_station", "mc_pot_odds", "perma_jam",
                        "competent_tag", "polar_3bettor", "multi_barrel"],
        "perma_heavy":   ["perma_jam"] * 5 + ["simple_tag"],
        "station_heavy": ["calling_station"] * 5 + ["simple_tag"],
        "folder_heavy":  ["nit_folder"] * 5 + ["simple_tag"],
    }
    # Inline timestamp because _now_iso() is defined later in the file and
    # this runs at module-level via _init_state().
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
    ss.setdefault("selected_preset", "") # for the config form
    # Seed the default presets dir on first run. Cheap to call on every
    # rerun; the function early-returns once any *.json exists.
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
    st.session_state.bots_dir = st.text_input("Bots dir", value=st.session_state.bots_dir)
    st.session_state.presets_dir = st.text_input("Presets dir", value=st.session_state.presets_dir)
    st.session_state.results_dir = st.text_input("Results dir", value=st.session_state.results_dir)

    for p in (st.session_state.bots_dir,
              st.session_state.presets_dir,
              st.session_state.results_dir):
        Path(p).mkdir(parents=True, exist_ok=True)

    st.caption(f"Working dir: `{APP_DIR}`")


# ---------------------------------------------------------------------------
# Import backtest (after the sidebar so the user can set repo_path first)
# ---------------------------------------------------------------------------

def _import_backtest():
    if st.session_state.repo_path:
        rp = os.path.abspath(st.session_state.repo_path)
        if rp not in sys.path:
            sys.path.insert(0, rp)
    if str(APP_DIR) not in sys.path:
        sys.path.insert(0, str(APP_DIR))
    try:
        import backtest as bt  # noqa: WPS433  intentional lazy import
        return bt
    except Exception as exc:
        return exc


_bt = _import_backtest()
if isinstance(_bt, Exception):
    st.title("Fullhouse Backtest Dashboard")
    st.error(
        "Couldn't import `backtest.py` / `engine.game`.\n\n"
        f"`{type(_bt).__name__}: {_bt}`\n\n"
        "Fix: set the **Engine repo path** in the sidebar, or place this file "
        "next to `backtest.py` inside the fullhouse engine repo."
    )
    st.stop()
bt = _bt


# ---------------------------------------------------------------------------
# Bot library
# ---------------------------------------------------------------------------

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
            # Ignore broken preset files but keep the rest usable
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


# ---------------------------------------------------------------------------
# Jobs / queue
# ---------------------------------------------------------------------------

def _new_job_id() -> str:
    return (dt.datetime.utcnow().strftime("%Y%m%dT%H%M%S")
            + "_" + secrets.token_hex(3))


def _now_iso() -> str:
    return dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _base_job(mode: str, label: str, preset_name: str | None, params: dict) -> dict:
    """Common scaffolding for every job dict. Mode-specific keys are added by
    the callers (make_eval_job / make_ab_job)."""
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
        "error": None,                 # {type, message, traceback} or None
    }


def make_eval_job(label: str,
                  hero: dict,
                  opponents: list[dict],
                  preset_name: str | None,
                  params: dict) -> dict:
    if not label:
        label = f"{hero['id']} vs {len(opponents)} opp(s)"
    job = _base_job("eval", label, preset_name, params)
    job["hero"] = hero
    job["opponents"] = opponents
    return job


def make_ab_job(label: str,
                hero_a: dict,
                hero_b: dict,
                opponents: list[dict],
                preset_name: str | None,
                params: dict) -> dict:
    if not label:
        label = f"A:{hero_a['id']} vs B:{hero_b['id']} on {len(opponents)} field"
    job = _base_job("ab", label, preset_name, params)
    job["hero_a"] = hero_a
    job["hero_b"] = hero_b
    job["opponents"] = opponents
    return job


def write_result_json(job: dict,
                      results_dir: str,
                      result: dict | None,
                      error: dict | None) -> Path:
    """Persist one JSON per job. Shape depends on job["mode"]:

    Both modes share at top level:
        schema_version, metadata{job_id,label,mode,created_at,started_at,completed_at},
        mode, opponents, preset_name, params, elapsed_s, error,
        stats, per_match_deltas, errors_per_bot, timing_per_bot

    Eval-only:
        hero{id,path}
        stats: {bot_id: summarize(...)}
        per_match_deltas: {bot_id: [...]}

    Ab-only:
        hero_a{id,path}, hero_b{id,path}, t_stat
        stats: {"A": ..., "B": ..., "A_minus_B": ...}
        per_match_deltas: {"A": [...], "B": [...], "A_minus_B": [...]}
    """
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


def out_path_safe(obj):
    """JSON values can include numpy / non-finite floats in unusual cases.
    Pass-through for normal types; replace NaN/Inf with None to keep the
    output valid JSON."""
    if isinstance(obj, dict):
        return {k: out_path_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [out_path_safe(v) for v in obj]
    if isinstance(obj, float):
        if obj != obj or obj in (float("inf"), float("-inf")):
            return None
    return obj


def run_job(job: dict, results_dir: str, progress_callback=None) -> None:
    """Execute one job. Always writes a result JSON, even on failure.
    Mutates the job dict in place with status / timing / paths."""
    job["status"] = "running"
    job["started_at"] = _now_iso()
    try:
        params = job["params"]
        if job["mode"] == "eval":
            result = bt.run_eval(
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
            result = bt.run_ab(
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


def _fmt_dur(seconds: float | None) -> str:
    """Compact human-readable duration: '5s', '1m 24s', '1h 03m'."""
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
st.caption("Wraps `backtest.py` eval mode. Sequential run queue, one JSON per run.")

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
        st.markdown("**Upload a bot**")
        up_name = st.text_input(
            "Bot name (used as the id and folder name)",
            key="upload_name",
            placeholder="e.g. shark, nit, my_hero_v3",
        )
        up_file = st.file_uploader("bot.py", type="py", key="upload_file")
        if st.button("Save to library", key="save_bot"):
            name = (up_name or "").strip()
            if not name:
                st.error("Give the bot a name.")
            elif not up_file:
                st.error("Choose a .py file.")
            elif any(c in name for c in r"\/:*?\"<>| "):
                st.error("Bot name can't contain spaces or path separators.")
            elif (Path(st.session_state.bots_dir) / name).exists():
                st.error(f"A bot named `{name}` already exists.")
            else:
                target = save_uploaded_bot(
                    st.session_state.bots_dir, name, up_file.read()
                )
                st.success(f"Saved → {target}")
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
                c1, c2, c3 = st.columns([3, 6, 1])
                c1.markdown(f"**{p['name']}**")
                c2.caption(", ".join(p["bot_ids"]) or "(empty)")
                if c3.button("✕", key=f"del_preset_{p['name']}", help="Delete preset"):
                    delete_preset(st.session_state.presets_dir, p["name"])
                    st.rerun()

        st.markdown("---")
        st.markdown("**Save a new preset**")
        bot_ids = [b["id"] for b in bots]
        new_preset_name = st.text_input(
            "Preset name",
            key="new_preset_name",
            placeholder="e.g. mixed, trappy, nit",
        )
        new_preset_bots = st.multiselect(
            "Bots in this preset",
            options=bot_ids,
            key="new_preset_bots",
        )
        if st.button("Save preset", key="save_preset"):
            name = (new_preset_name or "").strip()
            if not name:
                st.error("Give the preset a name.")
            elif any(c in name for c in r"\/:*?\"<>| "):
                st.error("Preset name can't contain spaces or path separators.")
            elif not new_preset_bots:
                st.error("Pick at least one bot.")
            else:
                save_preset(st.session_state.presets_dir, name, new_preset_bots)
                st.success(f"Saved preset `{name}` ({len(new_preset_bots)} bots).")
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

        # ----- Opponents / Field (via preset or manual) -----
        preset_names = [""] + [p["name"] for p in presets]
        opps_label = "Opponents" if mode == "eval" else "Field"
        preset_choice = st.selectbox(
            f"Preset (optional — prefills {opps_label.lower()})",
            options=preset_names,
            key="cfg_preset",
            format_func=lambda s: "(none)" if s == "" else s,
        )

        # When the user picks a preset, prefill the multiselect — but recompute
        # this if the mode or hero(es) changed, since exclusion changes too.
        sig = (preset_choice, mode, hero_id, hero_a_id, hero_b_id)
        if preset_choice and sig != st.session_state.get("_last_preset_sig"):
            preset = next((p for p in presets if p["name"] == preset_choice), None)
            if preset:
                st.session_state["cfg_opponents"] = [
                    b for b in preset["bot_ids"]
                    if b in bot_ids and b not in excluded_ids
                ]
        st.session_state["_last_preset_sig"] = sig

        # Default and always-filter: keep heroes out of the field.
        st.session_state.setdefault("cfg_opponents", [])
        st.session_state["cfg_opponents"] = [
            b for b in st.session_state["cfg_opponents"]
            if b in bot_ids and b not in excluded_ids
        ]
        opponent_ids = st.multiselect(
            opps_label,
            options=[b for b in bot_ids if b not in excluded_ids],
            key="cfg_opponents",
        )

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
        # reload + rotate_seats are eval-only knobs. ab mode always reloads
        # fresh modules per match (the paired test depends on that), and seat
        # rotation is replaced by the deterministic same-seat-for-A-and-B logic.
        if mode == "eval":
            reload_each = pc6.checkbox(
                "Reload bots per match", value=True, key="cfg_reload",
                help="Fresh module load per match — safer for stateful bots.",
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

        # Build the params dict once — reused by both single-add and sweep.
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
            if not opponent_ids:
                st.error(f"Pick at least one {'opponent' if mode == 'eval' else 'field bot'}.")
            elif mode == "ab" and hero_a_id == hero_b_id:
                st.error("Hero A and Hero B must be different bots.")
            else:
                opponents = [bot_by_id[b] for b in opponent_ids]
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
                "Queue one job per selected preset. Uses the current mode, "
                "hero(es), and parameters above. Each preset's bots become "
                "that job's "
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
                    skipped = []
                    for pname in sweep_choices:
                        preset = next((p for p in presets if p["name"] == pname), None)
                        if not preset:
                            continue
                        opp_ids = [b for b in preset["bot_ids"]
                                   if b in bot_ids and b not in excluded_ids]
                        if not opp_ids:
                            skipped.append(pname)
                            continue
                        opps = [bot_by_id[b] for b in opp_ids]
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
                    if skipped:
                        msg += (f" Skipped (empty after excluding heroes): "
                                f"{', '.join(skipped)}.")
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
            # Mutable holder so the per-job callback can update the queue-wide
            # match counter (closures capture by reference, not by value).
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
                        # --- per-job ----------------------------------------
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
                        # --- queue-wide -------------------------------------
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

                    run_job(
                        job,
                        st.session_state.results_dir,
                        progress_callback=_cb,
                    )

                    # Lock in whatever the callback last reported for this job
                    # (matches the actual amount of work done — important when
                    # a job errors partway through so the queue ETA stays honest).
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
    """Translate v1 (CLI-flat) JSON files into the v2 dashboard shape so the
    inspector can render them. No-op on anything already v2-shaped (or any
    shape we don't recognise — pass-through, the inspector will degrade
    gracefully via .get() everywhere).

    Recognised legacy shapes:

    AB v1 (from `backtest.py ab --json`):
        {"a": "A:foo", "b": "B:bar", "elapsed_s": ...,
         "A": {...}, "B": {...}, "A_minus_B": {...}, "t_stat": ...}

    Eval v1 (from `backtest.py eval --json`):
        {"hero": "myhero", "elapsed_s": ...,
         "stats": {bot_id: summarize(...), ...}}
    """
    # v2 files have a "metadata" block. Anything with metadata is already new.
    if d.get("schema_version") == 2 or "metadata" in d:
        return d

    # ---- AB v1 -----------------------------------------------------------
    if {"A", "B", "A_minus_B"} <= set(d.keys()):
        a = d.get("a", "A")
        b = d.get("b", "B")
        a_stats = d.get("A") or {}
        return {
            **d,
            "schema_version": 2,
            "mode": "ab",
            "metadata": {
                "job_id": f"legacy_{a}_{b}",
                "label": f"legacy: {a} vs {b}",
                "mode": "ab",
                "created_at": None,
                "started_at": None,
                "completed_at": None,
            },
            "hero_a": {"id": a, "path": ""},
            "hero_b": {"id": b, "path": ""},
            "opponents": [],              # CLI JSON didn't record the field
            "preset_name": None,
            "params": {
                "matches": a_stats.get("matches"),
                "hands": 400,             # default that the CLI used
                "seed_base": None, "budget": None, "fold_on_timeout": None,
            },
            "stats": {"A": d["A"], "B": d["B"], "A_minus_B": d["A_minus_B"]},
            "t_stat": d.get("t_stat"),
            "elapsed_s": d.get("elapsed_s"),
            "per_match_deltas": None,     # not in v1
            "errors_per_bot": None,
            "timing_per_bot": None,
            "error": None,
            "_legacy": "ab_v1",
        }

    # ---- Eval v1 ---------------------------------------------------------
    # Eval v1 had hero as a *string*; v2 has hero as {id, path} dict. Use that
    # shape difference as the gate, so we don't accidentally re-translate v2.
    if isinstance(d.get("hero"), str) and isinstance(d.get("stats"), dict):
        hero_str = d["hero"]
        stats = d["stats"]
        hero_stats = stats.get(hero_str) or {}
        return {
            **d,
            "schema_version": 2,
            "mode": "eval",
            "metadata": {
                "job_id": f"legacy_{hero_str}",
                "label": f"legacy: {hero_str}",
                "mode": "eval",
                "created_at": None,
                "started_at": None,
                "completed_at": None,
            },
            "hero": {"id": hero_str, "path": ""},
            "opponents": [
                {"id": bid, "path": ""} for bid in stats.keys() if bid != hero_str
            ],
            "preset_name": None,
            "params": {
                "matches": hero_stats.get("matches"),
                "hands": 400,
                "seed_base": None, "budget": None, "fold_on_timeout": None,
                "reload": None, "rotate_seats": None,
            },
            "stats": stats,
            "elapsed_s": d.get("elapsed_s"),
            "per_match_deltas": None,
            "errors_per_bot": None,
            "timing_per_bot": None,
            "error": None,
            "_legacy": "eval_v1",
        }

    # Unknown shape: pass through. The inspector's .get() calls degrade.
    return d


with tab_results:
    st.subheader("Results")
    results_dir = Path(st.session_state.results_dir)
    files = sorted(results_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)

    if not files:
        st.info(f"No result files in `{results_dir}` yet.")
    else:
        st.caption(f"{len(files)} result file(s) in `{results_dir}`")

        # ----- Index table -----
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
                    # Surface the t-stat in the index — it's the headline number for ab.
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
                    "file": f.name,
                    "mode": "?",
                    "completed_at": None,
                    "label": f"(unreadable: {exc})",
                    "hero(es)": None, "opponents": None, "preset": "",
                    "matches": None, "hands": None,
                    "headline": "", "elapsed_s": None, "error": True,
                })
        st.dataframe(index_rows, use_container_width=True, hide_index=True)

        st.markdown("---")
        st.markdown("**Inspect one result**")
        selected_name = st.selectbox(
            "Result file",
            options=[f.name for f in files],
            key="result_select",
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
                # --- Stats tables --------------------------------------------
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

                    # Significance verdict
                    t_stat = data.get("t_stat")
                    diff = stats.get("A_minus_B") or {}
                    ci_low, ci_high = diff.get("ci_low"), diff.get("ci_high")
                    mean_d = diff.get("mean_delta")
                    significance = ("significant at ~95%"
                                    if t_stat is not None and abs(t_stat) >= 1.96
                                    else "not significant at ~95%")
                    if ci_low is not None and ci_low > 0:
                        verdict = f"**A beats B** — 95% CI on (A − B) strictly above 0."
                    elif ci_high is not None and ci_high < 0:
                        verdict = f"**B beats A** — 95% CI on (A − B) strictly below 0."
                    else:
                        verdict = ("**Indistinguishable** — 95% CI on (A − B) "
                                   "straddles 0. Run more matches.")
                    st.markdown(
                        f"paired t-stat: `{t_stat:+.2f}` ({significance})  ·  "
                        f"mean (A−B)/match: `{mean_d:+.0f}`  ·  {verdict}"
                    )

                # --- Timing / crashes -----------------------------------------
                if stats:
                    errs = data.get("errors_per_bot") or {}
                    timing = data.get("timing_per_bot") or {}
                    has_bad_timing = any(t.get("slow", 0) for t in timing.values())
                    if any(errs.values()) or has_bad_timing:
                        st.markdown("**Timing / crashes**")
                        # For eval, stats.keys() are bot ids. For ab, they're
                        # {"A","B","A_minus_B"} — not real bot ids. Pull bot
                        # ids from errors_per_bot / timing_per_bot directly.
                        bot_ids = sorted(set(list(errs.keys()) + list(timing.keys())))
                        timing_rows = []
                        for bid in bot_ids:
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
