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


# ---------------------------------------------------------------------------
# Paths / session state
# ---------------------------------------------------------------------------

APP_DIR = Path(__file__).parent.resolve()
DEFAULT_BOTS_DIR = APP_DIR / "bots"
DEFAULT_PRESETS_DIR = APP_DIR / "presets"
DEFAULT_RESULTS_DIR = APP_DIR / "results"


def _init_state() -> None:
    ss = st.session_state
    ss.setdefault("repo_path", "")
    ss.setdefault("bots_dir", str(DEFAULT_BOTS_DIR))
    ss.setdefault("presets_dir", str(DEFAULT_PRESETS_DIR))
    ss.setdefault("results_dir", str(DEFAULT_RESULTS_DIR))
    ss.setdefault("queue", [])           # list of job dicts
    ss.setdefault("selected_preset", "") # for the config form


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

def make_job(label: str,
             hero: dict,
             opponents: list[dict],
             preset_name: str | None,
             params: dict) -> dict:
    job_id = (dt.datetime.utcnow().strftime("%Y%m%dT%H%M%S")
              + "_" + secrets.token_hex(3))
    if not label:
        label = f"{hero['id']} vs {len(opponents)} opp(s)"
    return {
        "job_id": job_id,
        "label": label,
        "status": "pending",       # pending | running | done | error
        "hero": hero,
        "opponents": opponents,
        "preset_name": preset_name,
        "params": dict(params),
        "created_at": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "started_at": None,
        "completed_at": None,
        "elapsed_s": None,
        "result_path": None,
        "error": None,             # {type, message, traceback} or None
    }


def _now_iso() -> str:
    return dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"


def write_result_json(job: dict,
                      results_dir: str,
                      eval_result: dict | None,
                      error: dict | None) -> Path:
    """Persist one JSON per job to results_dir. Includes everything needed
    to retrace the run later: metadata, hero, opponents, preset name, params,
    full stats / per-match deltas (when successful), and the error block when
    the job crashed."""
    out = {
        "schema_version": 1,
        "metadata": {
            "job_id": job["job_id"],
            "label": job["label"],
            "mode": "eval",
            "created_at": job["created_at"],
            "started_at": job["started_at"],
            "completed_at": job["completed_at"],
        },
        "hero": job["hero"],
        "opponents": job["opponents"],
        "preset_name": job["preset_name"],
        "params": job["params"],
        "elapsed_s": (eval_result or {}).get("elapsed_s"),
        "stats": (eval_result or {}).get("stats"),
        "errors_per_bot": (eval_result or {}).get("errors"),
        "timing_per_bot": (eval_result or {}).get("timing"),
        "per_match_deltas": (eval_result or {}).get("per_match_deltas"),
        "error": error,
    }
    out_path = Path(results_dir) / f"{job['job_id']}.json"
    out_path.write_text(json.dumps(out, indent=2))
    return out_path


