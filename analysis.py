"""
analysis.py — pure orchestration / IO / schema logic for the Fullhouse backtest
dashboard. **Imports no streamlit**: everything here is plain Python so it can be
unit-tested directly (and so app.py stays thin glue around it).

Responsibilities:
  * eval / paired-multi orchestration (`_run_eval`, `_run_multi`) — mirrors
    backtest.cmd_eval / cmd_ab match-for-match, but returns the richer dashboard
    result shape (per-match realized + EV series, placement, crash/timing
    counts) and threads a per-match progress callback.
  * `placement` — pure per-match finish-rank helper.
  * result IO: `write_result_json` (schema_version 3, self-describing params),
    `out_path_safe`, and `normalize_result` (loads v1/v2/v3, flags realized-only).

The harness primitives (`run_match_inproc`, `build_decide_map`, `make_ids`,
`summarize`) and engine constants (`STARTING_STACK`, `BIG_BLIND`) are injected by
`bind_harness(bt)` so this module never hard-imports `backtest` / `engine.game`
/ `eval7`. Tests can either call `bind_harness` with a stub module or set the
module-level names directly. Sensible defaults let the pure helpers
(`placement`, `write_result_json`, `normalize_result`) work with no binding.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import math
import os
import random
import statistics
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

# --- Harness primitives, injected via bind_harness() -----------------------
# Left None until bound; the orchestration functions need them, but the pure
# helpers (placement / schema IO / normalizer) do not.
make_ids = None
build_decide_map = None
run_match_inproc = None
summarize = None

# Engine constants. Defaults match engine.game; bind_harness overrides with the
# authoritative values when the real harness is wired in.
STARTING_STACK = 10_000
BIG_BLIND = 100


def bind_harness(bt) -> None:
    """Wire the harness primitives + engine constants from an imported backtest
    module (or any object exposing the same names). Called by app.py after it
    imports backtest; tests may call it with a stub or skip it and set globals."""
    global make_ids, build_decide_map, run_match_inproc, summarize
    global STARTING_STACK, BIG_BLIND
    make_ids = getattr(bt, "make_ids", make_ids)
    build_decide_map = getattr(bt, "build_decide_map", build_decide_map)
    run_match_inproc = getattr(bt, "run_match_inproc", run_match_inproc)
    summarize = getattr(bt, "summarize", summarize)
    STARTING_STACK = getattr(bt, "STARTING_STACK", STARTING_STACK)
    BIG_BLIND = getattr(bt, "BIG_BLIND", BIG_BLIND)


# ---------------------------------------------------------------------------
# Per-match placement (pure)
# ---------------------------------------------------------------------------

def placement(hero_id, chip_delta: dict) -> float:
    """Finish rank of `hero_id` among all seats in one match, by final stack.

    Final stack = chip_delta + STARTING_STACK, but the constant offset does not
    change the ordering, so we rank on chip_delta directly. 1 = best (largest
    stack). Ties take the AVERAGE of the ranks they span (standard
    average/fractional ranking), so e.g. a clean 3-way tie is 2.0 and the worst
    of three distinct stacks is 3.0.
    """
    hero_val = chip_delta[hero_id]
    vals = list(chip_delta.values())
    greater = sum(1 for v in vals if v > hero_val)   # seats strictly ahead
    tied = sum(1 for v in vals if v == hero_val)      # seats level (incl. hero)
    # The tied block occupies ranks [greater+1 .. greater+tied]; their mean is:
    return greater + (tied + 1) / 2.0


# ---------------------------------------------------------------------------
# Orchestration (mirrors backtest.cmd_eval / cmd_ab; richer result shape)
# ---------------------------------------------------------------------------

def _hero_label(prefix: str, path: str) -> str:
    """Same labelling backtest.cmd_ab uses: file stem, or parent dir when the
    file is the conventional bot.py."""
    pp = Path(path)
    name = pp.stem
    if name in ("bot",):
        name = pp.parent.name or name
    return prefix + name


def _accumulate(dst_err, dst_tim, res) -> None:
    """Fold one match's per-bot errors/timing into running totals."""
    for bid, c in (res.get("errors") or {}).items():
        dst_err[bid] = dst_err.get(bid, 0) + c
    for bid, tinfo in (res.get("timing") or {}).items():
        agg = dst_tim.setdefault(bid, {"max": 0.0, "slow": 0})
        agg["max"] = max(agg["max"], (tinfo or {}).get("max", 0.0))
        agg["slow"] += (tinfo or {}).get("slow", 0)


def _ev_of(res) -> dict:
    """EV-adjusted per-seat deltas for a match, falling back to realized if the
    harness didn't provide an `ev_chip_delta` (older harness / EV disabled)."""
    return res.get("ev_chip_delta") or res.get("chip_delta") or {}


# ---------------------------------------------------------------------------
# Parallel match dispatch (process pool)
# ---------------------------------------------------------------------------
# Each match is independent (its own seed_base+i and its own freshly built
# decide_map), so the per-match for-loops in _run_eval / _run_multi are
# embarrassingly parallel. We dispatch via ProcessPoolExecutor with one task
# per match index. For _run_multi each task does the whole match-index ROUND
# (all heroes against the same field at the same seat/seed) so the paired
# (hero − baseline) per-match alignment that the rest of analysis.py relies
# on stays exact: per-hero arrays are filled by index `i`, not arrival order.
#
# Why processes, not threads:
#   * Each worker has its own sys.modules → load_decide's _bt_bot_N uniqueness
#     and the bot-state-reset-per-match guarantee survive across workers.
#   * The per-action budget is wall-clock inside run_match_inproc; if any
#     signal.alarm path is used it only works from a process's main thread,
#     which threads would violate.
#   * eval7 / engine code is CPU-bound; processes sidestep the GIL.
#
# Why one task per match (not per-hero sub-match in _run_multi): keeps the
# paired-round structure visible as the unit of progress, and at 5 heroes per
# round × ~5s each, round granularity is already small relative to total work.

def _pool_init(sys_path_entries, repo_path):
    """ProcessPoolExecutor initializer. Runs once per worker process: rebuilds
    sys.path so `import backtest` resolves the same way it did in the parent,
    then imports backtest and binds the harness primitives into this module's
    globals (which is what _eval_match_worker / _multi_round_worker rely on).

    With the 'fork' start method (Linux default) workers already inherit the
    parent's sys.path and imported modules — running this init is a cheap
    no-op there. With 'spawn' (Windows; macOS Python 3.8+) workers start
    clean and this is doing the real work."""
    for p in sys_path_entries:
        if p and p not in sys.path:
            sys.path.insert(0, p)
    if repo_path and repo_path not in sys.path:
        sys.path.insert(0, repo_path)
    import backtest as _bt  # noqa: WPS433  intentional lazy import in worker
    bind_harness(_bt)


