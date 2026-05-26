#!/usr/bin/env python3
"""
backtest.py — EV backtesting harness for Fullhouse bots.

Runs many *seeded* matches in-process (no Docker / no subprocess) against your
real engine.game.PokerEngine, then reports chip-delta EV with variance and 95%
confidence intervals.

Why in-process? The sandbox runner spawns a subprocess per bot and does JSON
over stdin/stdout per action. Fine for one tournament match, far too slow for
the hundreds/thousands of matches you want for a stable EV estimate. This calls
decide() directly. You lose the OS-level sandbox, but for *your own* bots that's
fine — and it faithfully reproduces the two behaviours that matter for EV:
crashes auto-fold, and decide() calls over the time budget auto-fold.

Two modes:

  eval   measure ONE hero bot's EV against a field of opponents
         python3 backtest.py eval bots/mybot/bot.py bots/shark/bot.py \
                 bots/aggressor/bot.py bots/mathematician/bot.py \
                 --matches 200 --hands 400

  ab     PAIRED comparison of two hero variants on identical seeds + opponents.
         Answers "did the exploit layer beat SafeTAG?" with a paired t-stat.
         python3 backtest.py ab --a bots/exploit/bot.py --b bots/safetag/bot.py \
                 --field bots/shark/bot.py bots/aggressor/bot.py \
                         bots/mathematician/bot.py bots/ref_bot_2/bot.py \
                 --matches 200 --hands 400

Notes
-----
* Per match the deck is seeded by (seed * 1000003 + hand_num) exactly like
  sandbox/match.py, so a seed reproduces a match. Because the deck depends only
  on (seed, hand_num) and NOT on seating, the ab mode can place A and B into the
  identical seat against the identical cards — that pairing kills most of the
  variance, so you need far fewer matches to detect a real EV edge.
* Opponent bots are reloaded fresh per match by default (--reload), because the
  real tournament starts a fresh process per match and bots like an
  opponent-modeller keep state in module globals. Pass --no-reload if every bot
  is stateless and you want the speed.
"""

import argparse
import importlib.util
import json
import math
import os
import random
import statistics
import sys
import time
from pathlib import Path


# ---------------------------------------------------------------------------
# Locate the repo so `import engine.game` works regardless of where this lives
# ---------------------------------------------------------------------------

def _find_repo_root(start=None):
    cur = Path(start or os.getcwd()).resolve()
    for d in [cur, *cur.parents]:
        if (d / "engine" / "game.py").is_file():
            return d
    return None


# Honor --repo before importing the engine (the import runs at module load,
# so we peek at argv rather than waiting for argparse).
if "--repo" in sys.argv:
    _i = sys.argv.index("--repo")
    if _i + 1 < len(sys.argv):
        sys.path.insert(0, os.path.abspath(sys.argv[_i + 1]))

_ROOT = _find_repo_root(__file__) or _find_repo_root(os.getcwd())
if _ROOT:
    sys.path.insert(0, str(_ROOT))

try:
    from engine.game import PokerEngine, STARTING_STACK, BIG_BLIND
except Exception as e:  # pragma: no cover
    sys.stderr.write(
        "Could not import engine.game. Run this from inside the fullhouse repo, "
        "or pass --repo /path/to/fullhouse-engine.\n"
        f"Underlying error: {e}\n"
    )
    raise


# ---------------------------------------------------------------------------
# Bot loading
# ---------------------------------------------------------------------------

_load_seq = 0


def load_decide(path):
    """Load bot.py at `path` and return its decide callable. Fresh module each
    call (unique module name) so module-global state doesn't leak across loads."""
    global _load_seq
    _load_seq += 1
    name = f"_bt_bot_{_load_seq}"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    if not hasattr(mod, "decide") or not callable(mod.decide):
        raise AttributeError(f"{path} has no callable decide()")
    return mod.decide


def make_ids(paths):
    """Stable, unique bot_id per path (mirrors sandbox/match.py's logic:
    use the file stem, fall back to the parent dir name on collision)."""
    ids, used = [], set()
    for i, p in enumerate(paths):
        pp = Path(p)
        bid = pp.stem
        if bid in ("bot",) or bid in used:
            bid = pp.parent.name or f"bot_{i}"
        n, base = 1, bid
        while bid in used:
            n += 1
            bid = f"{base}_{n}"
        used.add(bid)
        ids.append(bid)
    return ids


