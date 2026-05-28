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

EV-adjustment (this version)
----------------------------
Alongside realized `chip_delta`, every match now also returns
`ev_chip_delta`: the all-in-adjusted chip delta. For any pot where chips locked
up before the board was complete (an all-in run-out), we remove the variance of
the remaining community cards by replacing the realized award with the
*equity-weighted expected* award over the to-come cards (exact enumeration when
<=2 cards are to come, hand_id-seeded Monte Carlo otherwise). This is a pure
post-hoc *measurement* read off the completed hand — it never touches the engine
and cannot change match outcomes. See BLUEPRINT.md Section 3 / Layer 2.

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
import hashlib
import importlib.util
import itertools
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

# eval7 is the same library the engine uses; importing it here lets us compute
# equities post-hoc. If it is somehow unavailable, EV-adjustment degrades
# gracefully to realized (the harness still runs).
try:
    import eval7
    _HAS_EVAL7 = True
except Exception:  # pragma: no cover
    _HAS_EVAL7 = False


# ---------------------------------------------------------------------------
# Bot loading
# ---------------------------------------------------------------------------

_load_seq = 0

# Identity-probe dedup: tracks (path, BOT_NAME) pairs already logged in this
# process so the probe in load_decide prints one stderr line per unique pair
# instead of once per match (which would be thousands of lines per job in
# parallel mode). The diagnostic value is in the SET of pairs seen, not in
# the count — a healthy run shows one line per deployed bot with all
# BOT_NAMEs distinct from each other; an unhealthy run shows the same
# BOT_NAME under multiple paths (or vice versa), surfacing a bot-deployment
# bug at load time rather than after the run finishes.
_LOAD_PROBE_SEEN: set = set()


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
    # Identity probe — see _LOAD_PROBE_SEEN. Cheap permanent insurance against
    # the deployment-level failure mode where multiple "variant" paths point
    # to the same file on disk: if that ever happens again, this surfaces it
    # the first time those paths get loaded rather than 30 minutes into a run.
    bot_name = getattr(mod, "BOT_NAME", "<no BOT_NAME>")
    probe_key = (path, bot_name)
    if probe_key not in _LOAD_PROBE_SEEN:
        _LOAD_PROBE_SEEN.add(probe_key)
        print(f"[load_decide] BOT_NAME={bot_name!r}  path={path}",
              file=sys.stderr, flush=True)
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
# EV adjustment (post-hoc measurement; reads the completed hand only)
# ---------------------------------------------------------------------------

# Full 52-card deck as the engine serializes it (ranks upper, suits lower),
# e.g. "As", "Td", "9h". Card strings in revealed_cards / community_cards use
# this exact format, so set membership / eval7.Card() round-trip cleanly.
_RANKS = "23456789TJQKA"
_SUITS = "shdc"
_FULL_DECK = [r + s for r in _RANKS for s in _SUITS]


def _stable_seed(key):
    """Deterministic 32-bit seed from any string key. Uses md5 (not Python's
    salted hash()) so Monte-Carlo equities reproduce across processes/runs.
    The key folds in the match seed + hand_id, so reruns of the same job
    reproduce identically while distinct matches sample independently."""
    h = hashlib.md5(str(key).encode("utf-8")).hexdigest()
    return int(h, 16) % (2 ** 32)