def _eval_match_worker(payload):
    """One eval-mode match. Pure function of its payload; no shared state with
    the parent or other workers. Returns (i, match_result_dict) so the parent
    can fill per-bot[i] regardless of completion order.

    Builds a fresh decide_map per call — matches reload=True semantics, which
    is the default and the only mode parallel eval supports (the dashboard
    guards this; serial path still honors reload=False via its cached map)."""
    (ids, id_to_path, hands, seed_base, i, rotate_seats,
     budget, fold_on_timeout) = payload
    seed = seed_base + i
    order = ids[:]
    if rotate_seats:
        k = i % len(order)
        order = order[k:] + order[:k]
    else:
        random.Random(seed * 7919 + 1).shuffle(order)
    dm = build_decide_map({b: id_to_path[b] for b in order})
    res = run_match_inproc(dm, hands, seed, budget=budget,
                           fold_on_timeout=fold_on_timeout)
    return i, res


def _multi_round_worker(payload):
    """One paired-multi match-index ROUND: every hero plays its sub-match
    against the same field at the same seat on the same seed. Returns
    (i, [(hero_id, sub_match_result), ...]) so the parent stitches results
    back into per-hero arrays indexed by `i`.

    Note on a known sub-optimality preserved from the serial code: the field
    is reloaded N times per round (once per hero), where loading it once and
    only swapping the hero seat would be enough. Keeping serial behavior so
    the only thing changing here is the parallelism axis; that load-once
    optimization is a separate follow-up."""
    (hero_ids, hero_path, field_ids, field_map, hands, seed_base, i, n_seats,
     budget, fold_on_timeout) = payload
    seed = seed_base + i
    hero_idx = i % n_seats
    out = []
    for hid in hero_ids:
        order = field_ids[:]
        order.insert(hero_idx, hid)
        paths = {bid: (hero_path[hid] if bid == hid else field_map[bid])
                 for bid in order}
        dm = build_decide_map({b: paths[b] for b in order})
        res = run_match_inproc(dm, hands, seed, budget=budget,
                               fold_on_timeout=fold_on_timeout)
        out.append((hid, res))
    return i, out


def default_workers() -> int:
    """Conservative-but-useful default: one less than the visible CPU count,
    floored at 1. Lets app.py pick a sensible initial value without each
    caller redoing the os.cpu_count() dance."""
    return max(1, (os.cpu_count() or 2) - 1)


def _run_eval(hero_path, opponent_paths, matches, hands, seed_base,
              budget, fold_on_timeout, reload, rotate_seats,
              progress_callback=None, workers=1, worker_repo_path=None) -> dict:
    """One hero vs a field of opponents. opponent_paths may contain duplicate
    paths — make_ids gives each a distinct id (foo, foo_2, …) and build_decide_map
    loads each under a unique module name, so duplicates seat independently with
    independent state. Mirrors backtest.cmd_eval's seating/seeding.

    Returns realized per-match deltas, EV-adjusted per-match deltas, and a
    per-bot per-match placement series (all bots share one match, so their ranks
    are mutually consistent 1..n).

    `workers > 1` dispatches matches across a ProcessPoolExecutor. Parallel mode
    requires reload=True (each match must build its own decide_map; reload=False
    relies on a parent-process cache that can't be shared across workers without
    pickling decide callables, which load_decide can't promise). When the caller
    asks for parallel + reload=False we silently fall back to the serial path
    rather than fail — the dashboard surfaces a warning for that combination."""
    paths = [hero_path] + list(opponent_paths)
    ids = make_ids(paths)
    hero_id = ids[0]
    id_to_path = dict(zip(ids, paths))

    # Pre-allocated per-bot arrays so out-of-order future completion still
    # produces lists indexed by match index `i`. Serial path uses the same
    # layout for symmetry — produces identical output to the old .append() form.
    per_bot = {bid: [None] * matches for bid in ids}           # realized chip Δ
    per_bot_ev = {bid: [None] * matches for bid in ids}        # EV-adjusted Δ
    per_bot_placement = {bid: [None] * matches for bid in ids} # finish rank
    errors_total: dict = {}
    timing_total: dict = {}

    use_parallel = workers > 1 and reload and matches > 1

    def _absorb(i, res):
        """Common per-match folding logic — shared between serial and parallel
        branches so they can't drift."""
        chip = res["chip_delta"]
        evd = _ev_of(res)
        for bid in ids:
            per_bot[bid][i] = chip[bid]
            per_bot_ev[bid][i] = evd.get(bid, chip[bid])
            per_bot_placement[bid][i] = placement(bid, chip)
        _accumulate(errors_total, timing_total, res)

    t0 = time.time()
    if use_parallel:
        n_workers = min(workers, matches)
        payloads = [
            (ids, id_to_path, hands, seed_base, i, rotate_seats,
             budget, fold_on_timeout)
            for i in range(matches)
        ]
        done = 0
        with ProcessPoolExecutor(
            max_workers=n_workers,
            initializer=_pool_init,
            initargs=(list(sys.path), worker_repo_path),
        ) as pool:
            futures = [pool.submit(_eval_match_worker, p) for p in payloads]
            for fut in as_completed(futures):
                i, res = fut.result()
                _absorb(i, res)
                done += 1
                if progress_callback:
                    progress_callback(done, matches)
    else:
        cached = None if reload else build_decide_map(id_to_path)
        for i in range(matches):
            seed = seed_base + i
            order = ids[:]
            if rotate_seats:
                k = i % len(order)
                order = order[k:] + order[:k]
            else:
                random.Random(seed * 7919 + 1).shuffle(order)

            dm = (build_decide_map({b: id_to_path[b] for b in order})
                  if reload else {b: cached[b] for b in order})

            res = run_match_inproc(dm, hands, seed, budget=budget,
                                   fold_on_timeout=fold_on_timeout)
            _absorb(i, res)

            if progress_callback:
                progress_callback(i + 1, matches)

    elapsed = time.time() - t0
    stats = {bid: summarize(per_bot[bid], hands) for bid in ids}
    return {
        "elapsed_s": round(elapsed, 2),
        "hero_id": hero_id,
        "stats": stats,
        "per_match_deltas": per_bot,
        "per_match_ev_deltas": per_bot_ev,
        "per_match_placement": per_bot_placement,
        "errors": errors_total,
        "timing": timing_total,
    }