def run_job(job: dict, results_dir: str, progress_callback=None) -> None:
    """Execute one job. Always writes a result JSON, even on failure.
    Mutates the job dict in place with status / timing / paths."""
    job["status"] = "running"
    job["started_at"] = _now_iso()
    try:
        result = bt.run_eval(
            hero_path=job["hero"]["path"],
            opponent_paths=[o["path"] for o in job["opponents"]],
            matches=job["params"]["matches"],
            hands=job["params"]["hands"],
            seed_base=job["params"]["seed_base"],
            budget=job["params"]["budget"],
            fold_on_timeout=job["params"]["fold_on_timeout"],
            reload=job["params"]["reload"],
            rotate_seats=job["params"]["rotate_seats"],
            progress_callback=progress_callback,
        )
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
        # ----- Hero -----
        hero_id = st.selectbox("Hero", options=bot_ids, key="cfg_hero")

        # ----- Opponents (via preset or manual) -----
        preset_names = [""] + [p["name"] for p in presets]
        preset_choice = st.selectbox(
            "Preset (optional — prefills opponents)",
            options=preset_names,
            key="cfg_preset",
            format_func=lambda s: "(none)" if s == "" else s,
        )
        if preset_choice and preset_choice != st.session_state.get("_last_preset"):
            # The user just picked a different preset — prefill the opponents.
            preset = next((p for p in presets if p["name"] == preset_choice), None)
            if preset:
                st.session_state["cfg_opponents"] = [
                    b for b in preset["bot_ids"] if b in bot_ids and b != hero_id
                ]
        st.session_state["_last_preset"] = preset_choice

        # Default value if the field hasn't been set yet.
        st.session_state.setdefault("cfg_opponents", [])
        # Filter out the hero — can't play against yourself.
        st.session_state["cfg_opponents"] = [
            b for b in st.session_state["cfg_opponents"]
            if b in bot_ids and b != hero_id
        ]
        opponent_ids = st.multiselect(
            "Opponents",
            options=[b for b in bot_ids if b != hero_id],
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
        reload_each = pc6.checkbox(
            "Reload bots per match", value=True, key="cfg_reload",
            help="Fresh module load per match — safer for stateful bots.",
        )

        rotate = st.checkbox(
            "Deterministic seat rotation",
            value=False, key="cfg_rotate",
            help="Cycle the seating order each match instead of pseudo-random shuffle.",
        )

        label = st.text_input(
            "Run label (optional)",
            key="cfg_label",
            placeholder=f"e.g. {hero_id} vs {preset_choice or 'field'}",
        )

        if st.button("Add to queue", type="primary", key="add_to_queue"):
            if not opponent_ids:
                st.error("Pick at least one opponent.")
            else:
                hero_bot = bot_by_id[hero_id]
                opponents = [bot_by_id[b] for b in opponent_ids]
                params = {
                    "matches": int(matches),
                    "hands": int(hands),
                    "seed_base": int(seed_base),
                    "budget": float(budget),
                    "fold_on_timeout": bool(fold_on_timeout),
                    "reload": bool(reload_each),
                    "rotate_seats": bool(rotate),
                }
                job = make_job(
                    label=label or "",
                    hero=hero_bot,
                    opponents=opponents,
                    preset_name=preset_choice or None,
                    params=params,
                )
                st.session_state.queue.append(job)
                st.success(f"Queued: {job['label']}  (id `{job['job_id']}`).")

    # ----- Queue table -----
    st.markdown("---")
    st.subheader("Queue")

    q = st.session_state.queue
    if not q:
        st.caption("Queue is empty.")
    else:
        rows = []
        for j in q:
            rows.append({
                "status": j["status"],
                "label": j["label"],
                "hero": j["hero"]["id"],
                "opponents": ", ".join(o["id"] for o in j["opponents"]),
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
                d = json.loads(f.read_text())
                index_rows.append({
                    "file": f.name,
                    "completed_at": (d.get("metadata") or {}).get("completed_at"),
                    "label": (d.get("metadata") or {}).get("label"),
                    "hero": (d.get("hero") or {}).get("id"),
                    "opponents": ", ".join(o.get("id", "?") for o in (d.get("opponents") or [])),
                    "preset": d.get("preset_name") or "",
                    "matches": (d.get("params") or {}).get("matches"),
                    "hands": (d.get("params") or {}).get("hands"),
                    "elapsed_s": d.get("elapsed_s"),
                    "error": bool(d.get("error")),
                })
            except Exception as exc:
                index_rows.append({
                    "file": f.name,
                    "completed_at": None,
                    "label": f"(unreadable: {exc})",
                    "hero": None, "opponents": None, "preset": "",
                    "matches": None, "hands": None,
                    "elapsed_s": None, "error": True,
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
                data = json.loads(selected.read_text())
            except Exception as exc:
                st.error(f"Could not parse {selected.name}: {exc}")
                data = None

            if data:
                meta_col, dl_col = st.columns([4, 1])
                meta_col.json({
                    "metadata": data.get("metadata"),
                    "hero": data.get("hero"),
                    "opponents": data.get("opponents"),
                    "preset_name": data.get("preset_name"),
                    "params": data.get("params"),
                    "elapsed_s": data.get("elapsed_s"),
                })
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
                if stats:
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

                    errs = data.get("errors_per_bot") or {}
                    timing = data.get("timing_per_bot") or {}
                    if any(errs.values()) or any(t.get("slow", 0) for t in timing.values()):
                        st.markdown("**Timing / crashes**")
                        timing_rows = []
                        for bid in stats.keys():
                            timing_rows.append({
                                "bot": bid,
                                "crashes (auto-folded)": errs.get(bid, 0),
                                "max decide(s)": round((timing.get(bid) or {}).get("max") or 0.0, 3),
                                "over budget": (timing.get(bid) or {}).get("slow", 0),
                            })
                        st.dataframe(timing_rows, use_container_width=True, hide_index=True)