def _pot_equities(contender_holes, pots, lock_board, to_come, mc_key,
                  mc_samples=4000):
    """Per-pot win-share equity over the to-come community cards.

    contender_holes : {bot_id: [card_str, card_str]} for non-folded contenders.
    pots            : list of {"amount", "eligible"} from _reconstruct_side_pots.
    lock_board      : card_str already on the board at lock-up (len 5 - to_come).
    to_come         : community cards still to be dealt (0, 1, 2, or 5).
    mc_key          : string seed key for the Monte-Carlo RNG.

    Returns a list parallel to `pots`; entry i is {bot_id: equity in [0,1]} over
    that pot's eligible subset, summing to ~1 within the subset. This is the
    correct quantity for side pots: P(b is best *among that pot's eligible set*),
    NOT a renormalization of each contender's global win-share (which is biased
    once stacks diverge and real side pots form). Pots are nested, so a single
    runout enumeration scores every contender once and feeds all pots. Exact
    enumeration for to_come <= 2 (also covers the 0-card self-check case),
    seeded Monte Carlo otherwise.
    """
    hole = {b: [eval7.Card(c) for c in cs] for b, cs in contender_holes.items()}
    board = [eval7.Card(c) for c in lock_board]

    dead = set(lock_board)
    for cs in contender_holes.values():
        dead.update(cs)
    rem = [eval7.Card(c) for c in _FULL_DECK if c not in dead]

    pot_eq = [{b: 0.0 for b in p["eligible"]} for p in pots]

    def tally(extra):
        fb = board + list(extra)
        score = {b: eval7.evaluate(hole[b] + fb) for b in contender_holes}
        for pi, p in enumerate(pots):
            elig = p["eligible"]
            best = max(score[b] for b in elig)
            winners = [b for b in elig if score[b] == best]
            share = 1.0 / len(winners)
            for w in winners:
                pot_eq[pi][w] += share

    if to_come <= 2:
        # combinations(rem, 0) yields a single empty tuple -> handles to_come==0.
        n = 0
        for combo in itertools.combinations(rem, to_come):
            tally(combo)
            n += 1
    else:
        rng = random.Random(_stable_seed(mc_key))
        n = mc_samples
        for _ in range(n):
            tally(rng.sample(rem, to_come))

    if n == 0:
        return pot_eq
    return [{b: e / n for b, e in d.items()} for d in pot_eq]


def _reconstruct_side_pots(total_invested, contenders):
    """Replay the engine's level algorithm over reconstructed total_invested.

    Mirrors PokerEngine._compute_side_pots:
      levels       = sorted distinct positive total_invested (ALL dealt players)
      contributors = all dealt players with total_invested >= level  (sets amount)
      eligible     = contenders (revealed/non-folded) with total_invested >= level
      amount       = (level - prev_level) * #contributors
    Rounding residual (dead money from folders) absorbed into the last pot.
    Returns a list of {"amount": int, "eligible": [bot_id, ...]} or [] if none.
    """
    contenders = set(contenders)
    levels = sorted({ti for ti in total_invested.values() if ti > 0})
    total_pot = sum(ti for ti in total_invested.values() if ti > 0)

    pots, prev = [], 0
    for lvl in levels:
        per = lvl - prev
        contributors = [b for b, ti in total_invested.items() if ti >= lvl]
        amount = per * len(contributors)
        eligible = [b for b in contributors if b in contenders]
        if amount > 0 and eligible:
            pots.append({"amount": amount, "eligible": eligible})
        prev = lvl

    if not pots:
        return []

    booked = sum(p["amount"] for p in pots)
    if booked != total_pot:
        pots[-1]["amount"] += total_pot - booked
    return pots