def _run_multi(hero_specs, baseline_id, field_paths, matches, hands, seed_base,
               budget, fold_on_timeout, progress_callback=None,
               workers=1, worker_repo_path=None) -> dict:
    """Paired N-way comparison (2..7 heroes) on identical seeds, identical seat,
    identical (freshly loaded) field. Generalises the old A-vs-B path: at each
    match index every hero plays its OWN match against the same field, in the
    same seat, on the same seed — so all heroes are mutually paired and each
    variant's per-match delta lines up index-for-index with every other's.

    hero_specs: list of {"id", "path"} (caller guarantees 2..7 unique ids).
    baseline_id: the hero id everyone else is compared against (paired diff).

    Per hero we record realized + EV-adjusted per-match deltas and the hero's
    finish rank in its own sub-match (1..n_seats).

    Note: the engine's 9-seat cap applies to field + 1 hero per sub-match, NOT
    to the number of heroes — each hero is seated alone against the field.

    `workers > 1` dispatches whole match-index rounds (all heroes for one i)
    across a ProcessPoolExecutor; per-hero per-match arrays are filled by `i`
    (not arrival order) so the paired (hero − baseline) diff stays
    index-aligned no matter what order futures complete in. Always safe to
    parallelise here — multi mode is always reload-per-match by construction."""
    field_ids = make_ids(list(field_paths))
    field_map = dict(zip(field_ids, field_paths))
    n_seats = len(field_ids) + 1

    # Seat label per hero. Ids are unique already; only disambiguate the rare
    # case where a hero id collides with a generated field id.
    hero_ids, hero_path = [], {}
    field_set = set(field_ids)
    for h in hero_specs:
        hid = h["id"]
        if hid in field_set or hid in hero_path:
            hid = hid + "#h"
        hero_ids.append(hid)
        hero_path[hid] = h["path"]
    if baseline_id not in hero_path:           # baseline got disambiguated too
        baseline_id = hero_ids[0]

    # Pre-allocated arrays: index-based fill survives out-of-order futures.
    deltas = {hid: [None] * matches for hid in hero_ids}       # realized chip Δ
    ev_deltas = {hid: [None] * matches for hid in hero_ids}    # EV-adjusted Δ
    placements = {hid: [None] * matches for hid in hero_ids}   # finish rank
    errors_total: dict = {}
    timing_total: dict = {}

    def _absorb_round(i, round_results):
        for hid, res in round_results:
            _accumulate(errors_total, timing_total, res)
            chip = res["chip_delta"]
            evd = _ev_of(res)
            deltas[hid][i] = chip[hid]
            ev_deltas[hid][i] = evd.get(hid, chip[hid])
            placements[hid][i] = placement(hid, chip)

    use_parallel = workers > 1 and matches > 1

    t0 = time.time()
    if use_parallel:
        n_workers = min(workers, matches)
        payloads = [
            (hero_ids, hero_path, field_ids, field_map, hands, seed_base, i,
             n_seats, budget, fold_on_timeout)
            for i in range(matches)
        ]
        done = 0
        with ProcessPoolExecutor(
            max_workers=n_workers,
            initializer=_pool_init,
            initargs=(list(sys.path), worker_repo_path),
        ) as pool:
            futures = [pool.submit(_multi_round_worker, p) for p in payloads]
            for fut in as_completed(futures):
                i, round_results = fut.result()
                _absorb_round(i, round_results)
                done += 1
                if progress_callback:
                    progress_callback(done, matches)
    else:
        for i in range(matches):
            seed = seed_base + i
            hero_idx = i % n_seats  # same seat for every hero at this seed

            def seated(hid):
                order = field_ids[:]
                order.insert(hero_idx, hid)
                paths = {bid: (hero_path[hid] if bid == hid else field_map[bid])
                         for bid in order}
                dm = build_decide_map({b: paths[b] for b in order})
                return run_match_inproc(dm, hands, seed, budget=budget,
                                        fold_on_timeout=fold_on_timeout)

            round_results = [(hid, seated(hid)) for hid in hero_ids]
            _absorb_round(i, round_results)

            if progress_callback:
                progress_callback(i + 1, matches)

    elapsed = time.time() - t0

    # Per-hero absolute stats.
    stats = {hid: summarize(deltas[hid], hands) for hid in hero_ids}

    # Paired (hero - baseline) per match, for every non-baseline hero.
    base_deltas = deltas[baseline_id]
    paired = {}        # hid -> list of per-match (hid - baseline)
    vs_baseline = {}   # hid -> {"stats": summarize(paired), "t_stat": ...}
    for hid in hero_ids:
        if hid == baseline_id:
            continue
        pd = [a - b for a, b in zip(deltas[hid], base_deltas)]
        paired[hid] = pd
        sd = summarize(pd, hands)
        t_stat = (sd["mean_delta"] / sd["stderr"]) if sd["stderr"] else 0.0
        vs_baseline[hid] = {"stats": sd, "t_stat": t_stat}

    return {
        "elapsed_s": round(elapsed, 2),
        "baseline": baseline_id,
        "hero_ids": hero_ids,
        "stats": stats,                       # absolute, per hero
        "vs_baseline": vs_baseline,           # paired diff vs baseline
        "per_match_deltas": deltas,           # absolute realized, per hero
        "per_match_ev_deltas": ev_deltas,     # absolute EV-adjusted, per hero
        "per_match_placement": placements,    # finish rank, per hero
        "per_match_paired": paired,           # (hero - baseline), per non-baseline hero
        "errors": errors_total,
        "timing": timing_total,
    }


# ---------------------------------------------------------------------------
# Result IO / schema
# ---------------------------------------------------------------------------