def build_decide_map(id_to_path):
    """id -> path  ==>  id -> decide callable (all freshly loaded)."""
    return {bid: load_decide(p) for bid, p in id_to_path.items()}


# ---------------------------------------------------------------------------
# In-process match runner (faithful to sandbox/match.py outcomes)
# ---------------------------------------------------------------------------

def run_match_inproc(decide_map, n_hands, seed, budget=2.0, fold_on_timeout=True):
    """decide_map: ordered dict {bot_id: decide_callable}. Insertion order is the
    seating order. Returns chip deltas, hands played, per-bot timing and errors."""
    bot_ids = list(decide_map.keys())
    stacks = {b: STARTING_STACK for b in bot_ids}
    match_log = []
    dealer = 0
    hands_played = 0
    timing = {b: {"max": 0.0, "slow": 0} for b in bot_ids}
    errors = {b: 0 for b in bot_ids}

    for hand_num in range(n_hands):
        alive = [b for b in bot_ids if stacks[b] > 0]
        if len(alive) < 2:
            break

        hseed = (seed * 1000003 + hand_num) if seed is not None else None
        eng = PokerEngine(
            hand_id=f"bt_h{hand_num}",
            bot_ids=alive,
            dealer_seat=dealer % len(alive),
            starting_stacks={b: stacks[b] for b in alive},
            seed=hseed,
        )

        state = eng.start_hand()
        if state.get("type") == "action_request":
            state["match_action_log"] = match_log[-200:]

        steps = 0
        while state.get("type") == "action_request":
            seat = state["seat_to_act"]
            bid = alive[seat]

            t0 = time.perf_counter()
            try:
                action = decide_map[bid](state)
            except Exception:
                action = {"action": "fold"}
                errors[bid] += 1
            dt = time.perf_counter() - t0

            if dt > timing[bid]["max"]:
                timing[bid]["max"] = dt
            if dt > budget:
                timing[bid]["slow"] += 1
                if fold_on_timeout:
                    action = {"action": "fold"}

            if not isinstance(action, dict) or "action" not in action:
                action = {"action": "fold"}

            match_log.append({
                "hand_num": hand_num, "seat": seat, "bot_id": bid,
                "action": action.get("action"), "amount": action.get("amount"),
            })

            state = eng.apply_action(seat, action)
            if state.get("type") == "action_request":
                state["match_action_log"] = match_log[-200:]

            steps += 1
            if steps > 1000:  # same guard as the real runner
                break

        for b, s in state.get("final_stacks", {}).items():
            stacks[b] = s
        dealer += 1
        hands_played += 1

    return {
        "chip_delta": {b: stacks[b] - STARTING_STACK for b in bot_ids},
        "hands": hands_played,
        "timing": timing,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def summarize(deltas, hands_per_match):
    """deltas: list of per-match chip deltas for one bot."""
    n = len(deltas)
    total = sum(deltas)
    mean = total / n if n else 0.0
    sd = statistics.stdev(deltas) if n > 1 else 0.0
    se = sd / math.sqrt(n) if n else 0.0
    ci = 1.96 * se
    total_hands = hands_per_match * n if hands_per_match else 0
    # big blinds won per 100 hands
    bb100 = (total / BIG_BLIND) / total_hands * 100 if total_hands else 0.0
    wins = sum(1 for d in deltas if d > 0)
    return {
        "matches": n,
        "total_delta": total,
        "mean_delta": mean,
        "stdev": sd,
        "stderr": se,
        "ci95": ci,
        "ci_low": mean - ci,
        "ci_high": mean + ci,
        "bb_per_100": bb100,
        "win_rate": wins / n if n else 0.0,
    }


def verdict(ci_low, ci_high):
    if ci_low > 0:
        return "edge looks real (95% CI > 0)"
    if ci_high < 0:
        return "losing (95% CI < 0)"
    return "indistinguishable from break-even — run more matches"


# ---------------------------------------------------------------------------
# eval mode
# ---------------------------------------------------------------------------

def cmd_eval(args):
    paths = args.bots
    if len(paths) < 2:
        sys.exit("eval needs at least 2 bots (hero first, then opponents).")
    ids = make_ids(paths)
    hero_id = ids[0]
    id_to_path = dict(zip(ids, paths))

    per_bot = {bid: [] for bid in ids}
    t0 = time.time()
    cached = None if args.reload else build_decide_map(id_to_path)

    for i in range(args.matches):
        seed = args.seed_base + i
        # Reproducible seat order per match. Shuffling the dict insertion order
        # shuffles seating, since the runner seats in key order.
        order = ids[:]
        if args.rotate_seats:
            k = i % len(order)
            order = order[k:] + order[:k]
        else:
            random.Random(seed * 7919 + 1).shuffle(order)

        dm = (build_decide_map({b: id_to_path[b] for b in order})
              if args.reload else {b: cached[b] for b in order})

        res = run_match_inproc(dm, args.hands, seed,
                               budget=args.budget,
                               fold_on_timeout=not args.no_fold_on_timeout)
        for bid, d in res["chip_delta"].items():
            per_bot[bid].append(d)

        if args.verbose and (i + 1) % max(1, args.matches // 10) == 0:
            sys.stderr.write(f"  ...{i+1}/{args.matches} matches\n")

    elapsed = time.time() - t0
    stats = {bid: summarize(per_bot[bid], args.hands) for bid in ids}

    if args.json:
        print(json.dumps({"hero": hero_id, "elapsed_s": round(elapsed, 2),
                          "stats": stats}, indent=2))
        return

    print(f"\nEVAL — {args.matches} matches x {args.hands} hands "
          f"({len(ids)}-handed) in {elapsed:.1f}s\n")
    hdr = f"{'bot':<22}{'mean Δ/match':>14}{'95% CI':>20}{'bb/100':>10}{'win%':>8}"
    print(hdr)
    print("-" * len(hdr))
    for bid in sorted(ids, key=lambda b: -stats[b]["mean_delta"]):
        s = stats[bid]
        ci = f"[{s['ci_low']:+,.0f}, {s['ci_high']:+,.0f}]"
        star = "  <-- hero" if bid == hero_id else ""
        print(f"{bid:<22}{s['mean_delta']:>+14,.0f}{ci:>20}"
              f"{s['bb_per_100']:>+10.2f}{s['win_rate']*100:>7.0f}%{star}")

    s = stats[hero_id]
    print(f"\nHero ({hero_id}): {verdict(s['ci_low'], s['ci_high'])}")
    _print_timing_warnings(per_bot.keys(), args, id_to_path)


# ---------------------------------------------------------------------------
# ab mode (paired)
# ---------------------------------------------------------------------------

def _hero_label(prefix, path):
    pp = Path(path)
    name = pp.stem
    if name in ("bot",):
        name = pp.parent.name or name
    return prefix + name


def cmd_ab(args):
    field_ids = make_ids(args.field)
    field_map = dict(zip(field_ids, args.field))
    a_id = _hero_label("A:", args.a)
    b_id = _hero_label("B:", args.b)
    if a_id == b_id:
        a_id, b_id = a_id + "#1", b_id + "#2"

    n_seats = len(field_ids) + 1
    a_deltas, b_deltas, paired_diff = [], [], []
    t0 = time.time()

    for i in range(args.matches):
        seed = args.seed_base + i
        hero_idx = i % n_seats  # same seat for A and B at this seed

        def seated(hero_id, hero_path):
            order = field_ids[:]
            order.insert(hero_idx, hero_id)
            paths = {bid: (hero_path if bid == hero_id else field_map[bid])
                     for bid in order}
            # Fresh loads every match for both variants -> identical, independent
            # opponents, valid pairing.
            dm = build_decide_map({b: paths[b] for b in order})
            return run_match_inproc(dm, args.hands, seed,
                                    budget=args.budget,
                                    fold_on_timeout=not args.no_fold_on_timeout)

        ra = seated(a_id, args.a)
        rb = seated(b_id, args.b)
        da = ra["chip_delta"][a_id]
        db = rb["chip_delta"][b_id]
        a_deltas.append(da)
        b_deltas.append(db)
        paired_diff.append(da - db)

        if args.verbose and (i + 1) % max(1, args.matches // 10) == 0:
            sys.stderr.write(f"  ...{i+1}/{args.matches} paired matches\n")

    elapsed = time.time() - t0
    sa = summarize(a_deltas, args.hands)
    sb = summarize(b_deltas, args.hands)
    sd = summarize(paired_diff, args.hands)
    # paired t-stat on the per-match difference
    t_stat = (sd["mean_delta"] / sd["stderr"]) if sd["stderr"] else 0.0

    if args.json:
        print(json.dumps({"a": a_id, "b": b_id, "elapsed_s": round(elapsed, 2),
                          "A": sa, "B": sb, "A_minus_B": sd, "t_stat": t_stat},
                         indent=2))
        return

    print(f"\nA/B (paired) — {args.matches} matches x {args.hands} hands, "
          f"{n_seats}-handed, in {elapsed:.1f}s\n")
    for label, s in ((a_id, sa), (b_id, sb)):
        print(f"  {label:<18} mean Δ/match {s['mean_delta']:>+12,.0f}   "
              f"bb/100 {s['bb_per_100']:>+7.2f}")
    print("-" * 56)
    print(f"  A - B            mean Δ/match {sd['mean_delta']:>+12,.0f}   "
          f"95% CI [{sd['ci_low']:+,.0f}, {sd['ci_high']:+,.0f}]")
    print(f"  paired t-stat    {t_stat:>+.2f}  "
          f"({'significant' if abs(t_stat) >= 1.96 else 'not significant'} at ~95%)")

    if sd["ci_low"] > 0:
        print(f"\n  => {a_id} beats {b_id}. The change is +EV. Ship it.")
    elif sd["ci_high"] < 0:
        print(f"\n  => {a_id} is WORSE than {b_id}. Roll back (classifier too "
              f"noisy or exploit too aggressive).")
    else:
        print(f"\n  => No detectable difference yet. Either it's neutral, or you "
              f"need more matches to resolve it.")


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------

def _print_timing_warnings(ids, args, id_to_path):
    # Re-run is unnecessary; timing is per-match and already discarded. We just
    # flag the budget so users remember fold-on-timeout is active.
    if not args.no_fold_on_timeout:
        sys.stderr.write(
            f"\n(note: calls over {args.budget:.1f}s auto-folded, matching "
            f"tournament rules. Use --budget to change, --no-fold-on-timeout "
            f"to disable.)\n"
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="EV backtester for Fullhouse bots")
    ap.add_argument("--repo", help="path to fullhouse-engine repo root")
    sub = ap.add_subparsers(dest="mode", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--matches", type=int, default=100)
    common.add_argument("--hands", type=int, default=400, help="hands per match")
    common.add_argument("--seed-base", type=int, default=0)
    common.add_argument("--budget", type=float, default=2.0,
                        help="per-action time budget in seconds")
    common.add_argument("--no-fold-on-timeout", action="store_true")
    common.add_argument("--json", action="store_true")
    common.add_argument("--verbose", action="store_true")

    pe = sub.add_parser("eval", parents=[common],
                        help="one hero vs a field")
    pe.add_argument("bots", nargs="+", help="hero first, then opponents")
    pe.add_argument("--reload", dest="reload", action="store_true", default=True,
                    help="reload bot modules each match (default)")
    pe.add_argument("--no-reload", dest="reload", action="store_false")
    pe.add_argument("--rotate-seats", action="store_true",
                    help="cycle seat order deterministically instead of random")

    pa = sub.add_parser("ab", parents=[common],
                        help="paired A vs B on identical seeds/opponents")
    pa.add_argument("--a", required=True, help="variant A bot.py (e.g. exploit)")
    pa.add_argument("--b", required=True, help="variant B bot.py (e.g. safetag)")
    pa.add_argument("--field", nargs="+", required=True, help="opponent bots")

    args = ap.parse_args()

    if args.repo:
        sys.path.insert(0, os.path.abspath(args.repo))

    if args.mode == "eval":
        cmd_eval(args)
    elif args.mode == "ab":
        cmd_ab(args)


if __name__ == "__main__":
    main()