def compute_hand_ev(state, pre_hand_stacks, mc_samples=4000, debug=False,
                    match_seed=None):
    """EV-adjusted per-hand chip delta for the hand described by `state` (a
    hand_complete dict). `pre_hand_stacks` is {bot_id: stack} before the hand
    for the participants. Returns {bot_id: ev_delta} over participants.

    Falls back to realized whenever there is no luck to remove (non-showdown,
    complete board, run-it-out behind a live checker) or when reconstruction is
    not possible. Pure measurement: reads `state` only, never the engine.
    """
    final_stacks = state.get("final_stacks", {})
    participants = list(final_stacks.keys())
    realized = {b: final_stacks[b] - pre_hand_stacks[b] for b in participants}

    # No showdown (uncontested / guard) -> nothing to adjust.
    if not state.get("showdown"):
        return dict(realized)

    events = state.get("events", [])

    # Lock-up street = the street of the LAST action event, NOT the last
    # street_start. _advance_street deals the next street and emits its
    # street_start *before* it checks for a live actor and delegates to
    # _run_it_out, so the last street_start sits one street too deep on every
    # all-in run-out (turn all-ins would look complete, flop all-ins would keep
    # turn variance, preflop jams would keep the flop swing — exactly the
    # variance we most want gone). The locking action is emitted while
    # self.street is still the betting street, so key the board off that.
    action_events = [e for e in events if e.get("type") == "action"]
    if not action_events:
        return dict(realized)
    lock_street = action_events[-1].get("street")
    ss = [e for e in events
          if e.get("type") == "street_start" and e.get("street") == lock_street]
    if not ss:
        return dict(realized)
    lock_board = list(ss[-1].get("community_cards", []))
    to_come = 5 - len(lock_board)

    # Contender set = revealed (non-folded) hole cards; need exactly 2 each.
    revealed = state.get("revealed_cards", {}) or {}
    contenders = [b for b in participants
                  if b in revealed and revealed.get(b) and len(revealed[b]) == 2]
    if not contenders:
        return dict(realized)

    # total_invested[bot] = pre-hand stack - stack in the LAST action event's
    # snapshot (it includes every player, post-action, pre-award).
    last_stacks = action_events[-1].get("stacks", {})
    total_invested = {
        b: pre_hand_stacks[b] - last_stacks.get(b, pre_hand_stacks[b])
        for b in participants
    }

    pots = _reconstruct_side_pots(total_invested, contenders)
    if not pots:
        return dict(realized)

    if not _HAS_EVAL7:
        return dict(realized)

    contender_holes = {b: list(revealed[b]) for b in contenders}
    mc_key = f"{match_seed}:{state.get('hand_id')}"
    pot_eq = _pot_equities(contender_holes, pots, lock_board, to_come,
                           mc_key, mc_samples=mc_samples)

    # Each pot is split by its eligible subset's own win-shares (already sum to
    # ~1 within the subset) — no global renormalization.
    ev_award = {b: 0.0 for b in contenders}
    for pi, pot in enumerate(pots):
        amount = pot["amount"]
        for b in pot["eligible"]:
            ev_award[b] += amount * pot_eq[pi][b]

    # to_come == 0: complete board, no luck. ev == realized. Self-check that the
    # equity split reproduces the engine's awards — validates the side-pot
    # reconstruction AND the per-pot allocation. (Per-player tolerance absorbs
    # the engine's remainder-to-first-winner integer split vs. our fair float
    # split on chopped pots.)
    if to_come == 0:
        won = {}
        for w in state.get("winners", []):
            won[w["bot_id"]] = won.get(w["bot_id"], 0) + w.get("amount", 0)
        tol = float(len(contenders)) + 1.0
        for b in contenders:
            diff = abs(ev_award.get(b, 0.0) - won.get(b, 0))
            if diff > tol:
                msg = (f"[EV self-check] {state.get('hand_id')}: bot {b} "
                       f"reconstructed award {ev_award.get(b, 0.0):.1f} != "
                       f"realized award {won.get(b, 0)} (|diff|={diff:.1f})\n")
                if debug:
                    raise AssertionError(msg.strip())
                sys.stderr.write(msg)
        return dict(realized)

    ev = {}
    for b in participants:
        if b in ev_award:
            ev[b] = ev_award[b] - total_invested[b]
        else:
            # Folded players: ev == realized == -total_invested.
            ev[b] = realized[b]
    return ev


# ---------------------------------------------------------------------------
# In-process match runner (faithful to sandbox/match.py outcomes)
# ---------------------------------------------------------------------------