SCHEMA_VERSION = 3


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
    """Persist one JSON per job (schema_version 3).

    New in v3: `per_match_ev_deltas` and `per_match_placement` are persisted, and
    `params` is stamped with `big_blind` / `starting_stack` so the Results tab is
    self-describing (no engine import needed for bb/100). v2 files still load via
    `normalize_result` — the new fields are optional."""
    # Self-describing params: stamp engine constants at write time (don't clobber
    # any values a caller already set).
    params = dict(job.get("params") or {})
    params.setdefault("big_blind", BIG_BLIND)
    params.setdefault("starting_stack", STARTING_STACK)

    base = {
        "schema_version": SCHEMA_VERSION,
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
        "params": params,
        "elapsed_s": (result or {}).get("elapsed_s"),
        "stats": (result or {}).get("stats"),
        "per_match_deltas": (result or {}).get("per_match_deltas"),
        "per_match_ev_deltas": (result or {}).get("per_match_ev_deltas"),
        "per_match_placement": (result or {}).get("per_match_placement"),
        "errors_per_bot": (result or {}).get("errors"),
        "timing_per_bot": (result or {}).get("timing"),
        "error": error,
    }
    if job["mode"] == "eval":
        base["hero"] = job["hero"]
    elif job["mode"] == "ab":
        base["heroes"] = job["heroes"]
        base["baseline"] = job["baseline"]
        base["vs_baseline"] = (result or {}).get("vs_baseline")
        base["per_match_paired"] = (result or {}).get("per_match_paired")

    out_path = Path(results_dir) / f"{job['job_id']}.json"
    out_path.write_text(json.dumps(out_path_safe(base), indent=2))
    return out_path


def _translate_legacy(d: dict) -> dict:
    """Translate v1 (CLI-flat) JSON files into the v2 dashboard shape. No-op on
    anything already v2/v3-shaped or unrecognised (pass-through; the inspector
    degrades gracefully via .get())."""
    if d.get("schema_version") in (2, 3) or "metadata" in d:
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


def normalize_result(d: dict) -> dict:
    """Load any result dict (v1 / v2 / v3) into the dashboard shape and flag
    whether it carries the EV-adjusted series. v1 files are translated to the v2
    shape; v2/v3 pass through. Sets `_realized_only = True` when no
    `per_match_ev_deltas` is present (v1/v2: realized-only — can't EV-adjust or
    show the EV/placement series), False for v3 results that carry it."""
    d = _translate_legacy(d)
    d["_realized_only"] = not bool(d.get("per_match_ev_deltas"))
    return d


# Back-compat alias: app.py historically called _normalize_legacy_result.
_normalize_legacy_result = normalize_result

# ===========================================================================
# Step 3 — Stats engine (shared; recompute live from per-match series)
# ===========================================================================
#
# All functions below are pure (no streamlit). The dashboard recomputes every
# matrix/table cell from the stored per-match series so the baseline, series
# (realized / EV / placement) and metric are all selectable live. Ground truth
# for the tests is scipy; the engine only *uses* scipy if it is importable —
# otherwise it falls back to a self-contained Student-t so the dashboard never
# hard-depends on scipy.

try:                                            # optional acceleration only
    from scipy import stats as _scipy_stats     # noqa: WPS433
    _HAS_SCIPY = True
except Exception:                               # pragma: no cover - env dependent
    _scipy_stats = None
    _HAS_SCIPY = False


# --- Self-contained Student-t (regularized incomplete beta) ----------------
# Numerical-Recipes-style continued fraction for the regularized incomplete
# beta I_x(a, b); accurate to ~1e-12, far inside the 1e-6 the tests demand.

def _betacf(a: float, b: float, x: float) -> float:
    MAXIT, EPS, FPMIN = 300, 3.0e-16, 1.0e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < FPMIN:
        d = FPMIN
    d = 1.0 / d
    h = d
    for m in range(1, MAXIT + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < EPS:
            break
    return h


def _betai(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta I_x(a, b), x in [0, 1]."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lbeta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    bt = math.exp(lbeta + a * math.log(x) + b * math.log1p(-x))
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def _t_sf_pure(t: float, df: float) -> float:
    """One-sided upper-tail survival P(T > t) of Student-t (self-contained)."""
    if df <= 0 or t != t:                       # df<=0 or NaN
        return float("nan")
    x = df / (df + t * t)
    half = 0.5 * _betai(df / 2.0, 0.5, x)       # = P(T > |t|)
    return half if t >= 0 else 1.0 - half


def _t_two_sided_p_pure(t: float, df: float) -> float:
    """Two-sided p = I_x(df/2, 1/2), x = df/(df+t^2). Equals 2*sf(|t|)."""
    if df <= 0 or t != t:
        return float("nan")
    x = df / (df + t * t)
    return _betai(df / 2.0, 0.5, x)


def _t_cdf_pure(t: float, df: float) -> float:
    return 1.0 - _t_sf_pure(t, df)


def _t_ppf_pure(q: float, df: float) -> float:
    """Inverse CDF (quantile) of Student-t by bisection on the self-contained
    CDF. Used for the critical value of the CI when scipy is absent."""
    if df <= 0 or not (0.0 < q < 1.0):
        return float("nan")
    if q == 0.5:
        return 0.0
    # Symmetric, monotone increasing; bracket generously then bisect tight.
    lo, hi = -1.0e6, 1.0e6
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if _t_cdf_pure(mid, df) < q:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1.0e-12 * max(1.0, abs(mid)):
            break
    return 0.5 * (lo + hi)


# --- Public scipy-or-pure wrappers (use_scipy=None ⇒ auto) -----------------

def student_t_two_sided_p(t: float, df: float, use_scipy=None) -> float:
    use_scipy = _HAS_SCIPY if use_scipy is None else use_scipy
    if use_scipy and _scipy_stats is not None:
        return float(2.0 * _scipy_stats.t.sf(abs(t), df))
    return _t_two_sided_p_pure(t, df)


def student_t_ppf(q: float, df: float, use_scipy=None) -> float:
    use_scipy = _HAS_SCIPY if use_scipy is None else use_scipy
    if use_scipy and _scipy_stats is not None:
        return float(_scipy_stats.t.ppf(q, df))
    return _t_ppf_pure(q, df)


# --- Wilcoxon signed-rank (normal approximation, scipy-'approx' compatible) -
# Matches scipy.stats.wilcoxon(..., method='approx', correction=False,
# zero_method='wilcox') — appropriate for the dozens-of-matches regime here and
# version-independent (no exact/auto mode switch to track).

def _rankdata_avg(values):
    """Average ranks (1-based), ties share the mean of their rank span."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0               # mean of ranks [i+1 .. j+1]
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _norm_sf(z: float) -> float:
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def wilcoxon_p(diffs, use_scipy=None) -> float:
    """Two-sided Wilcoxon signed-rank p-value (normal approximation)."""
    d = [x for x in diffs if x != 0]            # zero_method='wilcox'
    n = len(d)
    if n == 0:
        return float("nan")
    use_scipy = _HAS_SCIPY if use_scipy is None else use_scipy
    if use_scipy and _scipy_stats is not None:
        try:
            return float(_scipy_stats.wilcoxon(
                d, alternative="two-sided", method="approx",
                correction=False, zero_method="wilcox").pvalue)
        except Exception:                       # pragma: no cover
            pass
    absd = [abs(x) for x in d]
    ranks = _rankdata_avg(absd)
    r_plus = sum(r for r, x in zip(ranks, d) if x > 0)
    r_minus = sum(r for r, x in zip(ranks, d) if x < 0)
    T = min(r_plus, r_minus)
    mn = n * (n + 1) * 0.25
    se2 = n * (n + 1) * (2 * n + 1)
    # tie correction (same form scipy uses)
    counts = {}
    for v in absd:
        counts[v] = counts.get(v, 0) + 1
    for c in counts.values():
        if c > 1:
            se2 -= 0.5 * c * (c * c - 1)
    se = math.sqrt(se2 / 24.0)
    if se == 0:
        return 1.0
    z = (T - mn) / se
    return 2.0 * _norm_sf(abs(z))


# --- Holm–Bonferroni step-down adjustment ----------------------------------

def holm_adjust(pvals):
    """Holm step-down adjusted p-values, returned in the input order.
    Matches statsmodels.multipletests(method='holm')."""
    m = len(pvals)
    if m == 0:
        return []
    order = sorted(range(m), key=lambda i: pvals[i])     # ascending by p
    adj_sorted = [0.0] * m
    running = 0.0
    for rank, idx in enumerate(order):
        val = (m - rank) * pvals[idx]
        running = max(running, val)             # enforce monotonicity
        adj_sorted[rank] = min(running, 1.0)
    out = [0.0] * m
    for rank, idx in enumerate(order):
        out[idx] = adj_sorted[rank]
    return out


# --- bb/100 -----------------------------------------------------------------

def bb_per_100(chips: float, hands, big_blind) -> float:
    """Exact bb/100: chips * 100 / (hands * big_blind)."""
    if not hands or not big_blind:
        return float("nan")
    return chips * 100.0 / (hands * big_blind)


# --- Bootstrap CI of the mean (deterministic) ------------------------------

def _bootstrap_ci_mean(d, n_boot=2000, alpha=0.05, seed=0xC0FFEE):
    n = len(d)
    if n == 0:
        return float("nan"), float("nan")
    rng = random.Random(seed)
    means = []
    for _ in range(n_boot):
        s = 0.0
        for _ in range(n):
            s += d[rng.randrange(n)]
        means.append(s / n)
    means.sort()
    lo_i = max(0, min(n_boot - 1, int(math.floor((alpha / 2.0) * n_boot))))
    hi_i = max(0, min(n_boot - 1, int(math.ceil((1.0 - alpha / 2.0) * n_boot)) - 1))
    return means[lo_i], means[hi_i]


# --- The headline paired-stats routine -------------------------------------

def paired_stats(series_cand, series_base, hands, big_blind, *,
                 alpha=0.05, n_boot=2000, boot_seed=0xC0FFEE, use_scipy=None):
    """Paired (cand − base) statistics, recomputed live from two aligned
    per-match series.

    Returns a dict with: n, mean_chips, mean_bb_per_100, sd, stderr, t, df,
    p_value (two-sided), t_crit, ci_low/ci_high (mean ± t_crit·stderr),
    median_chips, wilcoxon_p, boot_ci_low/high, pairing_efficiency, corr,
    unpaired_stderr.

    The t / p / CI use scipy when importable, else the self-contained Student-t
    (set use_scipy=False to force the pure path, e.g. in tests)."""
    n = min(len(series_cand), len(series_base))
    d = [series_cand[i] - series_base[i] for i in range(n)]

    base = {
        "n": n, "mean_chips": float("nan"), "mean_bb_per_100": float("nan"),
        "sd": float("nan"), "stderr": float("nan"), "t": float("nan"),
        "df": max(n - 1, 0), "p_value": float("nan"), "t_crit": float("nan"),
        "ci_low": float("nan"), "ci_high": float("nan"),
        "median_chips": float("nan"), "wilcoxon_p": float("nan"),
        "boot_ci_low": float("nan"), "boot_ci_high": float("nan"),
        "pairing_efficiency": float("nan"), "corr": float("nan"),
        "unpaired_stderr": float("nan"),
    }
    if n == 0:
        return base
    mean = sum(d) / n
    base["mean_chips"] = mean
    base["mean_bb_per_100"] = bb_per_100(mean, hands, big_blind)
    base["median_chips"] = statistics.median(d)
    if n < 2:
        return base

    var = sum((x - mean) ** 2 for x in d) / (n - 1)     # sample var, ddof=1
    sd = math.sqrt(var)
    stderr = sd / math.sqrt(n)
    df = n - 1
    base.update(sd=sd, stderr=stderr, df=df)

    if stderr == 0:
        t = 0.0 if mean == 0 else math.copysign(float("inf"), mean)
        base.update(t=t, p_value=(1.0 if mean == 0 else 0.0),
                    t_crit=float("nan"), ci_low=mean, ci_high=mean)
    else:
        t = mean / stderr
        tcrit = student_t_ppf(1.0 - alpha / 2.0, df, use_scipy=use_scipy)
        base.update(
            t=t,
            p_value=student_t_two_sided_p(t, df, use_scipy=use_scipy),
            t_crit=tcrit,
            ci_low=mean - tcrit * stderr,
            ci_high=mean + tcrit * stderr,
        )

    base["wilcoxon_p"] = wilcoxon_p(d, use_scipy=use_scipy)
    base["boot_ci_low"], base["boot_ci_high"] = _bootstrap_ci_mean(
        d, n_boot=n_boot, alpha=alpha, seed=boot_seed)

    # Pairing efficiency: unpaired (independent-samples) stderr ÷ paired stderr.
    cand = list(series_cand[:n])
    bse = list(series_base[:n])
    mc = sum(cand) / n
    mb = sum(bse) / n
    var_c = sum((x - mc) ** 2 for x in cand) / (n - 1)
    var_b = sum((x - mb) ** 2 for x in bse) / (n - 1)
    unpaired_se = math.sqrt((var_c + var_b) / n)
    base["unpaired_stderr"] = unpaired_se
    base["pairing_efficiency"] = (unpaired_se / stderr) if stderr else float("nan")
    denom = math.sqrt(var_c * var_b)
    if denom > 0:
        cov = sum((a - mc) * (b - mb) for a, b in zip(cand, bse)) / (n - 1)
        base["corr"] = cov / denom
    return base


# ===========================================================================
# Step 3 — Comparison-set grouping (by hero-id set; probe columns; dedup)
# ===========================================================================

def hero_set_key(result: dict) -> tuple:
    """Identity of a comparison set = sorted tuple of hero ids. ab results use
    `heroes`; eval results fall back to the single `hero`."""
    heroes = result.get("heroes")
    if heroes:
        return tuple(sorted(h.get("id") for h in heroes if h.get("id")))
    hero = result.get("hero")
    if isinstance(hero, dict) and hero.get("id"):
        return (hero["id"],)
    return ()


def field_signature(result: dict) -> str:
    """Collapsed field signature, e.g. 'min_ball×2, overbet_polar×2'. Sorted by
    id; counts >1 shown as 'id×k'."""
    counts = {}
    for o in (result.get("opponents") or []):
        oid = o.get("id", "?")
        counts[oid] = counts.get(oid, 0) + 1
    parts = []
    for oid in sorted(counts):
        c = counts[oid]
        parts.append(f"{oid}×{c}" if c > 1 else oid)
    return ", ".join(parts)


def probe_label(result: dict) -> str:
    """Column key within a comparison set: the preset name if present, else the
    collapsed field signature."""
    return result.get("preset_name") or field_signature(result) or "(no field)"


def _completed_at(result: dict) -> str:
    return ((result.get("metadata") or {}).get("completed_at")) or ""


def group_results(results):
    """Group normalized result dicts into comparison sets.

    Returns an ordered dict keyed by hero_set_key →
        {"heroes": [ids...],
         "probes": {probe_label: {"runs": [results sorted oldest→newest],
                                  "winner": newest result, "n_runs": k}}}
    Newest-wins dedup per (set, probe) by `completed_at` (lexicographic on ISO
    timestamps; empty timestamps sort first so a stamped run beats an unstamped
    one). Insertion order of sets follows first appearance in `results`."""
    sets = {}
    for r in results:
        key = hero_set_key(r)
        if not key:
            continue
        grp = sets.setdefault(key, {"heroes": list(key), "probes": {}})
        probe = probe_label(r)
        grp["probes"].setdefault(probe, {"runs": []})["runs"].append(r)
    for grp in sets.values():
        for bucket in grp["probes"].values():
            runs = sorted(bucket["runs"], key=_completed_at)
            bucket["runs"] = runs
            bucket["n_runs"] = len(runs)
            bucket["winner"] = runs[-1]
    return sets


def most_recent_set_key(results):
    """Hero-set key of the most recently completed result (default selection)."""
    best, best_ts = None, None
    for r in results:
        key = hero_set_key(r)
        if not key:
            continue
        ts = _completed_at(r)
        if best_ts is None or ts > best_ts:
            best, best_ts = key, ts
    return best


# ===========================================================================
# Step 3 — Hero × preset matrix (cell = paired (cand − base) on a series)
# ===========================================================================

SERIES_KEYS = {
    "realized": "per_match_deltas",
    "ev": "per_match_ev_deltas",
    "placement": "per_match_placement",
}


def series_arrays(result: dict, series: str) -> dict:
    """The stored per-match arrays for a series ('realized'|'ev'|'placement'),
    as {hero_id: [values...]}. Empty dict if the series is absent (e.g. a
    realized-only legacy file has no EV/placement)."""
    return result.get(SERIES_KEYS[series]) or {}


def hands_and_bb(result: dict):
    """(hands, big_blind) from the self-describing params, with engine-constant
    fallbacks for legacy files that didn't stamp them."""
    params = result.get("params") or {}
    hands = params.get("hands") or 0
    bb = params.get("big_blind") or BIG_BLIND
    return hands, bb


def matrix_cell(result: dict, hero_id: str, baseline_id: str, series: str, *,
                use_scipy=None):
    """Paired (hero_id − baseline_id) stats on the chosen series, recomputed
    live from the stored per-match arrays. None if either series is missing."""
    arrs = series_arrays(result, series)
    cand = arrs.get(hero_id)
    base = arrs.get(baseline_id)
    if not cand or not base:
        return None
    hands, bb = hands_and_bb(result)
    return paired_stats(cand, base, hands, bb, use_scipy=use_scipy)


# Which field of a paired_stats dict each matrix metric reads, plus a label.
MATRIX_METRICS = {
    "bb/100": ("mean_bb_per_100", "mean Δ bb/100"),
    "chips": ("mean_chips", "mean Δ chips"),
    "t": ("t", "paired t"),
    "p": ("p_value", "two-sided p"),
}


def build_matrix(result: dict, baseline_id: str, hero_ids, series: str,
                 metric: str, *, alpha=0.05, use_scipy=None):
    """Build one comparison matrix for a single result/probe.

    Returns {"baseline": ..., "series": ..., "metric": ...,
             "rows": [{"hero", "cell"(full paired_stats), "value"(metric),
                       "holm_p", "raw_p", "significant"(Holm@alpha)}],
             "holm_alpha": alpha}.
    Rows are the non-baseline heroes in `hero_ids` order. Holm is applied across
    the raw two-sided p-values of the rows in THIS matrix."""
    field, _label = MATRIX_METRICS[metric]
    rows = []
    raw_ps = []
    for hid in hero_ids:
        if hid == baseline_id:
            continue
        cell = matrix_cell(result, hid, baseline_id, series, use_scipy=use_scipy)
        if cell is None:
            continue
        rows.append({"hero": hid, "cell": cell,
                     "value": cell.get(field), "raw_p": cell.get("p_value")})
        raw_ps.append(cell.get("p_value"))
    # Holm across the rows shown in this matrix.
    clean = [p if isinstance(p, (int, float)) and p == p else 1.0 for p in raw_ps]
    holm = holm_adjust(clean)
    for row, hp in zip(rows, holm):
        row["holm_p"] = hp
        row["significant"] = isinstance(hp, (int, float)) and hp < alpha
    return {"baseline": baseline_id, "series": series, "metric": metric,
            "rows": rows, "holm_alpha": alpha}


def absolute_matrix(result: dict, hero_ids, series: str):
    """Collapsed companion: each hero's ABSOLUTE per-match series summary on the
    chosen series (all heroes incl. baseline). Returns
    {hero_id: {"n", "mean_chips", "mean_bb_per_100", "median_chips"}}."""
    arrs = series_arrays(result, series)
    hands, bb = hands_and_bb(result)
    out = {}
    for hid in hero_ids:
        vals = arrs.get(hid)
        if not vals:
            continue
        n = len(vals)
        mean = sum(vals) / n
        out[hid] = {
            "n": n,
            "mean_chips": mean,
            "mean_bb_per_100": bb_per_100(mean, hands, bb),
            "median_chips": statistics.median(vals),
        }
    return out

# ===========================================================================
# Step 4 — drill-down (pairwise table), series selection, placement, CSV
# ===========================================================================
#
# Pure logic only (no streamlit). Everything recomputes live from the stored
# per-match series via paired_stats, so the drill-down, histogram and CSVs are
# all consistent with the matrix.

# --- Series sign/units helpers (placement is a rank diff: lower = better) ---

def metrics_for_series(series: str):
    """Allowed matrix/table metrics for a series. bb/100 is meaningless on
    ranks, so the placement series drops it (chips = mean rank diff, t, p)."""
    if series == "placement":
        return ["chips", "t", "p"]
    return ["bb/100", "chips", "t", "p"]


def series_lower_is_better(series: str) -> bool:
    """True when a SMALLER value is better on this series (placement ranks)."""
    return series == "placement"


def matrix_color_value(value, series: str):
    """Value to drive the diverging color so the *better* direction is always
    the positive (high) end. For placement (lower rank = better) the sign is
    flipped, so a hero that beats the baseline (negative Δ rank) colors as
    'better'."""
    if value is None or (isinstance(value, float) and value != value):
        return value
    return -value if series_lower_is_better(series) else value


def matrix_value_label(series: str, metric: str) -> str:
    """Column/value caption for a (series, metric). Placement gets the
    '− = better' annotation since its sign is inverted vs chips."""
    if series == "placement":
        return {
            "chips": "Δ rank (− = better)",
            "t": "rank t (− = better)",
            "p": "rank two-sided p",
        }.get(metric, metric)
    return MATRIX_METRICS.get(metric, (None, metric))[1]


# --- Hero-id resolution -----------------------------------------------------

def hero_ids_of(result: dict):
    """Ordered hero ids of a result: `heroes` (ab) → `hero` (eval) → fall back
    to the keys present in the realized per-match series."""
    heroes = result.get("heroes")
    if heroes:
        return [h.get("id") for h in heroes if h.get("id")]
    hero = result.get("hero")
    if isinstance(hero, dict) and hero.get("id"):
        return [hero["id"]]
    return list((result.get("per_match_deltas") or {}).keys())


def field_list(result: dict):
    """Raw opponent ids in order, duplicates PRESERVED (e.g. ['min_ball',
    'min_ball', 'overbet_polar']). Distinct from `field_signature`, which
    collapses to 'id×k'."""
    return [o.get("id", "?") for o in (result.get("opponents") or [])]


# --- Long-form pairwise paired table ---------------------------------------

# Scalar columns of one pairwise row, in display/CSV order.
PAIRWISE_COLUMNS = [
    "pair", "cand", "base", "n", "mean_chips", "mean_bb_per_100", "stderr",
    "t", "df", "p_value", "holm_p", "ci_low", "ci_high", "median_chips",
    "wilcoxon_p",
]


def pairwise_table(result: dict, series: str, baseline=None, *,
                   alpha=0.05, use_scipy=None):
    """Long-form paired table on `series`. With `baseline` set → each
    non-baseline hero vs the baseline; otherwise → every unordered hero pair
    (i<j with cand=heroes[i], base=heroes[j]). Holm is applied across all rows
    of the table. Each row carries the full paired_stats plus 'pair', 'cand',
    'base', 'holm_p', 'significant'."""
    arrs = series_arrays(result, series)
    hero_ids = [h for h in hero_ids_of(result) if h in arrs]
    hands, bb = hands_and_bb(result)

    pairs = []
    if baseline is not None and baseline in arrs:
        for hid in hero_ids:
            if hid != baseline:
                pairs.append((hid, baseline))
    else:
        for i in range(len(hero_ids)):
            for j in range(i + 1, len(hero_ids)):
                pairs.append((hero_ids[i], hero_ids[j]))

    rows, raw_ps = [], []
    for cand, base in pairs:
        ps = paired_stats(arrs[cand], arrs[base], hands, bb,
                          alpha=alpha, use_scipy=use_scipy)
        row = {"pair": f"{cand} − {base}", "cand": cand, "base": base}
        row.update(ps)
        rows.append(row)
        raw_ps.append(ps.get("p_value"))
    clean = [p if isinstance(p, (int, float)) and p == p else 1.0 for p in raw_ps]
    for row, hp in zip(rows, holm_adjust(clean)):
        row["holm_p"] = hp
        row["significant"] = isinstance(hp, (int, float)) and hp < alpha
    return rows


# --- Series selector for the histogram -------------------------------------

def select_series_array(result: dict, series: str, hero_id: str,
                        baseline_id=None, paired: bool = False):
    """Return the per-match array to histogram for one choice.

    paired=False → the hero's ABSOLUTE series ('realized'|'ev'|'placement').
    paired=True with a distinct baseline → the elementwise paired diff
    (hero − baseline) on that series. Empty list if the series/hero is absent."""
    arrs = series_arrays(result, series)
    if paired and baseline_id and baseline_id != hero_id:
        cand = arrs.get(hero_id) or []
        base = arrs.get(baseline_id) or []
        n = min(len(cand), len(base))
        return [cand[i] - base[i] for i in range(n)]
    return list(arrs.get(hero_id) or [])


def realized_ev_gap(result: dict, hero_id: str):
    """Per-hero realized − EV gap (a luck indicator): (mean_realized,
    mean_ev, gap). NaNs if a series is missing."""
    rz = (result.get("per_match_deltas") or {}).get(hero_id) or []
    ev = (result.get("per_match_ev_deltas") or {}).get(hero_id) or []
    mr = (sum(rz) / len(rz)) if rz else float("nan")
    me = (sum(ev) / len(ev)) if ev else float("nan")
    return mr, me, (mr - me if (rz and ev) else float("nan"))


# --- Placement aggregates ---------------------------------------------------

def placement_aggregates(result: dict, hero_ids=None, ks=(1, 2, 3)):
    """Per-hero placement summary from `per_match_placement`.

    mean_rank = arithmetic mean of the per-match finish ranks (which already use
    the average-rank tie convention). p_first = fraction of matches with rank
    EXACTLY 1.0 (a sole win). p_top[k] = fraction with rank <= k. Returns
    {hero_id: {"n", "mean_rank", "p_first", "p_top": {k: frac}}}."""
    pm = result.get("per_match_placement") or {}
    if hero_ids is None:
        hero_ids = list(pm.keys())
    out = {}
    for hid in hero_ids:
        ranks = pm.get(hid) or []
        n = len(ranks)
        if n == 0:
            out[hid] = {"n": 0, "mean_rank": float("nan"),
                        "p_first": float("nan"),
                        "p_top": {k: float("nan") for k in ks}}
            continue
        out[hid] = {
            "n": n,
            "mean_rank": sum(ranks) / n,
            "p_first": sum(1 for r in ranks if r == 1.0) / n,
            "p_top": {k: sum(1 for r in ranks if r <= k) / n for k in ks},
        }
    return out


# --- CSV builders -----------------------------------------------------------

def _csv_scalar(v):
    """Render one value for CSV so floats round-trip exactly via float()."""
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, float):
        if v != v:
            return ""                            # NaN → empty
        if v in (float("inf"), float("-inf")):
            return repr(v)
        return repr(v)                           # full precision
    if v is None:
        return ""
    return str(v)


def records_to_csv(records, fieldnames=None) -> str:
    """List-of-dicts → CSV text (LF line endings, full-precision floats)."""
    if fieldnames is None:
        fieldnames = list(records[0].keys()) if records else []
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(fieldnames)
    for r in records:
        w.writerow([_csv_scalar(r.get(k)) for k in fieldnames])
    return buf.getvalue()


def comparison_matrix_records(group, baseline: str, series: str, metric: str, *,
                              alpha=0.05, use_scipy=None):
    """Long-form records for the hero × probe matrix of ONE comparison set.
    `group` is a value from `group_results` ({'heroes', 'probes'}). One row per
    (probe, non-baseline hero). Holm is applied across the WHOLE matrix. Each
    row carries the dup-preserving raw `field`, the selected-metric `value`, and
    full cell stats for completeness."""
    field_key, _ = MATRIX_METRICS[metric]
    heroes = group["heroes"]
    hero_rows = [h for h in heroes if h != baseline]

    rows, raw_ps = [], []
    for probe, bucket in group["probes"].items():
        winner = bucket["winner"]
        field_raw = "|".join(field_list(winner))
        for hid in hero_rows:
            cell = matrix_cell(winner, hid, baseline, series, use_scipy=use_scipy)
            if cell is None:
                continue
            rows.append({
                "probe": probe,
                "field": field_raw,
                "baseline": baseline,
                "hero": hid,
                "series": series,
                "metric": metric,
                "value": cell.get(field_key),
                "mean_chips": cell.get("mean_chips"),
                "mean_bb_per_100": cell.get("mean_bb_per_100"),
                "t": cell.get("t"),
                "p_value": cell.get("p_value"),
            })
            raw_ps.append(cell.get("p_value"))
    clean = [p if isinstance(p, (int, float)) and p == p else 1.0 for p in raw_ps]
    for row, hp in zip(rows, holm_adjust(clean)):
        row["holm_p"] = hp
        row["significant"] = isinstance(hp, (int, float)) and hp < alpha
    return rows


MATRIX_CSV_COLUMNS = [
    "probe", "field", "baseline", "hero", "series", "metric", "value",
    "mean_chips", "mean_bb_per_100", "t", "p_value", "holm_p", "significant",
]


def matrix_csv(group, baseline: str, series: str, metric: str, *,
               alpha=0.05, use_scipy=None) -> str:
    return records_to_csv(
        comparison_matrix_records(group, baseline, series, metric,
                                  alpha=alpha, use_scipy=use_scipy),
        MATRIX_CSV_COLUMNS)


def pairwise_records(result: dict, series: str, baseline=None, *,
                     alpha=0.05, use_scipy=None):
    """pairwise_table rows flattened with dup-preserving `field` + `probe`
    context columns, ready for CSV."""
    field_raw = "|".join(field_list(result))
    probe = probe_label(result)
    out = []
    for row in pairwise_table(result, series, baseline,
                              alpha=alpha, use_scipy=use_scipy):
        rec = {"probe": probe, "field": field_raw, "series": series}
        rec.update({k: row.get(k) for k in PAIRWISE_COLUMNS})
        rec["significant"] = row.get("significant")
        out.append(rec)
    return out


PAIRWISE_CSV_COLUMNS = (["probe", "field", "series"] + PAIRWISE_COLUMNS
                        + ["significant"])


def pairwise_csv(result: dict, series: str, baseline=None, *,
                 alpha=0.05, use_scipy=None) -> str:
    return records_to_csv(
        pairwise_records(result, series, baseline,
                         alpha=alpha, use_scipy=use_scipy),
        PAIRWISE_CSV_COLUMNS)


def raw_per_match_records(result: dict):
    """One record per match index (1-based) with each hero's realized & EV (and
    placement when present) per-match values. Columns: match, <hero>_realized,
    <hero>_ev, <hero>_placement."""
    rz = result.get("per_match_deltas") or {}
    ev = result.get("per_match_ev_deltas") or {}
    pl = result.get("per_match_placement") or {}
    hero_ids = hero_ids_of(result)
    n = max([len(rz.get(h) or []) for h in hero_ids] + [0])
    recs = []
    for i in range(n):
        rec = {"match": i + 1}
        for h in hero_ids:
            rec[f"{h}_realized"] = (rz.get(h) or [None] * n)[i] if i < len(rz.get(h) or []) else None
            rec[f"{h}_ev"] = (ev.get(h) or [None] * n)[i] if i < len(ev.get(h) or []) else None
            if pl:
                rec[f"{h}_placement"] = (pl.get(h) or [None] * n)[i] if i < len(pl.get(h) or []) else None
        recs.append(rec)
    return recs


def raw_per_match_columns(result: dict):
    rz = result.get("per_match_deltas") or {}
    pl = result.get("per_match_placement") or {}
    cols = ["match"]
    for h in hero_ids_of(result):
        cols += [f"{h}_realized", f"{h}_ev"]
        if pl:
            cols += [f"{h}_placement"]
    return cols


def raw_per_match_csv(result: dict) -> str:
    return records_to_csv(raw_per_match_records(result),
                          raw_per_match_columns(result))