def run_match_inproc(decide_map, n_hands, seed, budget=2.0, fold_on_timeout=True,
                     compute_ev=True, ev_mc_samples=4000, ev_debug=False):
    """decide_map: ordered dict {bot_id: decide_callable}. Insertion order is the
    seating order. Returns chip deltas, EV-adjusted chip deltas, hands played,
    per-bot timing and errors.

    EV-adjustment is a post-hoc measurement computed from each completed hand;
    it does not affect play. If `compute_ev` is False, `ev_chip_delta` mirrors
    `chip_delta`."""
    bot_ids = list(decide_map.keys())
    stacks = {b: STARTING_STACK for b in bot_ids}
    match_log = []
    dealer = 0
    hands_played = 0
    timing = {b: {"max": 0.0, "slow": 0} for b in bot_ids}
    errors = {b: 0 for b in bot_ids}
    ev_total = {b: 0.0 for b in bot_ids}

    do_ev = compute_ev and _HAS_EVAL7

    for hand_num in range(n_hands):
        alive = [b for b in bot_ids if stacks[b] > 0]
        if len(alive) < 2:
            break

        # Pre-hand stacks for the participants (before this hand's awards).
        pre_hand = {b: stacks[b] for b in alive}

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

        # ---- EV accounting (post-hoc, reads the completed hand only) --------
        if do_ev:
            if state.get("type") == "hand_complete":
                try:
                    hand_ev = compute_hand_ev(state, pre_hand,
                                              mc_samples=ev_mc_samples,
                                              debug=ev_debug,
                                              match_seed=seed)
                except Exception:
                    # Never let a measurement bug abort the match; fall back to
                    # realized for this hand.
                    hand_ev = {b: stacks[b] - pre_hand[b] for b in alive}
            else:
                # 1000-step guard tripped (no hand_complete): realized fallback.
                hand_ev = {b: stacks.get(b, pre_hand[b]) - pre_hand[b]
                           for b in alive}
            for b, v in hand_ev.items():
                ev_total[b] += v

        dealer += 1
        hands_played += 1

    chip_delta = {b: stacks[b] - STARTING_STACK for b in bot_ids}
    if do_ev:
        ev_chip_delta = {b: round(ev_total[b], 4) for b in bot_ids}
    else:
        # EV disabled / eval7 missing: mirror realized so the key always exists.
        ev_chip_delta = dict(chip_delta)

    return {
        "chip_delta": chip_delta,
        "ev_chip_delta": ev_chip_delta,
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
    per_bot_ev = {bid: [] for bid in ids}
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
                               fold_on_timeout=not args.no_fold_on_timeout,
                               compute_ev=not args.no_ev,
                               ev_mc_samples=args.ev_mc,
                               ev_debug=args.ev_debug)
        for bid, d in res["chip_delta"].items():
            per_bot[bid].append(d)
        for bid, d in res.get("ev_chip_delta", {}).items():
            per_bot_ev[bid].append(d)

        if args.verbose and (i + 1) % max(1, args.matches // 10) == 0:
            sys.stderr.write(f"  ...{i+1}/{args.matches} matches\n")

    elapsed = time.time() - t0
    stats = {bid: summarize(per_bot[bid], args.hands) for bid in ids}
    ev_stats = {bid: summarize(per_bot_ev[bid], args.hands) for bid in ids}

    if args.json:
        print(json.dumps({"hero": hero_id, "elapsed_s": round(elapsed, 2),
                          "stats": stats, "ev_stats": ev_stats}, indent=2))
        return

    print(f"\nEVAL — {args.matches} matches x {args.hands} hands "
          f"({len(ids)}-handed) in {elapsed:.1f}s\n")
    hdr = (f"{'bot':<22}{'mean Δ/match':>14}{'95% CI':>20}"
           f"{'bb/100':>10}{'ev bb/100':>11}{'win%':>8}")
    print(hdr)
    print("-" * len(hdr))
    for bid in sorted(ids, key=lambda b: -stats[b]["mean_delta"]):
        s = stats[bid]
        se = ev_stats[bid]
        ci = f"[{s['ci_low']:+,.0f}, {s['ci_high']:+,.0f}]"
        star = "  <-- hero" if bid == hero_id else ""
        print(f"{bid:<22}{s['mean_delta']:>+14,.0f}{ci:>20}"
              f"{s['bb_per_100']:>+10.2f}{se['bb_per_100']:>+11.2f}"
              f"{s['win_rate']*100:>7.0f}%{star}")

    s = stats[hero_id]
    se = ev_stats[hero_id]
    print(f"\nHero ({hero_id}): {verdict(s['ci_low'], s['ci_high'])}")
    print(f"  realized mean Δ {s['mean_delta']:+,.0f}  (sd {s['stdev']:,.0f})   "
          f"EV-adj mean Δ {se['mean_delta']:+,.0f}  (sd {se['stdev']:,.0f})")
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
    a_ev_deltas, b_ev_deltas, paired_ev_diff = [], [], []
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
                                    fold_on_timeout=not args.no_fold_on_timeout,
                                    compute_ev=not args.no_ev,
                                    ev_mc_samples=args.ev_mc,
                                    ev_debug=args.ev_debug)

        ra = seated(a_id, args.a)
        rb = seated(b_id, args.b)
        da = ra["chip_delta"][a_id]
        db = rb["chip_delta"][b_id]
        da_e = ra.get("ev_chip_delta", {}).get(a_id, da)
        db_e = rb.get("ev_chip_delta", {}).get(b_id, db)
        a_deltas.append(da)
        b_deltas.append(db)
        paired_diff.append(da - db)
        a_ev_deltas.append(da_e)
        b_ev_deltas.append(db_e)
        paired_ev_diff.append(da_e - db_e)

        if args.verbose and (i + 1) % max(1, args.matches // 10) == 0:
            sys.stderr.write(f"  ...{i+1}/{args.matches} paired matches\n")

    elapsed = time.time() - t0
    sa = summarize(a_deltas, args.hands)
    sb = summarize(b_deltas, args.hands)
    sd = summarize(paired_diff, args.hands)
    sa_e = summarize(a_ev_deltas, args.hands)
    sb_e = summarize(b_ev_deltas, args.hands)
    sd_e = summarize(paired_ev_diff, args.hands)
    # paired t-stat on the per-match difference (realized and EV-adjusted)
    t_stat = (sd["mean_delta"] / sd["stderr"]) if sd["stderr"] else 0.0
    t_stat_ev = (sd_e["mean_delta"] / sd_e["stderr"]) if sd_e["stderr"] else 0.0

    if args.json:
        print(json.dumps({
            "a": a_id, "b": b_id, "elapsed_s": round(elapsed, 2),
            "A": sa, "B": sb, "A_minus_B": sd, "t_stat": t_stat,
            "A_ev": sa_e, "B_ev": sb_e, "A_minus_B_ev": sd_e,
            "t_stat_ev": t_stat_ev,
        }, indent=2))
        return

    print(f"\nA/B (paired) — {args.matches} matches x {args.hands} hands, "
          f"{n_seats}-handed, in {elapsed:.1f}s\n")
    print("  realized:")
    for label, s in ((a_id, sa), (b_id, sb)):
        print(f"    {label:<18} mean Δ/match {s['mean_delta']:>+12,.0f}   "
              f"bb/100 {s['bb_per_100']:>+7.2f}")
    print("  " + "-" * 56)
    print(f"    A - B            mean Δ/match {sd['mean_delta']:>+12,.0f}   "
          f"95% CI [{sd['ci_low']:+,.0f}, {sd['ci_high']:+,.0f}]")
    print(f"    paired t-stat    {t_stat:>+.2f}  "
          f"({'significant' if abs(t_stat) >= 1.96 else 'not significant'} at ~95%)")

    print("\n  EV-adjusted (variance-reduced):")
    for label, s in ((a_id, sa_e), (b_id, sb_e)):
        print(f"    {label:<18} mean Δ/match {s['mean_delta']:>+12,.1f}   "
              f"bb/100 {s['bb_per_100']:>+7.2f}")
    print("  " + "-" * 56)
    print(f"    A - B (EV)       mean Δ/match {sd_e['mean_delta']:>+12,.1f}   "
          f"95% CI [{sd_e['ci_low']:+,.1f}, {sd_e['ci_high']:+,.1f}]")
    print(f"    paired t-stat    {t_stat_ev:>+.2f}  "
          f"({'significant' if abs(t_stat_ev) >= 1.96 else 'not significant'} at ~95%)")

    sig = sd_e if sd_e["stderr"] else sd
    if sig["ci_low"] > 0:
        print(f"\n  => {a_id} beats {b_id}. The change is +EV. Ship it.")
    elif sig["ci_high"] < 0:
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
    common.add_argument("--no-ev", action="store_true",
                        help="disable EV-adjustment (ev_chip_delta mirrors realized)")
    common.add_argument("--ev-mc", type=int, default=4000,
                        help="Monte-Carlo samples for >2-card run-outs (preflop all-ins)")
    common.add_argument("--ev-debug", action="store_true",
                        help="assert (instead of warn) if the river self-check fails")

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
