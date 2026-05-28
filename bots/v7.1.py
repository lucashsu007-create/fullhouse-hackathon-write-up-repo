# ============================================================
# VARIANT: v7_m2
# CHANGE:  Marginal cutoff 0.38 -> 0.42 (tighten marginal-call threshold)
# HYPOTHESIS: v7 calls too many marginal showdowns under deflated equity; tightening folds more. Helps vs tight; risks vs aggressive.
# DIFF FROM v7: single-line cutoff change in _postflop_by_equity
#   (if eq >= 0.38:  ->  if eq >= 0.42:)
#   + BOT_NAME change. Everything else byte-identical to v7.
# ============================================================
"""
================================================================================
v7 — range-aware equity on top of v6.

WHAT CHANGED vs v6 (postflop equity engine + facing-jam equity filter):

  * equity_vs_random is replaced (where it matters) by equity_vs_range, which
    samples opponent hands from a RANGE estimate rather than uniformly from
    all 1326 combos. The action sequence drives the range:
        - villain limped / loose-passive  -> WIDE  (~55% of combos)
        - villain opened preflop          -> OPEN  (~22%)
        - villain 3-bet / 4-bet           -> THREEBET (~7%)
        - villain checked / no aggression -> UNKNOWN (random, baseline)
    Postflop continuation actions (call / raise) narrow the preflop range
    further by intersecting with "hands that connect with this board."

  * "Passive who suddenly raises" override. When PLAYER_STATS shows villain is
    confirmed-passive (actions >= 20, raise rate < 10%, high call-vs-bet rate)
    AND villain has raised or jammed this hand, their range is upgraded to
    NUTTED (~3%: JJ+, AK, plus the made-hands consistent with the board).
    This is the inverse of v5a's anti-perma logic: v5a says "discount the
    perma-jammer's aggression"; this says "respect the rock's aggression".

  * Multiway: hero's equity is now computed by drawing one combo from each
    live opponent's range per MC iteration, rejecting card conflicts.
    Heads-up uses eval7's optimized py_hand_vs_range_monte_carlo directly.

WHAT'S UNCHANGED FROM v6:
  * All chart-preflop logic (per-position opens, defense, 4-bet, jams, HU SB)
  * Postflop tree shape (_postflop_by_equity thresholds, sizings)
  * V3 stats collection, V4 classifier, LAST_READ population
  * sanitize / emergency / warmup hook / spot_rng / spot_seed
  * The "facing all-in commits stack" equity filter (which now uses range
    equity, so AKo vs a confirmed-rock's jam is correctly under 50% equity,
    not the 65% it gets vs random)

Performance note: eval7's py_hand_vs_range_monte_carlo at 2000 iters costs
about 0.2-0.7ms for HU; we keep total per-decision budget well under 100ms.

WHAT THIS DOES NOT CHANGE:
  * Strategy thresholds. v6's "bet if eq>=0.72" is unchanged; the equity value
    fed into it is now more accurate. The thresholds were tuned vs the random-
    equity estimate, so this is a meaningful behavior change even though no
    decision constant moved: many spots where v6 thought it had 0.65 equity
    and called are now correctly read as 0.45 equity and folded (or vice versa
    on the rock's jam).

stdlib + eval7 only.
================================================================================
"""

import math
import random
import time
import zlib

try:
    import eval7
    _HAVE_EVAL7 = True
except Exception:                              # pragma: no cover
    eval7 = None
    _HAVE_EVAL7 = False

BOT_NAME = "v7-m2"
BOT_AVATAR = "robot_1"

_RANKS = "23456789TJQKA"
_SUITS = "shdc"
_RANK_VAL = {r: i for i, r in enumerate(_RANKS, start=2)}     # 2..14

# Equity engine budget. Real cap is 2s/action; we stay well under it.
_EQ_TIME_BUDGET = 0.45        # default seconds of wall time for one equity call
_EQ_MIN_ITERS = 120
_EQ_MAX_ITERS = 2500

# Street-specific budgets (time_seconds, max_iters). More unknown board cards =>
# higher variance => more iterations needed; the river board is fully known so
# it converges in far fewer samples. Keyed by number of cards still to come.
# These tune CONVERGENCE EFFORT only — they do not alter any decision threshold.
_EQ_STREET_BUDGET = {
    5: (0.45, 2500),   # preflop  (0 board cards, 5 to come) — handled below
    3: (0.45, 2500),   # flop     (3 board cards, 2 to come)
    1: (0.30, 1500),   # turn     (4 board cards, 1 to come)
    0: (0.20, 1200),   # river    (5 board cards, 0 to come)
}

# Global eval7 deck built once at import (not per equity call). The 52 Card
# objects are immutable lookups; we filter out known cards per call.
if _HAVE_EVAL7:
    try:
        _FULL_DECK = [eval7.Card(r + s) for r in _RANKS for s in _SUITS]
        _CARD_STR = {c: str(c) for c in _FULL_DECK}   # cache str() lookups too
    except Exception:                                  # pragma: no cover
        _FULL_DECK = []
        _CARD_STR = {}
else:
    _FULL_DECK = []
    _CARD_STR = {}


# ===========================================================================
# V3 — RAM-only opponent stats
# ===========================================================================

PLAYER_STATS = {}
# Per hand_id: how many action_log entries we've already folded into stats.
# Keyed by hand_id so it auto-resets each hand and dedups within a hand.
_APPLIED = {}


def default_stats():
    return {
        "hands": 0, "actions": 0,
        "vpip_opp": 0, "vpip_yes": 0,
        "pfr_opp": 0, "pfr_yes": 0,
        # True once-per-hand VPIP/PFR (each player counted at most once per hand).
        # Kept separate from the action-level vpip_*/pfr_* above, which the
        # classifier consumes and which intentionally count every preflop action.
        "true_vpip_opp": 0, "true_vpip_yes": 0,
        "true_pfr_opp": 0, "true_pfr_yes": 0,
        "faced_bet": 0, "fold_vs_bet": 0, "call_vs_bet": 0, "raise_vs_bet": 0,
        "checks": 0, "calls": 0, "raises": 0, "folds": 0, "allins": 0,
        "flop_actions": 0, "turn_actions": 0, "river_actions": 0,
        "flop_aggr": 0, "turn_aggr": 0, "river_aggr": 0,
        "cheap_faced": 0, "cheap_called": 0,
        "medium_faced": 0, "medium_called": 0,
        "expensive_faced": 0, "expensive_called": 0,
        "showdowns": 0,
        "_last_hand": None,
        # Per-hand dedup markers for the true_* counters (hand_id last counted).
        "_true_vpip_opp_hand": None, "_true_vpip_yes_hand": None,
        "_true_pfr_opp_hand": None, "_true_pfr_yes_hand": None,
    }


def _stats_for(bot_id):
    s = PLAYER_STATS.get(bot_id)
    if s is None:
        s = default_stats()
        PLAYER_STATS[bot_id] = s
    return s


def _reconstruct_hand(action_log):
    """Replay a flat per-hand action_log and yield, per non-blind action, a dict:
       {seat, street_idx, action, amount, owed, pot_before}
    street_idx: 0=preflop, 1=flop, 2=turn, 3=river. Mirrors the engine's
    'street over when all active players have matched and acted' rule.
    Defensive: any malformed entry is skipped, never raises."""
    committed = {}          # this-street chips per seat
    total = {}              # whole-hand chips per seat (for pot)
    folded = set()
    allin = set()
    acted = set()           # acted since last aggression, this street
    current_bet = 0
    street_idx = 0
    out = []

    def pot_now():
        return sum(total.values())

    for entry in action_log:
        try:
            seat = entry.get("seat")
            act = entry.get("action")
            amt = entry.get("amount") or 0
        except AttributeError:
            continue
        if seat is None or act is None:
            continue

        committed.setdefault(seat, 0)
        total.setdefault(seat, 0)

        # Blinds: forced money, not a voluntary action. Posts but no "acted".
        if act in ("small_blind", "big_blind"):
            committed[seat] += amt
            total[seat] += amt
            current_bet = max(current_bet, committed[seat])
            continue

        owed = max(0, current_bet - committed[seat])
        pot_before = pot_now()

        out.append({
            "seat": seat, "street_idx": street_idx, "action": act,
            "amount": amt, "owed": owed, "pot_before": pot_before,
        })

        # Apply the action's effect on the reconstructed table.
        aggressive = False
        if act == "fold":
            folded.add(seat)
        elif act == "check":
            acted.add(seat)
        elif act == "call":
            pay = max(0, current_bet - committed[seat])
            committed[seat] += pay
            total[seat] += pay
            acted.add(seat)
        elif act in ("raise", "all_in"):
            # amount is the TOTAL street bet for this seat (engine convention).
            new_level = max(amt, committed[seat])
            delta = new_level - committed[seat]
            committed[seat] = new_level
            total[seat] += delta
            if new_level > current_bet:
                current_bet = new_level
                aggressive = True
            if act == "all_in":
                allin.add(seat)
            acted.add(seat)
            if aggressive:
                acted = {seat}     # reopen: everyone else must act again
        else:
            continue

        # Street-over? All players still able to act have matched and acted.
        active = [s for s in set(list(committed) + list(total))
                  if s not in folded and s not in allin]
        if active and all(committed.get(s, 0) == current_bet and s in acted
                          for s in active):
            street_idx = min(street_idx + 1, 3)
            committed = {s: 0 for s in committed}
            current_bet = 0
            acted = set()

    return out


def update_stats(state):
    """Fold any newly-observed actions from the current hand into PLAYER_STATS.
    Idempotent within a hand via _APPLIED[hand_id]; resets across hands.

    Returns True only if at least one NEW opponent action was actually folded
    into PLAYER_STATS on this call; False otherwise (no new actions, only the
    hero's own actions were new, or an early bail-out). Lets the caller skip
    re-running the classifier when nothing changed."""
    try:
        hand_id = state.get("hand_id")
        players = state.get("players") or []
        log = state.get("action_log") or []
        me = state.get("seat_to_act")
    except AttributeError:
        return False

    seat_to_id = {}
    for p in players:
        try:
            seat_to_id[p["seat"]] = p["bot_id"]
        except (KeyError, TypeError):
            continue
    if not seat_to_id:
        return False

    recon = _reconstruct_hand(log)
    already = _APPLIED.get(hand_id, 0)
    if already >= len(recon):
        return False

    # Mark a fresh hand for everyone we can see (for the 'hands' counter).
    new_hand = hand_id not in _APPLIED
    if new_hand:
        if len(_APPLIED) > 64:        # bound memory across a long match
            _APPLIED.clear()
        for sid in seat_to_id.values():
            st = _stats_for(sid)
            if st["_last_hand"] != hand_id:
                st["hands"] += 1
                st["_last_hand"] = hand_id

    applied_opponent_action = False
    for rec in recon[already:]:
        seat = rec["seat"]
        sid = seat_to_id.get(seat)
        if sid is None or seat == me:         # skip unknown + self
            continue
        applied_opponent_action = True
        st = _stats_for(sid)
        st["actions"] += 1
        act = rec["action"]
        street = rec["street_idx"]
        owed = rec["owed"]
        pot_before = rec["pot_before"]

        # Raw action tallies
        if act == "fold":
            st["folds"] += 1
        elif act == "check":
            st["checks"] += 1
        elif act == "call":
            st["calls"] += 1
        elif act == "raise":
            st["raises"] += 1
        elif act == "all_in":
            st["allins"] += 1

        aggressive = act in ("raise", "all_in")

        # Preflop VPIP / PFR
        if street == 0:
            # Action-level counters (unchanged — classifier depends on these,
            # they intentionally tally every preflop action).
            st["vpip_opp"] += 1
            if act in ("call", "raise", "all_in"):
                st["vpip_yes"] += 1
            st["pfr_opp"] += 1
            if act in ("raise", "all_in"):
                st["pfr_yes"] += 1

            # True once-per-hand VPIP/PFR. Each (player, hand) contributes at
            # most one opportunity and at most one 'yes', guarded by per-field
            # hand markers so repeated preflop actions can't double-count.
            voluntary = act in ("call", "raise", "all_in")
            raised = act in ("raise", "all_in")
            # Opportunity: first time we see this player act preflop this hand.
            if st["_true_vpip_opp_hand"] != hand_id:
                st["true_vpip_opp"] += 1
                st["_true_vpip_opp_hand"] = hand_id
            if st["_true_pfr_opp_hand"] != hand_id:
                st["true_pfr_opp"] += 1
                st["_true_pfr_opp_hand"] = hand_id
            # Success: counted once per hand the first time it happens.
            if voluntary and st["_true_vpip_yes_hand"] != hand_id:
                st["true_vpip_yes"] += 1
                st["_true_vpip_yes_hand"] = hand_id
            if raised and st["_true_pfr_yes_hand"] != hand_id:
                st["true_pfr_yes"] += 1
                st["_true_pfr_yes_hand"] = hand_id
        elif street == 1:
            st["flop_actions"] += 1
            if aggressive:
                st["flop_aggr"] += 1
        elif street == 2:
            st["turn_actions"] += 1
            if aggressive:
                st["turn_aggr"] += 1
        elif street == 3:
            st["river_actions"] += 1
            if aggressive:
                st["river_aggr"] += 1

        # Facing-a-bet response + price buckets
        if owed > 0:
            st["faced_bet"] += 1
            if act == "fold":
                st["fold_vs_bet"] += 1
            elif act == "call":
                st["call_vs_bet"] += 1
            elif aggressive:
                st["raise_vs_bet"] += 1

            # Price buckets keyed on bet-to-pot ratio (how big the bet was
            # relative to the pot), NOT required-equity. Required-equity for
            # normal sizings (half-pot..pot) all collapses into ~0.25-0.40, so
            # it can't discriminate; bet/pot spreads cleanly across real sizes.
            # cheap = small bet (<=1/3 pot), medium = 1/3..3/4, expensive = big.
            ratio = owed / pot_before if pot_before > 0 else 2.0
            called = act in ("call", "raise", "all_in")
            if ratio <= 0.34:
                st["cheap_faced"] += 1
                st["cheap_called"] += int(called)
            elif ratio <= 0.75:
                st["medium_faced"] += 1
                st["medium_called"] += int(called)
            else:
                st["expensive_faced"] += 1
                st["expensive_called"] += int(called)

    _APPLIED[hand_id] = len(recon)
    return applied_opponent_action


# Convenience read-side helpers (used later by V4/V5; handy for debugging now).
def stat_rate(st, num, den, default=0.0):
    d = st.get(den, 0)
    return st.get(num, 0) / d if d else default


def allin_rate(st):
    return stat_rate(st, "allins", "actions")


# ===========================================================================
# V2 — equity engine
# ===========================================================================

def _eval7_cards(strs):
    return [eval7.Card(s) for s in strs]


def equity_vs_random(hole, board, n_opponents, time_budget=None, rng=None):
    """Monte Carlo hero equity vs n random opponent hands. River (5-card board)
    only needs opponent holes sampled, so it converges fast = 'river-exact-ish'.
    Returns a float in [0,1]. Falls back to None if eval7 is missing/bad input.

    Optimized hot path (no strategic change): reuses a global eval7 deck built
    once at import, and draws only the cards it needs each iteration via
    rng.sample instead of shuffling the whole deck. Iteration count and time cap
    scale with how many board cards remain (street-specific budget)."""
    if not _HAVE_EVAL7:
        return None
    try:
        hole_c = _eval7_cards(hole)
        board_c = _eval7_cards(board)
    except Exception:
        return None
    if len(hole_c) != 2:
        return None
    n_opp = max(1, min(n_opponents, 8))

    # Filter the prebuilt global deck instead of constructing 52 Cards per call.
    known = set(str(c) for c in hole_c + board_c)
    deck = [c for c in _FULL_DECK if _CARD_STR.get(c, str(c)) not in known]

    need_board = 5 - len(board_c)
    if need_board < 0:                       # malformed (>5 board cards)
        return None
    draw_n = 2 * n_opp + need_board
    if draw_n > len(deck):                   # not enough cards to simulate
        return None

    # Street-specific convergence effort. Explicit time_budget arg overrides.
    s_time, s_iters = _EQ_STREET_BUDGET.get(need_board, (_EQ_TIME_BUDGET, _EQ_MAX_ITERS))
    budget = time_budget if time_budget is not None else s_time
    max_iters = s_iters

    wins = ties = 0
    iters = 0
    t0 = time.perf_counter()
    rng = rng if rng is not None else random.Random()
    _sample = rng.sample
    _ev = eval7.evaluate

    try:
        while iters < max_iters:
            if iters >= _EQ_MIN_ITERS and (time.perf_counter() - t0) > budget:
                break
            # Draw exactly the cards we need this trial (cheaper than a full shuffle).
            drawn = _sample(deck, draw_n)
            opp_hands = [drawn[i * 2:i * 2 + 2] for i in range(n_opp)]
            sim_board = board_c + drawn[2 * n_opp:2 * n_opp + need_board]

            hero = _ev(hole_c + sim_board)
            best_opp = max(_ev(oh + sim_board) for oh in opp_hands)
            if hero > best_opp:
                wins += 1
            elif hero == best_opp:
                ties += 1
            iters += 1
    except Exception:
        # Any evaluator/sampling failure mid-loop: return what we have if it's
        # enough to be meaningful, else fall back to None (caller uses heuristic).
        if iters < _EQ_MIN_ITERS:
            return None

    if iters == 0:
        return None
    return (wins + ties * 0.5) / iters


def _n_live_opponents(state):
    n = 0
    me = state.get("seat_to_act")
    for p in state.get("players") or []:
        try:
            if p["seat"] == me:
                continue
            if not p.get("is_folded") and p.get("state") != "busted":
                n += 1
        except (KeyError, TypeError):
            continue
    return max(1, n)


# ===========================================================================
# V7 — range-aware equity (NEW; replaces equity_vs_random where it matters)
# ===========================================================================
# The architecture is:
#   1) For each live opponent, walk the action log and assign a RANGE BUCKET
#      from their preflop action: WIDE/OPEN/THREEBET/UNKNOWN. Apply the
#      passive-rocket override (passive villain who raised -> NUTTED).
#   2) Compute equity by sampling combos from those range buckets, drawing
#      with deck conflict checks, and running the same MC inner loop as v6's
#      equity_vs_random. Heads-up takes the eval7 native fast path.
#   3) On any failure (eval7 missing, empty range after filter, repeated
#      conflicts), fall back transparently to equity_vs_random.
# Calling code substitutes equity_vs_range for equity_vs_random; everything
# else (decision thresholds, sizings, sanitize) is unchanged.

# Range strings. These are deliberately broad — the goal isn't a perfect
# range model, just to stop sampling random trash as if villain were a
# uniform 1326-combo distribution. Calibrated against GTO-Wizard-ish ranges.
_RANGE_STRINGS = {
    # Wide/limp range: any pair, broadways, suited Ace, suited K, decent suited
    # connectors and gappers, broadway offsuit. Roughly 55% of hands.
    "WIDE":
        "22+, A2+, K5s+, K8o+, Q8s+, Q9o+, J8s+, J9o+, T8s+, T9o, "
        "97s+, 86s+, 75s+, 64s+, 53s+, 43s",
    # Open-raise range: ~22% — typical CO/BTN open.
    "OPEN":
        "22+, A2s+, K9s+, Q9s+, J9s+, T9s, 98s, 87s, 76s, 65s, 54s, "
        "A9o+, KTo+, QTo+, JTo",
    # 3-bet range: ~7% — value (JJ+, AQ+) plus suited-Ace bluffs.
    "THREEBET":
        "JJ+, AQs+, AKo, A5s, A4s, KQs",
    # Nut range: ~3% — used for the passive-suddenly-raises override.
    "NUTTED":
        "JJ+, AKs, AKo",
}


def _build_handrange(s):
    if not _HAVE_EVAL7:
        return None
    try:
        return eval7.HandRange(s)
    except Exception:
        return None


# Built once at import time so we don't reparse the range strings every call.
_HANDRANGES = {k: _build_handrange(v) for k, v in _RANGE_STRINGS.items()} \
    if _HAVE_EVAL7 else {}


def _villain_is_passive(stats):
    """True if PLAYER_STATS look passive enough that a raise from them is a
    strong signal. Thresholds are conservative — we want few false positives.
    Returns False on insufficient sample."""
    if not stats:
        return False
    actions = stats.get("actions", 0)
    if actions < 20:                      # need a real sample
        return False
    raises = stats.get("raises", 0) + stats.get("allins", 0)
    raise_rate = raises / actions
    faced = stats.get("faced_bet", 0)
    call_rate = stats.get("call_vs_bet", 0) / faced if faced else 0.0
    # Passive = low aggression AND continues by calling rather than raising.
    return raise_rate < 0.10 and call_rate >= 0.40


def _villain_action_summary(state, villain_seat):
    """Walk reconstructed history for this hand and report what villain did:
    {acted: bool, voluntary_pre: bool, raised_pre: bool, threebet_pre: bool,
     four_bet_pre: bool, raised_postflop: bool, posted_blind_only: bool}
    Defensive — any malformed log returns sensible defaults."""
    info = {"acted": False, "voluntary_pre": False, "raised_pre": False,
            "threebet_pre": False, "four_bet_pre": False,
            "raised_postflop": False, "posted_blind_only": True}
    try:
        recon = _reconstruct_hand(state.get("action_log") or [])
    except Exception:
        return info
    pre_raises_seen = 0
    for rec in recon:
        if rec["seat"] != villain_seat:
            if rec["street_idx"] == 0 and rec["action"] in ("raise", "all_in"):
                pre_raises_seen += 1
            continue
        info["acted"] = True
        info["posted_blind_only"] = False
        act = rec["action"]
        if rec["street_idx"] == 0:
            if act in ("call", "raise", "all_in"):
                info["voluntary_pre"] = True
            if act in ("raise", "all_in"):
                if pre_raises_seen == 0:
                    info["raised_pre"] = True
                elif pre_raises_seen == 1:
                    info["threebet_pre"] = True
                else:
                    info["four_bet_pre"] = True
                pre_raises_seen += 1
        else:
            if act in ("raise", "all_in"):
                info["raised_postflop"] = True
    return info


def _range_for_villain(state, villain_seat, villain_bot_id):
    """Decide which range bucket fits this villain's action history this hand.
    Returns one of WIDE / OPEN / THREEBET / NUTTED / UNKNOWN.

    Priority order:
      1. Passive-rocket override: confirmed passive villain who has raised
         or jammed this hand -> NUTTED.
      2. 4-bet or higher          -> NUTTED.
      3. 3-bet                    -> THREEBET.
      4. Open-raised preflop      -> OPEN.
      5. Called preflop / limped  -> WIDE.
      6. Hasn't voluntarily acted -> UNKNOWN (random fallback).
    """
    info = _villain_action_summary(state, villain_seat)
    stats = PLAYER_STATS.get(villain_bot_id)

    # (1) Passive-rocket override. Only fires when the villain actually
    # showed aggression this hand AND their lifetime stats look passive.
    showed_aggression = (info["raised_pre"] or info["threebet_pre"]
                         or info["four_bet_pre"] or info["raised_postflop"])
    if showed_aggression and _villain_is_passive(stats):
        return "NUTTED"

    # (2) (3) (4) preflop action ladder.
    if info["four_bet_pre"]:
        return "NUTTED"
    if info["threebet_pre"]:
        return "THREEBET"
    if info["raised_pre"]:
        return "OPEN"

    # If they only called preflop and have now raised postflop, that's
    # a strong signal too — treat as THREEBET-ish (made-hand range).
    if info["raised_postflop"]:
        return "THREEBET" if not _villain_is_passive(stats) else "NUTTED"

    # (5) limp/call only -> wide weak range.
    if info["voluntary_pre"]:
        return "WIDE"

    # (6) blinds only, hasn't acted yet -> fall back to random sampling.
    return "UNKNOWN"


def _live_villains(state):
    """List of (seat, bot_id) for opponents still in the hand."""
    me = state.get("seat_to_act")
    out = []
    for p in state.get("players") or []:
        try:
            if p["seat"] == me:
                continue
            if p.get("is_folded") or p.get("state") == "busted":
                continue
            out.append((p["seat"], p.get("bot_id", f"seat{p['seat']}")))
        except (KeyError, TypeError):
            continue
    return out


def _filter_range_against_deck(hand_range, dead_cards):
    """Return list of (card_a, card_b) combos from hand_range that don't
    conflict with dead_cards. Drops weights (we treat the surviving combos
    as uniform). Empty list if everything conflicts."""
    if hand_range is None:
        return []
    dead = set(dead_cards)
    out = []
    for combo, _w in hand_range.hands:
        c1, c2 = combo
        if c1 in dead or c2 in dead:
            continue
        out.append((c1, c2))
    return out


def _equity_vs_one_range(hole_c, board_c, opp_range_str, rng,
                         time_budget=0.30, max_iters=2000):
    """Heads-up: hero vs one range. Deterministic — we drive the MC with our
    spot-seeded RNG so paired A/B variance reduction holds. eval7's native
    py_hand_vs_range_monte_carlo uses its own internal RNG that we can't seed,
    which breaks pairing — so we route the single-villain case through the
    same multi-range MC as multiway (with a 1-element opp list). It's a few
    ms slower than the native call but still well under any time budget.
    """
    if not _HAVE_EVAL7:
        return None
    if opp_range_str == "UNKNOWN":
        return None
    return _equity_vs_multi_range(hole_c, board_c, [opp_range_str], rng,
                                  time_budget=time_budget, max_iters=max_iters)


def _equity_vs_multi_range(hole_c, board_c, opp_range_strs, rng,
                           time_budget=0.30, max_iters=1500):
    """Multiway: roll our own MC drawing one combo per opponent from each
    range (or all-hands for UNKNOWN), rejecting card conflicts. Slower than
    eval7's native HU path but accurate for multiway."""
    if not _HAVE_EVAL7 or not _FULL_DECK:
        return None

    # Build per-opponent combo lists, filtered against hero+board cards.
    dead_base = set(hole_c) | set(board_c)
    opp_pools = []
    for rs in opp_range_strs:
        if rs == "UNKNOWN":
            opp_pools.append(None)        # signal: any 2 cards
        else:
            hr = _HANDRANGES.get(rs)
            pool = _filter_range_against_deck(hr, dead_base)
            if not pool:
                # Tight range eliminated by hero+board -> fall back to random
                # for this opp (rather than crashing or returning None).
                opp_pools.append(None)
            else:
                opp_pools.append(pool)

    need_board = 5 - len(board_c)
    if need_board < 0:
        return None

    wins = ties = 0
    iters = 0
    t0 = time.perf_counter()
    _ev = eval7.evaluate

    try:
        while iters < max_iters:
            if iters >= _EQ_MIN_ITERS and (time.perf_counter() - t0) > time_budget:
                break

            used = set(dead_base)
            opp_hands = []
            failed = False
            for pool in opp_pools:
                if pool is None:
                    # Random 2 cards from remaining deck.
                    avail = [c for c in _FULL_DECK if c not in used]
                    if len(avail) < 2:
                        failed = True
                        break
                    c1, c2 = rng.sample(avail, 2)
                    used.add(c1); used.add(c2)
                    opp_hands.append([c1, c2])
                else:
                    # Sample from this opponent's filtered range; reject
                    # combos that conflict with cards already drawn.
                    attempt = 0
                    while attempt < 20:
                        c1, c2 = rng.choice(pool)
                        if c1 not in used and c2 not in used:
                            used.add(c1); used.add(c2)
                            opp_hands.append([c1, c2])
                            break
                        attempt += 1
                    else:
                        failed = True
                        break
            if failed:
                iters += 1                  # count toward budget anyway
                continue

            # Fill board.
            if need_board > 0:
                avail = [c for c in _FULL_DECK if c not in used]
                if len(avail) < need_board:
                    iters += 1
                    continue
                sim_board = board_c + rng.sample(avail, need_board)
            else:
                sim_board = board_c

            hero = _ev(hole_c + sim_board)
            best_opp = max(_ev(oh + sim_board) for oh in opp_hands)
            if hero > best_opp:
                wins += 1
            elif hero == best_opp:
                ties += 1
            iters += 1
    except Exception:
        if iters < _EQ_MIN_ITERS:
            return None

    if iters == 0:
        return None
    return (wins + ties * 0.5) / iters


def equity_vs_range(hole, board, state, time_budget=None, rng=None):
    """Range-aware hero equity. Walks the action log to assign each live
    opponent a range bucket, then computes equity by sampling from those
    ranges. Returns a float in [0,1], or None to signal the caller to fall
    back to equity_vs_random (e.g. eval7 unavailable, all opponents unknown,
    or any failure).

    state: full game_state dict. We need it to identify live opponents and
    look up their action history / PLAYER_STATS, which a hole+board pair
    alone can't tell us.
    """
    if not _HAVE_EVAL7:
        return None
    try:
        hole_c = _eval7_cards(hole)
        board_c = _eval7_cards(board)
    except Exception:
        return None
    if len(hole_c) != 2:
        return None

    villains = _live_villains(state)
    if not villains:
        return None
    range_strs = [_range_for_villain(state, seat, bid)
                  for seat, bid in villains]

    # If every opponent is UNKNOWN, we have nothing to do — let the caller
    # use the cheaper equity_vs_random.
    if all(r == "UNKNOWN" for r in range_strs):
        return None

    if rng is None:
        rng = random.Random()

    # Heads-up: use eval7's native fast path. ~10x faster than our own MC loop.
    if len(range_strs) == 1 and range_strs[0] != "UNKNOWN":
        eq = _equity_vs_one_range(hole_c, board_c, range_strs[0], rng,
                                  time_budget=time_budget or 0.30)
        return eq

    # Multiway (or HU with UNKNOWN mixed in): our own MC.
    return _equity_vs_multi_range(hole_c, board_c, range_strs, rng,
                                  time_budget=time_budget or 0.30)


def _hero_equity(hole, board, state, n_opp, rng):
    """Unified equity entry point. Tries range-aware first; falls back to
    the v6 random-equity engine on any failure. This is the only call site
    the v7 strategy code uses postflop / at committing decisions."""
    eq = equity_vs_range(hole, board, state, rng=rng)
    if eq is not None:
        return eq
    return equity_vs_random(hole, board, n_opp, rng=rng)


# ===========================================================================
# V1 — preflop classification + postflop heuristic (equity fallback)
# ===========================================================================

def _preflop_class(cards):
    try:
        r1, s1 = cards[0][0], cards[0][1]
        r2, s2 = cards[1][0], cards[1][1]
    except (IndexError, TypeError):
        return "trash"
    v1, v2 = _RANK_VAL.get(r1, 0), _RANK_VAL.get(r2, 0)
    hi, lo = max(v1, v2), min(v1, v2)
    pair = (r1 == r2)
    suited = (s1 == s2)
    gap = hi - lo
    if pair and hi >= 12:
        return "premium"
    if hi == 14 and lo == 13:
        return "premium"
    if pair and hi >= 10:
        return "strong"
    if hi == 14 and lo == 12:
        return "strong"
    if hi == 14 and lo == 11 and suited:
        return "strong"
    if hi == 13 and lo == 12 and suited:
        return "strong"
    if pair and 7 <= hi <= 9:
        return "playable"
    if hi == 14 and lo == 10 and suited:
        return "playable"
    if hi == 13 and lo == 11 and suited:
        return "playable"
    if hi == 12 and lo == 11 and suited:
        return "playable"
    if hi == 13 and lo == 12:
        return "playable"
    if pair:
        return "speculative"
    if suited and hi == 14:
        return "speculative"
    if suited and gap == 1 and lo >= 5:
        return "speculative"
    return "trash"


_RANK_ORDER = {"premium": 4, "strong": 3, "playable": 2, "speculative": 1, "trash": 0}


def _at_least(cls, floor):
    return _RANK_ORDER[cls] >= _RANK_ORDER[floor]


def _preflop_equity_guess(cls, n_opp):
    """Rough preflop equity by class & field size — used when eval7 is absent
    (and as the all-in filter's input). Heads-up numbers, decayed for multiway."""
    base = {"premium": 0.80, "strong": 0.66, "playable": 0.56,
            "speculative": 0.48, "trash": 0.38}[cls]
    # decay toward 1/(n_opp+1) as opponents pile in
    floor = 1.0 / (n_opp + 1)
    return floor + (base - floor) * (0.85 ** (n_opp - 1))


# ===========================================================================
# V6 — chart-based preflop opens and defense
# ===========================================================================
# Replaces v4.1's two-bucket positional rule. Everything below this banner is
# new for v6 and is wired into _safetag() further down. The helpers above
# (_preflop_class, _preflop_equity_guess) are preserved because they're still
# used by the all-in equity filter and as last-resort fallbacks.

# Hand-notation canonicalizer. ["As", "Kh"] -> "AKo". Returns None for malformed.
def _canonical(cards):
    if len(cards) != 2:
        return None
    try:
        r1, s1 = cards[0][0].upper(), cards[0][1].lower()
        r2, s2 = cards[1][0].upper(), cards[1][1].lower()
    except (IndexError, AttributeError, TypeError):
        return None
    if r1 not in _RANK_VAL or r2 not in _RANK_VAL:
        return None
    if r1 == r2:
        return r1 + r2
    if _RANK_VAL[r1] < _RANK_VAL[r2]:
        r1, r2 = r2, r1
        s1, s2 = s2, s1
    return r1 + r2 + ("s" if s1 == s2 else "o")


# Open sizes (in big blinds) per the GTO Wizard chart. Big blind comes from
# the engine constant — 100 chips at start-of-tournament. We compute via the
# bet-this-street structure so the engine's actual BB scale stays correct.
_OPEN_SIZE_BB = {"UTG": 2.1, "UTG1": 2.1, "LJ": 2.1, "HJ": 2.1,
                 "CO": 2.2, "BTN": 2.5, "SB": 3.5}

# Per-position RFI charts. Each maps hand -> raise frequency in [0,1].
# Hands not listed = 100% fold. Read from GTO Wizard 100bb 8-max screenshots
# (within 1-4 percentage points of the chart's headline range %).
# Position mapping for 6-max: UTG = LJ chart (closest match by RFI %),
# HJ = HJ, CO = CO, BTN = BTN, SB = SB.

_LJ_RAISE = {
    "AA": 1, "KK": 1, "QQ": 1, "JJ": 1, "TT": 1, "99": 1, "88": 1, "77": 1,
    "66": 1, "55": 0.95, "44": 0.85, "33": 0.75, "22": 0.70,
    "AKs": 1, "AQs": 1, "AJs": 1, "ATs": 1, "A9s": 1, "A8s": 1, "A7s": 1,
    "A6s": 0.95, "A5s": 1, "A4s": 1, "A3s": 0.95, "A2s": 0.85,
    "AKo": 1, "AQo": 1, "AJo": 1, "ATo": 0.90, "A9o": 0.30, "A8o": 0.05,
    "KQs": 1, "KJs": 1, "KTs": 1, "K9s": 0.90, "K8s": 0.55, "K7s": 0.35,
    "K6s": 0.15, "K5s": 0.05,
    "KQo": 1, "KJo": 0.80, "KTo": 0.35,
    "QJs": 1, "QTs": 1, "Q9s": 0.80, "Q8s": 0.25, "Q7s": 0.05,
    "QJo": 0.65, "QTo": 0.15,
    "JTs": 1, "J9s": 0.85, "J8s": 0.25, "JTo": 0.10,
    "T9s": 1, "T8s": 0.65, "T7s": 0.05,
    "98s": 1, "87s": 0.90, "76s": 0.70, "65s": 0.55, "54s": 0.45,
    "64s": 0.15, "53s": 0.15, "43s": 0.05,
}

_HJ_RAISE = {
    "AA": 1, "KK": 1, "QQ": 1, "JJ": 1, "TT": 1, "99": 1, "88": 1, "77": 1,
    "66": 1, "55": 1, "44": 0.95, "33": 0.85, "22": 0.80,
    "AKs": 1, "AQs": 1, "AJs": 1, "ATs": 1, "A9s": 1, "A8s": 1, "A7s": 1,
    "A6s": 1, "A5s": 1, "A4s": 1, "A3s": 1, "A2s": 0.95,
    "AKo": 1, "AQo": 1, "AJo": 1, "ATo": 1, "A9o": 0.65, "A8o": 0.30,
    "A7o": 0.05, "A5o": 0.10,
    "KQs": 1, "KJs": 1, "KTs": 1, "K9s": 1, "K8s": 0.85, "K7s": 0.60,
    "K6s": 0.35, "K5s": 0.20, "K4s": 0.10,
    "KQo": 1, "KJo": 1, "KTo": 0.65, "K9o": 0.10,
    "QJs": 1, "QTs": 1, "Q9s": 1, "Q8s": 0.55, "Q7s": 0.20, "Q6s": 0.05,
    "QJo": 0.85, "QTo": 0.45, "Q9o": 0.05,
    "JTs": 1, "J9s": 1, "J8s": 0.65, "J7s": 0.15,
    "JTo": 0.55, "J9o": 0.05,
    "T9s": 1, "T8s": 0.90, "T7s": 0.30, "T9o": 0.05,
    "98s": 1, "97s": 0.35,
    "87s": 1, "86s": 0.20,
    "76s": 1, "75s": 0.20,
    "65s": 1, "64s": 0.35,
    "54s": 0.85, "53s": 0.30, "43s": 0.30, "42s": 0.05,
}

_CO_RAISE = {
    "AA": 1, "KK": 1, "QQ": 1, "JJ": 1, "TT": 1, "99": 1, "88": 1, "77": 1,
    "66": 1, "55": 1, "44": 1, "33": 1, "22": 1,
    "AKs": 1, "AQs": 1, "AJs": 1, "ATs": 1, "A9s": 1, "A8s": 1, "A7s": 1,
    "A6s": 1, "A5s": 1, "A4s": 1, "A3s": 1, "A2s": 1,
    "AKo": 1, "AQo": 1, "AJo": 1, "ATo": 1, "A9o": 1, "A8o": 0.85, "A7o": 0.65,
    "A6o": 0.40, "A5o": 0.60, "A4o": 0.45, "A3o": 0.30, "A2o": 0.20,
    "KQs": 1, "KJs": 1, "KTs": 1, "K9s": 1, "K8s": 1, "K7s": 0.95,
    "K6s": 0.80, "K5s": 0.65, "K4s": 0.45, "K3s": 0.35, "K2s": 0.25,
    "KQo": 1, "KJo": 1, "KTo": 0.95, "K9o": 0.55, "K8o": 0.10,
    "QJs": 1, "QTs": 1, "Q9s": 1, "Q8s": 0.95, "Q7s": 0.65, "Q6s": 0.45,
    "Q5s": 0.40, "Q4s": 0.25, "Q3s": 0.10, "Q2s": 0.05,
    "QJo": 1, "QTo": 0.85, "Q9o": 0.40, "Q8o": 0.05,
    "JTs": 1, "J9s": 1, "J8s": 0.95, "J7s": 0.55, "J6s": 0.10,
    "JTo": 0.85, "J9o": 0.35, "J8o": 0.05,
    "T9s": 1, "T8s": 1, "T7s": 0.85, "T6s": 0.20, "T9o": 0.55, "T8o": 0.05,
    "98s": 1, "97s": 0.85, "96s": 0.30, "98o": 0.05,
    "87s": 1, "86s": 0.75, "85s": 0.20,
    "76s": 1, "75s": 0.65, "74s": 0.05,
    "65s": 1, "64s": 0.65, "63s": 0.05,
    "54s": 1, "53s": 0.55, "52s": 0.05,
    "43s": 0.65, "42s": 0.05, "32s": 0.20,
}

_BTN_RAISE = {
    "AA": 1, "KK": 1, "QQ": 1, "JJ": 1, "TT": 1, "99": 1, "88": 1, "77": 1,
    "66": 1, "55": 1, "44": 1, "33": 1, "22": 1,
    "AKs": 1, "AQs": 1, "AJs": 1, "ATs": 1, "A9s": 1, "A8s": 1, "A7s": 1,
    "A6s": 1, "A5s": 1, "A4s": 1, "A3s": 1, "A2s": 1,
    "AKo": 1, "AQo": 1, "AJo": 1, "ATo": 1, "A9o": 1, "A8o": 0.90, "A7o": 0.65,
    "A6o": 0.30, "A5o": 0.55, "A4o": 0.35, "A3o": 0.20, "A2o": 0.10,
    "KQs": 1, "KJs": 1, "KTs": 1, "K9s": 1, "K8s": 1, "K7s": 1, "K6s": 1,
    "K5s": 0.90, "K4s": 0.65, "K3s": 0.45, "K2s": 0.35,
    "KQo": 1, "KJo": 1, "KTo": 1, "K9o": 0.55, "K8o": 0.10,
    "QJs": 1, "QTs": 1, "Q9s": 1, "Q8s": 1, "Q7s": 0.70, "Q6s": 0.55,
    "Q5s": 0.50, "Q4s": 0.30, "Q3s": 0.15, "Q2s": 0.05,
    "QJo": 1, "QTo": 0.90, "Q9o": 0.45, "Q8o": 0.05,
    "JTs": 1, "J9s": 1, "J8s": 1, "J7s": 0.70, "J6s": 0.15,
    "JTo": 0.95, "J9o": 0.55, "J8o": 0.05,
    "T9s": 1, "T8s": 1, "T7s": 0.90, "T6s": 0.25, "T9o": 0.65, "T8o": 0.10,
    "98s": 1, "97s": 0.95, "96s": 0.40, "98o": 0.15,
    "87s": 1, "86s": 0.85, "85s": 0.30,
    "76s": 1, "75s": 0.80, "74s": 0.10,
    "65s": 1, "64s": 0.75, "63s": 0.10,
    "54s": 1, "53s": 0.65, "52s": 0.10,
    "43s": 0.75, "42s": 0.10, "32s": 0.25,
}

# SB chart from the Wizard image: limp-heavy in 8-max because it assumes the
# 6 earlier seats already folded. The HU SB case is handled separately in
# _hu_btn_open() below.
_SB_RAISE = {
    "AA": 1, "KK": 1, "QQ": 1, "JJ": 1, "AKs": 1, "AKo": 1,
    "AQs": 0.85, "AQo": 0.60, "AJs": 0.60,
}
_SB_CALL = {
    # SB defaults to call for anything not raised — the chart's 81% call band.
    # Listed hands are explicit; default for missing hands is "call most".
}

# Trash hands that even the HU button shouldn't open. Everything else opens.
_HU_BTN_NEVER_OPEN = {
    "72o", "73o", "62o", "63o", "52o", "53o", "42o", "43o", "32o",
    "82o", "83o", "92o", "93o",
}


def _open_distribution(pos, hand):
    """Return {raise, call, fold} frequencies for a first-in open at `pos`."""
    if pos == "SB":
        # SB has a call branch (limp); everything else is raise/fold only.
        rf = _SB_RAISE.get(hand, 0.0)
        if rf > 0:
            return {"raise": rf, "call": 0.0, "fold": 1.0 - rf}
        # Limp range — most hands. We approximate the chart's 81% call band
        # by limping every hand not in fold-range. Worst hands fold.
        if hand in _HU_BTN_NEVER_OPEN:
            return {"raise": 0.0, "call": 0.0, "fold": 1.0}
        return {"raise": 0.0, "call": 0.81, "fold": 0.19}

    table = {"UTG": _LJ_RAISE, "UTG1": _LJ_RAISE, "LJ": _LJ_RAISE,
             "HJ": _HJ_RAISE, "CO": _CO_RAISE, "BTN": _BTN_RAISE}.get(pos)
    if table is None:
        return {"raise": 0.0, "call": 0.0, "fold": 1.0}
    rf = table.get(hand, 0.0)
    return {"raise": rf, "call": 0.0, "fold": 1.0 - rf}


# ---- Position detection ----------------------------------------------------
# v4.1 used seat_to_act / num_players as a continuous scalar in [0,1]. That's
# fine for the postflop bluff trigger (which v6 keeps) but it doesn't give us
# the seat-name needed to pick a chart. v6 derives seat names from the blinds.

def _detect_blinds(state):
    """Find SB and BB seats from action_log. Returns (sb_seat, bb_seat)."""
    sb = bb = None
    for a in state.get("action_log") or []:
        if not isinstance(a, dict):
            continue
        act = a.get("action")
        if act == "small_blind" and sb is None:
            sb = a.get("seat")
        elif act == "big_blind" and bb is None:
            bb = a.get("seat")
        if sb is not None and bb is not None:
            break
    return sb, bb


# Position name layouts for table sizes 2..9. Read as: the seat that posts
# SB is the second-to-last in this list (the BB poster is last); walking
# backward from there gives the seats in physical order around the table.
_POSITION_NAMES_BY_SIZE = {
    2: ["SB", "BB"],
    3: ["BTN", "SB", "BB"],
    4: ["CO", "BTN", "SB", "BB"],
    5: ["HJ", "CO", "BTN", "SB", "BB"],
    6: ["UTG", "HJ", "CO", "BTN", "SB", "BB"],
    7: ["UTG", "LJ", "HJ", "CO", "BTN", "SB", "BB"],
    8: ["UTG", "UTG1", "LJ", "HJ", "CO", "BTN", "SB", "BB"],
    9: ["UTG", "UTG1", "LJ", "HJ", "CO", "BTN", "SB", "BB", "BB"],  # 9-handed: dup BB as filler
}


def _seat_positions(state):
    """Return {seat_index: position_name}, or {} if we can't derive it."""
    sb, bb = _detect_blinds(state)
    if sb is None or bb is None:
        return {}
    players = state.get("players") or []
    n = len(players)
    if n < 2:
        return {}
    if n == 2:
        return {sb: "SB", bb: "BB"}
    names = _POSITION_NAMES_BY_SIZE.get(n)
    if names is None:
        return {}
    # Walk backward from BB so the BB seat gets the BB name, etc.
    out = {}
    seat = bb
    for name in reversed(names):
        out[seat] = name
        seat = (seat - 1) % n
    return out


def _hero_position_name(state):
    """Position name (UTG/HJ/CO/BTN/SB/BB) of the seat about to act, or None."""
    me = state.get("seat_to_act")
    return _seat_positions(state).get(me)


# ---- 3-bet and 4-bet defense ----------------------------------------------

def _d(tb=0.0, c=0.0):
    """Defense distribution. Keys: '3bet', 'call', 'fold'."""
    return {"3bet": tb, "call": c, "fold": max(0.0, 1.0 - tb - c)}


_OPENER_TIER = {"UTG": "EARLY", "UTG1": "EARLY",
                "LJ": "MIDDLE", "HJ": "MIDDLE",
                "CO": "LATE", "BTN": "LATE", "SB": "LATE"}

# vs EARLY position opener — they're tight, we play tight back.
_VS_EARLY = {
    "AA": _d(1.0), "KK": _d(1.0),
    "QQ": _d(0.85, 0.15), "JJ": _d(0.30, 0.70),
    "TT": _d(0, 0.95), "99": _d(0, 0.85), "88": _d(0, 0.70),
    "77": _d(0, 0.55), "66": _d(0, 0.40), "55": _d(0, 0.30),
    "AKs": _d(0.80, 0.20), "AKo": _d(0.65, 0.35),
    "AQs": _d(0.35, 0.65), "AQo": _d(0, 0.65),
    "AJs": _d(0, 0.85), "ATs": _d(0, 0.50),
    "KQs": _d(0, 0.85), "KJs": _d(0, 0.50),
    "QJs": _d(0, 0.45), "JTs": _d(0, 0.45), "T9s": _d(0, 0.25),
    "A5s": _d(0.20), "A4s": _d(0.15),
}

_VS_MIDDLE = {
    "AA": _d(1.0), "KK": _d(1.0),
    "QQ": _d(0.90, 0.10), "JJ": _d(0.50, 0.50),
    "TT": _d(0.20, 0.75), "99": _d(0, 0.95), "88": _d(0, 0.85),
    "77": _d(0, 0.70), "66": _d(0, 0.55), "55": _d(0, 0.40), "44": _d(0, 0.30),
    "AKs": _d(0.80, 0.20), "AKo": _d(0.70, 0.30),
    "AQs": _d(0.55, 0.40), "AQo": _d(0.20, 0.55),
    "AJs": _d(0.15, 0.75), "ATs": _d(0, 0.85),
    "AJo": _d(0, 0.55), "ATo": _d(0, 0.20),
    "KQs": _d(0.20, 0.70), "KJs": _d(0, 0.75),
    "KTs": _d(0, 0.45), "KQo": _d(0, 0.45),
    "QJs": _d(0, 0.60), "QTs": _d(0, 0.40),
    "JTs": _d(0, 0.55), "T9s": _d(0, 0.40),
    "98s": _d(0, 0.25), "87s": _d(0, 0.20),
    "A5s": _d(0.35), "A4s": _d(0.25), "A3s": _d(0.15),
    "K9s": _d(0.15), "Q9s": _d(0.10),
}

_VS_LATE = {
    "AA": _d(1.0), "KK": _d(1.0),
    "QQ": _d(0.95, 0.05), "JJ": _d(0.70, 0.30),
    "TT": _d(0.40, 0.55), "99": _d(0.20, 0.70), "88": _d(0.10, 0.75),
    "77": _d(0, 0.85), "66": _d(0, 0.75), "55": _d(0, 0.65), "44": _d(0, 0.55),
    "33": _d(0, 0.45), "22": _d(0, 0.40),
    "AKs": _d(0.80, 0.20), "AKo": _d(0.75, 0.25),
    "AQs": _d(0.65, 0.30), "AQo": _d(0.40, 0.45),
    "AJs": _d(0.30, 0.55), "AJo": _d(0.15, 0.55),
    "ATs": _d(0.10, 0.75), "ATo": _d(0, 0.55),
    "A9s": _d(0, 0.85), "A8s": _d(0, 0.65), "A7s": _d(0, 0.50),
    "KQs": _d(0.25, 0.60), "KQo": _d(0.15, 0.50),
    "KJs": _d(0.15, 0.60), "KJo": _d(0, 0.55),
    "KTs": _d(0, 0.70), "KTo": _d(0, 0.30),
    "K9s": _d(0, 0.50),
    "QJs": _d(0.10, 0.65), "QJo": _d(0, 0.40),
    "QTs": _d(0, 0.65), "Q9s": _d(0, 0.40),
    "JTs": _d(0.05, 0.65), "T9s": _d(0, 0.60),
    "98s": _d(0, 0.45), "87s": _d(0, 0.35),
    "76s": _d(0, 0.25), "65s": _d(0, 0.20),
    # Bluff 3-bets — suited aces with blockers
    "A5s": _d(0.65), "A4s": _d(0.50), "A3s": _d(0.40), "A2s": _d(0.25),
    "K9s": _d(0.30, 0.40),
    "Q9s": _d(0.25, 0.30),
}

# BB defense — closing the action, so call wider and 3-bet less for bluff.
_BB_VS_EARLY = {
    "AA": _d(1.0), "KK": _d(1.0),
    "QQ": _d(0.80, 0.20), "JJ": _d(0.30, 0.70),
    "TT": _d(0, 1.0), "99": _d(0, 1.0), "88": _d(0, 1.0),
    "77": _d(0, 1.0), "66": _d(0, 1.0), "55": _d(0, 0.90),
    "44": _d(0, 0.70), "33": _d(0, 0.50), "22": _d(0, 0.40),
    "AKs": _d(0.70, 0.30), "AKo": _d(0.60, 0.40),
    "AQs": _d(0.30, 0.70), "AQo": _d(0, 0.95),
    "AJs": _d(0, 1.0), "AJo": _d(0, 0.85), "ATs": _d(0, 1.0),
    "A9s": _d(0, 0.95), "A8s": _d(0, 0.85), "A7s": _d(0, 0.70),
    "A5s": _d(0.20, 0.60), "A4s": _d(0, 0.55),
    "KQs": _d(0, 1.0), "KQo": _d(0, 0.85),
    "KJs": _d(0, 1.0), "KJo": _d(0, 0.70),
    "KTs": _d(0, 1.0), "K9s": _d(0, 0.85), "K8s": _d(0, 0.55),
    "QJs": _d(0, 1.0), "QTs": _d(0, 0.95), "Q9s": _d(0, 0.60),
    "JTs": _d(0, 1.0), "J9s": _d(0, 0.80), "T9s": _d(0, 0.95),
    "T8s": _d(0, 0.55), "98s": _d(0, 0.85), "87s": _d(0, 0.70),
    "76s": _d(0, 0.55), "65s": _d(0, 0.45), "54s": _d(0, 0.35),
}

_BB_VS_MIDDLE = {
    "AA": _d(1.0), "KK": _d(1.0),
    "QQ": _d(0.85, 0.15), "JJ": _d(0.40, 0.60),
    "TT": _d(0, 1.0), "99": _d(0, 1.0), "88": _d(0, 1.0),
    "77": _d(0, 1.0), "66": _d(0, 1.0), "55": _d(0, 1.0),
    "44": _d(0, 0.90), "33": _d(0, 0.75), "22": _d(0, 0.60),
    "AKs": _d(0.75, 0.25), "AKo": _d(0.65, 0.35),
    "AQs": _d(0.40, 0.60), "AQo": _d(0.10, 0.85),
    "AJs": _d(0, 1.0), "AJo": _d(0, 0.95), "ATs": _d(0, 1.0), "ATo": _d(0, 0.80),
    "A9s": _d(0, 1.0), "A8s": _d(0, 0.85), "A7s": _d(0, 0.75),
    "A6s": _d(0, 0.55), "A5s": _d(0.25, 0.65), "A4s": _d(0.15, 0.55),
    "KQs": _d(0, 1.0), "KQo": _d(0, 0.85),
    "KJs": _d(0, 1.0), "KJo": _d(0, 0.60),
    "KTs": _d(0, 1.0), "KTo": _d(0, 0.30),
    "K9s": _d(0, 0.85), "K8s": _d(0, 0.60), "K7s": _d(0, 0.40),
    "QJs": _d(0, 1.0), "QJo": _d(0, 0.50),
    "QTs": _d(0, 1.0), "Q9s": _d(0, 0.65), "Q8s": _d(0, 0.30),
    "JTs": _d(0, 1.0), "J9s": _d(0, 0.85), "J8s": _d(0, 0.40),
    "T9s": _d(0, 1.0), "T8s": _d(0, 0.70),
    "98s": _d(0, 1.0), "87s": _d(0, 0.85), "76s": _d(0, 0.70),
    "65s": _d(0, 0.55), "54s": _d(0, 0.40), "43s": _d(0, 0.20),
}

_BB_VS_LATE = {
    "AA": _d(1.0), "KK": _d(1.0),
    "QQ": _d(0.90, 0.10), "JJ": _d(0.55, 0.45),
    "TT": _d(0.25, 0.70),
    "99": _d(0, 1.0), "88": _d(0, 1.0), "77": _d(0, 1.0),
    "66": _d(0, 1.0), "55": _d(0, 1.0), "44": _d(0, 1.0),
    "33": _d(0, 0.95), "22": _d(0, 0.90),
    "AKs": _d(0.65, 0.35), "AKo": _d(0.55, 0.45),
    "AQs": _d(0.40, 0.60), "AQo": _d(0.25, 0.70),
    "AJs": _d(0.20, 0.80), "AJo": _d(0, 1.0), "ATs": _d(0, 1.0), "ATo": _d(0, 0.95),
    "A9s": _d(0, 1.0), "A9o": _d(0, 0.90),
    "A8s": _d(0, 1.0), "A8o": _d(0, 0.70),
    "A7s": _d(0, 1.0), "A7o": _d(0, 0.50),
    "A6s": _d(0, 1.0), "A5s": _d(0.40, 0.60),
    "A4s": _d(0.30, 0.70), "A3s": _d(0.20, 0.75), "A2s": _d(0, 0.85),
    "A6o": _d(0, 0.30), "A5o": _d(0, 0.50), "A4o": _d(0, 0.30),
    "KQs": _d(0.15, 0.85), "KQo": _d(0, 1.0),
    "KJs": _d(0, 1.0), "KJo": _d(0, 0.95),
    "KTs": _d(0, 1.0), "KTo": _d(0, 0.70),
    "K9s": _d(0.20, 0.75), "K8s": _d(0, 0.80), "K7s": _d(0, 0.60),
    "K9o": _d(0, 0.50),
    "QJs": _d(0, 1.0), "QJo": _d(0, 0.85),
    "QTs": _d(0, 1.0), "QTo": _d(0, 0.60),
    "Q9s": _d(0.15, 0.80), "Q8s": _d(0, 0.70),
    "JTs": _d(0, 1.0), "JTo": _d(0, 0.70),
    "J9s": _d(0.10, 0.85), "J8s": _d(0, 0.50),
    "T9s": _d(0, 1.0), "T8s": _d(0, 0.70),
    "98s": _d(0, 1.0), "87s": _d(0, 0.95),
    "76s": _d(0, 0.85), "65s": _d(0, 0.75),
    "54s": _d(0, 0.60), "43s": _d(0, 0.35),
    "97s": _d(0, 0.55), "86s": _d(0, 0.40),
}

_DEFENSE_TABLES = {
    ("EARLY", "BB"): _BB_VS_EARLY,
    ("MIDDLE", "BB"): _BB_VS_MIDDLE,
    ("LATE", "BB"): _BB_VS_LATE,
    ("EARLY", "IP"): _VS_EARLY,
    ("MIDDLE", "IP"): _VS_MIDDLE,
    ("LATE", "IP"): _VS_LATE,
    ("EARLY", "OOP"): _VS_EARLY,
    ("MIDDLE", "OOP"): _VS_MIDDLE,
    ("LATE", "OOP"): _VS_LATE,
}


def _defender_role(opener_pos, our_pos):
    """IP / OOP / BB. BB is its own role (closing the action with discounted
    price). Otherwise IP if we act after opener post-flop."""
    if our_pos == "BB":
        return "BB"
    # Post-flop seat order (BTN = best position).
    order = ["UTG", "UTG1", "LJ", "HJ", "CO", "BTN"]
    if our_pos in ("SB", "BB"):
        return "OOP"  # SB plays OOP postflop vs everyone except BB
    try:
        return "IP" if order.index(our_pos) > order.index(opener_pos) else "OOP"
    except ValueError:
        return "OOP"


def _defense_distribution(opener_pos, our_pos, hand):
    tier = _OPENER_TIER.get(opener_pos, "MIDDLE")
    role = _defender_role(opener_pos, our_pos)
    table = _DEFENSE_TABLES.get((tier, role), _VS_MIDDLE)
    return table.get(hand, _d(0, 0))


# 4-bet defense: we opened and now face a 3-bet.
_VS_3BET = {
    "AA": _d(1.0), "KK": _d(0.95, 0.05),
    "QQ": _d(0.50, 0.50), "JJ": _d(0, 0.85), "TT": _d(0, 0.65),
    "99": _d(0, 0.40), "88": _d(0, 0.25),
    "AKs": _d(0.85, 0.15), "AKo": _d(0.50, 0.50),
    "AQs": _d(0, 0.85), "AQo": _d(0, 0.30),
    "AJs": _d(0, 0.55), "ATs": _d(0, 0.30),
    "KQs": _d(0, 0.50), "KJs": _d(0, 0.20),
    "QJs": _d(0, 0.20), "JTs": _d(0, 0.20),
    # Small 4-bet bluffs with blockers
    "A5s": _d(0.15), "A4s": _d(0.10),
}


def _four_bet_distribution(hand):
    return _VS_3BET.get(hand, _d(0, 0))


# ---- Short-stack push/fold tables -----------------------------------------

_JAM_20BB = {
    "UTG": {"AA", "KK", "QQ", "JJ", "TT", "99", "88",
            "AKs", "AKo", "AQs", "AQo", "AJs"},
    "HJ":  {"AA", "KK", "QQ", "JJ", "TT", "99", "88", "77", "66",
            "AKs", "AKo", "AQs", "AQo", "AJs", "AJo", "ATs", "KQs"},
    "CO":  {"AA", "KK", "QQ", "JJ", "TT", "99", "88", "77", "66", "55",
            "AKs", "AKo", "AQs", "AQo", "AJs", "AJo", "ATs", "ATo",
            "A9s", "A8s", "A7s", "A5s", "KQs", "KQo", "KJs", "QJs"},
    "BTN": {"AA", "KK", "QQ", "JJ", "TT", "99", "88", "77", "66", "55", "44",
            "33", "22",
            "AKs", "AKo", "AQs", "AQo", "AJs", "AJo", "ATs", "ATo", "A9s",
            "A9o", "A8s", "A8o", "A7s", "A6s", "A5s", "A4s", "A3s", "A2s",
            "KQs", "KQo", "KJs", "KJo", "KTs", "KTo", "K9s", "K8s",
            "QJs", "QJo", "QTs", "Q9s", "JTs", "J9s", "T9s", "98s", "87s"},
    "SB":  {"AA", "KK", "QQ", "JJ", "TT", "99", "88", "77", "66", "55", "44",
            "33", "22",
            "AKs", "AKo", "AQs", "AQo", "AJs", "AJo", "ATs", "ATo", "A9s",
            "A9o", "A8s", "A8o", "A7s", "A7o", "A6s", "A5s", "A5o", "A4s",
            "A3s", "A2s",
            "KQs", "KQo", "KJs", "KJo", "KTs", "KTo", "K9s", "K9o", "K8s",
            "K7s",
            "QJs", "QJo", "QTs", "QTo", "Q9s", "Q8s",
            "JTs", "JTo", "J9s", "T9s", "T8s", "98s", "87s", "76s", "65s"},
}

_JAM_12BB = {
    "UTG": _JAM_20BB["UTG"] | {"77", "66", "AJo", "KQs"},
    "HJ":  _JAM_20BB["HJ"]  | {"55", "44", "ATo", "KQo", "KJs", "QJs"},
    "CO":  _JAM_20BB["CO"]  | {"44", "33", "22", "KJo", "KTs", "K9s",
                               "QJo", "QTs", "JTs", "T9s"},
    "BTN": _JAM_20BB["BTN"] | {"KTo", "K9o", "K8s", "K7s", "Q8s", "Q7s",
                               "J8s", "T8s", "97s", "86s", "76s", "65s", "54s"},
    "SB":  _JAM_20BB["SB"]  | {"K8o", "K7o", "Q8o", "Q7s", "J8s", "T7s",
                               "97s", "86s", "75s", "54s"},
}

_JAM_8BB = {
    "UTG": _JAM_12BB["UTG"] | {"55", "44", "ATo", "KJs", "QJs"},
    "HJ":  _JAM_12BB["HJ"]  | {"33", "22", "A6o", "K9s", "JTs"},
    "CO":  _JAM_12BB["CO"]  | {"K7s", "Q8s", "J8s", "T8s", "97s", "76s", "65s"},
    "BTN": _JAM_12BB["BTN"] | {"K6s", "K5s", "Q6s", "J7s", "T7s", "96s",
                               "85s", "75s", "64s", "53s", "43s"},
    "SB":  _JAM_12BB["SB"]  | {"K6s", "K5s", "K4s", "Q6s", "Q5s", "Q4s",
                               "J7s", "J6s", "T6s", "96s", "85s", "74s",
                               "63s", "52s", "32s", "43s"},
}


def _jam_hands(eff_bb, pos):
    """Set of hands to open-jam at this effective stack depth from this seat."""
    if pos not in _JAM_20BB:
        # Map UTG1/LJ to UTG range; otherwise default to BTN (loosest).
        if pos in ("UTG1", "LJ"):
            pos = "UTG"
        elif pos == "MP":
            pos = "HJ"
        else:
            pos = "BTN"
    if eff_bb <= 8:
        return _JAM_8BB.get(pos, set())
    if eff_bb <= 12:
        return _JAM_12BB.get(pos, set())
    if eff_bb <= 20:
        return _JAM_20BB.get(pos, set())
    return set()


def _effective_stack_bb(state):
    """Effective stack vs largest live opponent, in big blinds.
    Falls back to v4.1's pot-relative estimate if blinds aren't visible."""
    me = state.get("seat_to_act")
    my_stack = state.get("your_stack", 0) + state.get("your_bet_this_street", 0)
    opp_stacks = []
    for p in state.get("players") or []:
        try:
            if p["seat"] == me:
                continue
            if p.get("is_folded") or p.get("state") == "busted":
                continue
            s = p.get("stack", 0) + p.get("bet_this_street", 0)
            if s > 0:
                opp_stacks.append(s)
        except (KeyError, TypeError):
            continue
    eff = min(my_stack, max(opp_stacks)) if opp_stacks else my_stack

    # Big blind: walk the action log for the big_blind amount. Failing that,
    # use the standard engine constant (100).
    bb = 100
    for a in state.get("action_log") or []:
        if isinstance(a, dict) and a.get("action") == "big_blind":
            bb_amt = a.get("amount")
            if bb_amt:
                bb = bb_amt
                break
    return eff / bb if bb else eff


# ---- Preflop history (richer than v4.1's _facing_raise) -------------------

def _preflop_history(state):
    """Reuse v4.1's _reconstruct_hand to get richer preflop context than the
    plain _facing_raise() boolean. Returns:
        raises:              count of preflop raises so far
        first_raiser_seat:   seat index of first raiser (or None)
        last_raiser_seat:    seat of most recent raiser
        last_raiser_was_us:  bool
        first_raiser_pos:    position name (UTG..SB) of first raiser
        last_raiser_pos:     position name of most recent raiser
    """
    info = {"raises": 0,
            "first_raiser_seat": None, "last_raiser_seat": None,
            "last_raiser_was_us": False,
            "first_raiser_pos": None, "last_raiser_pos": None}
    me = state.get("seat_to_act")
    log = state.get("action_log") or []
    positions = _seat_positions(state)
    for rec in _reconstruct_hand(log):
        if rec["street_idx"] != 0:
            continue
        if rec["action"] in ("raise", "all_in"):
            info["raises"] += 1
            seat = rec["seat"]
            if info["first_raiser_seat"] is None:
                info["first_raiser_seat"] = seat
                info["first_raiser_pos"] = positions.get(seat)
            info["last_raiser_seat"] = seat
            info["last_raiser_pos"] = positions.get(seat)
            info["last_raiser_was_us"] = (seat == me)
    return info


def _is_facing_jam(state):
    """True if calling the current bet would put us all-in."""
    owed = state.get("amount_owed", 0)
    stack = state.get("your_stack", 0)
    if stack > 0 and owed >= stack:
        return True
    # Also check the log for a recent all_in action.
    for a in (state.get("action_log") or [])[-8:]:
        if isinstance(a, dict) and a.get("action") == "all_in":
            return True
    return False


def _seats_in_hand(state):
    """Count of seats that haven't folded."""
    n = 0
    for p in state.get("players") or []:
        if isinstance(p, dict) and not p.get("is_folded") and p.get("state") != "busted":
            n += 1
    return n


def _sample_dist(dist, rng):
    """Sample a key from {key: freq} using the spot-deterministic rng."""
    r = rng.random()
    cum = 0.0
    for k, v in dist.items():
        cum += v
        if r < cum:
            return k
    return max(dist.items(), key=lambda kv: kv[1])[0]


def _bb_to_chips(state, bb_count):
    """Convert a big-blind multiple to a raise total."""
    # Find BB amount from log (engine constant fallback = 100).
    bb = 100
    for a in state.get("action_log") or []:
        if isinstance(a, dict) and a.get("action") == "big_blind":
            amt = a.get("amount")
            if amt:
                bb = amt
                break
    target = int(bb_count * bb)
    # Snap to legal raise.
    min_to = state.get("min_raise_to", 0)
    cap = state.get("your_stack", 0) + state.get("your_bet_this_street", 0)
    return min(max(target, min_to), cap)


# ---- The new preflop entry point -------------------------------------------

SHORT_STACK_BB = 20.0


def _preflop_v6(state):
    """Chart-based preflop. Returns an action dict, same shape as _safetag's
    preflop branch. Falls back to v4.1's original logic on any failure."""
    hole = state.get("your_cards", []) or []
    hand = _canonical(hole)
    pos = _hero_position_name(state)
    if hand is None or pos is None:
        # Position detection failed (e.g. blinds not in log yet). Fall back
        # to v4.1's preflop. Caller signals this by returning None.
        return None

    can_check = state.get("can_check", False)
    stack = state.get("your_stack", 0)
    owed = state.get("amount_owed", 0)
    history = _preflop_history(state)
    facing_jam = _is_facing_jam(state)
    eff_bb = _effective_stack_bb(state)
    rng = _spot_rng(state)

    # ------------------------------------------------------------------
    # Equity filter for committing decisions — PRESERVED from v4.1.
    # When facing a bet that would force us all-in (or near it), the chart
    # frequencies don't apply — we want to call if equity beats the price.
    # ------------------------------------------------------------------
    committing = owed >= 0.6 * (stack + state.get("your_bet_this_street", 0))
    if history["raises"] >= 1 and committing and owed > 0:
        cls = _preflop_class(hole)
        n_opp = _n_live_opponents(state)
        # v7: range-aware equity. _hero_equity falls back to random on any
        # failure, so we still get an answer even if eval7 / ranges are bad.
        eq = _hero_equity(hole, state.get("community_cards", []),
                          state, n_opp, rng)
        if eq is None:
            eq = _preflop_equity_guess(cls, n_opp)
        req = _required_equity(state)
        if eq >= req + 0.06:
            return {"action": "call"}
        return {"action": "fold"}

    # ------------------------------------------------------------------
    # Short-stack mode: ≤ 20bb effective.
    # ------------------------------------------------------------------
    if eff_bb <= SHORT_STACK_BB:
        if facing_jam:
            # Calling an all-in: use the equity filter (which the committing
            # branch above usually already handles, but fall through here in
            # the rare case it didn't). v7: range-aware.
            cls = _preflop_class(hole)
            n_opp = _n_live_opponents(state)
            eq = _hero_equity(hole, state.get("community_cards", []),
                              state, n_opp, rng)
            if eq is None:
                eq = _preflop_equity_guess(cls, n_opp)
            req = _required_equity(state)
            if eq >= req + 0.06:
                return {"action": "call"}
            return {"action": "fold"}
        if history["raises"] == 0:
            # First-in: jam if our hand is in the depth-appropriate jam range.
            jam_set = _jam_hands(eff_bb, pos)
            if hand in jam_set:
                return {"action": "all_in"}
            # Below 12bb, jam-or-fold strictly.
            if eff_bb <= 12:
                if can_check:
                    return {"action": "check"}
                return {"action": "fold"}
            # 12-20bb: with strong-but-not-jam hands, min-raise the chart.
            dist = _open_distribution(pos, hand)
            if dist.get("raise", 0) > 0.5:
                return {"action": "raise", "amount": _bb_to_chips(state, 2.2)}
            if can_check:
                return {"action": "check"}
            return {"action": "fold"}
        # Facing an open short-stacked: use defense chart (the chart's call
        # branches are mostly safe at 20bb, and our jam decisions are handled
        # by the committing-equity filter above).
        return _defense_branch(state, hand, pos, history, rng)

    # ------------------------------------------------------------------
    # 100bb mode.
    # ------------------------------------------------------------------
    if history["raises"] == 0:
        # First in: use the open chart.
        return _open_branch(state, hand, pos, rng)

    # There's been at least one raise.
    if history["last_raiser_was_us"]:
        # We opened and got 3-bet (or 5-bet, etc.). Use 4-bet defense.
        return _four_bet_branch(state, hand, rng)

    # Someone else opened.
    return _defense_branch(state, hand, pos, history, rng)


def _open_branch(state, hand, pos, rng):
    """First-in open decision via the position chart, with HU SB override."""
    # Heads-up override: the 8-max SB chart limps 81% which is wrong HU.
    if _seats_in_hand(state) == 2 and pos == "SB":
        return _hu_btn_open(state, hand, rng)
    dist = _open_distribution(pos, hand)
    choice = _sample_dist(dist, rng)
    if choice == "raise":
        size_bb = _OPEN_SIZE_BB.get(pos, 2.5)
        return {"action": "raise", "amount": _bb_to_chips(state, size_bb)}
    if choice == "call":
        # SB limp branch — call BB-amount.
        return {"action": "call"}
    if state.get("can_check", False):
        return {"action": "check"}
    return {"action": "fold"}


def _hu_btn_open(state, hand, rng):
    """HU button (=SB) open. Open ~85% of hands; only the worst trash folds."""
    if hand in _HU_BTN_NEVER_OPEN:
        if rng.random() < 0.10:
            return {"action": "raise", "amount": _bb_to_chips(state, 2.5)}
        if state.get("can_check", False):
            return {"action": "check"}
        return {"action": "fold"}
    return {"action": "raise", "amount": _bb_to_chips(state, 2.5)}


def _defense_branch(state, hand, our_pos, history, rng):
    """Facing an open. Use the opener-tier × defender-role table."""
    opener_pos = history["first_raiser_pos"] or "HJ"
    dist = _defense_distribution(opener_pos, our_pos, hand)
    choice = _sample_dist(dist, rng)
    if choice == "3bet":
        # IP defenders 3-bet to ~3x the open, OOP to ~4x. Use the current_bet
        # (which equals the opener's raise-to) as the base.
        opener_size = state.get("current_bet", 0)
        in_pos = _defender_role(opener_pos, our_pos) == "IP"
        multiplier = 3.0 if in_pos else 4.0
        target = int(opener_size * multiplier)
        min_to = state.get("min_raise_to", 0)
        cap = state.get("your_stack", 0) + state.get("your_bet_this_street", 0)
        target = min(max(target, min_to), cap)
        if target >= cap:
            return {"action": "all_in"}
        return {"action": "raise", "amount": target}
    if choice == "call":
        return {"action": "call"}
    if state.get("can_check", False):
        return {"action": "check"}
    return {"action": "fold"}


def _four_bet_branch(state, hand, rng):
    """We opened, got 3-bet. Use the 4-bet defense table."""
    dist = _four_bet_distribution(hand)
    choice = _sample_dist(dist, rng)
    if choice == "3bet":
        # "3bet" here means 4-bet (we're the most-recent raiser). Size ~2.25x
        # their 3-bet.
        three_bet_to = state.get("current_bet", 0)
        target = int(three_bet_to * 2.25)
        min_to = state.get("min_raise_to", 0)
        cap = state.get("your_stack", 0) + state.get("your_bet_this_street", 0)
        target = min(max(target, min_to), cap)
        if target >= cap:
            return {"action": "all_in"}
        return {"action": "raise", "amount": target}
    if choice == "call":
        return {"action": "call"}
    return {"action": "fold"}


def _postflop_strength(hole, board):
    try:
        cards = hole + board
        ranks = [c[0] for c in cards]
        suits = [c[1] for c in cards]
    except (TypeError, IndexError):
        return "air", False, False
    rank_counts = {}
    for r in ranks:
        rank_counts[r] = rank_counts.get(r, 0) + 1
    counts = sorted(rank_counts.values(), reverse=True)
    hole_vals = sorted((_RANK_VAL.get(c[0], 0) for c in hole), reverse=True)
    board_vals = set(_RANK_VAL.get(c[0], 0) for c in board)
    if counts and counts[0] >= 4:
        return "strong", False, False
    if len(counts) >= 2 and counts[0] >= 3 and counts[1] >= 2:
        return "strong", False, False
    if counts and counts[0] >= 3:
        return "strong", False, False
    if counts.count(2) >= 2:
        return "strong", False, False
    suit_counts = {}
    for s in suits:
        suit_counts[s] = suit_counts.get(s, 0) + 1
    max_suit = max(suit_counts.values()) if suit_counts else 0
    if max_suit >= 5:
        return "strong", False, False
    flush_draw = max_suit == 4
    uniq = sorted(set(_RANK_VAL.get(r, 0) for r in ranks))
    if 14 in uniq:
        uniq = sorted(set(uniq + [1]))
    made_straight = _has_run(uniq, 5)
    oesd = (not made_straight) and _has_run(uniq, 4)
    if made_straight:
        return "strong", False, False
    if counts and counts[0] == 2:
        paired_rank = next(_RANK_VAL.get(r, 0)
                           for r, c in rank_counts.items() if c == 2)
        top_board = max(board_vals) if board_vals else 0
        if paired_rank > top_board or (hole_vals and hole_vals[0] >= top_board
                                       and paired_rank in hole_vals):
            return "medium", flush_draw, oesd
        return "weak", flush_draw, oesd
    if flush_draw or oesd:
        return "weak", flush_draw, oesd
    return "air", flush_draw, oesd


def _has_run(sorted_vals, length):
    if len(sorted_vals) < length:
        return False
    run = 1
    for i in range(1, len(sorted_vals)):
        if sorted_vals[i] == sorted_vals[i - 1] + 1:
            run += 1
            if run >= length:
                return True
        else:
            run = 1
    return False


def _board_is_dry(board):
    try:
        suits = [c[1] for c in board]
        vals = sorted(_RANK_VAL.get(c[0], 0) for c in board)
    except Exception:
        return False
    max_suit = max((suits.count(s) for s in set(suits)), default=0)
    if max_suit >= 3:
        return False
    if len(set(c[0] for c in board)) < len(board):
        return False
    return (vals[-1] - vals[0]) >= 5 if vals else False


# ===========================================================================
# Position / sizing / odds
# ===========================================================================

def _spot_seed(state):
    """Stable 32-bit seed for THIS decision point. Same spot -> same seed,
    independent of how the global RNG stream was advanced elsewhere (warmup
    equity touch, stat reconstruction, prior hands). This is what makes paired
    A/B comparisons clean: only a genuine strategy difference changes the rolls.
    Uses zlib.crc32 (not built-in hash(), which is salted per process)."""
    try:
        parts = [
            str(state.get("hand_id", "")),
            str(state.get("street", "")),
            str(state.get("seat_to_act", "")),
            "".join(state.get("your_cards", []) or []),
            "".join(state.get("community_cards", []) or []),
            str(len(state.get("action_log", []) or [])),
            str(state.get("current_bet", 0)),
            str(state.get("amount_owed", 0)),
        ]
        return zlib.crc32("|".join(parts).encode("utf-8")) & 0xffffffff
    except Exception:
        return 0


def _spot_rng(state):
    return random.Random(_spot_seed(state))


def _position(state):
    try:
        return state["seat_to_act"] / max(len(state["players"]) - 1, 1)
    except Exception:
        return 0.5


def _facing_raise(state):
    try:
        for a in state.get("action_log", []):
            if a.get("action") in ("raise", "all_in"):
                return True
    except Exception:
        pass
    return False


def _raise_to(state, pot_fraction):
    pot = state.get("pot", 0)
    min_to = state.get("min_raise_to", 0)
    my_bet = state.get("your_bet_this_street", 0)
    stack = state.get("your_stack", 0)
    cap = stack + my_bet
    target = state.get("current_bet", 0) + int(pot * pot_fraction)
    return min(max(target, min_to), cap)


def _required_equity(state):
    owed = state.get("amount_owed", 0)
    pot = state.get("pot", 0)
    if owed <= 0:
        return 0.0
    return owed / (pot + owed)


# ===========================================================================
# Strategy (SafeTAG, now equity-driven postflop) — V2 behaviour
# ===========================================================================

def _safetag(state):
    can_check = state.get("can_check", False)
    street = state.get("street", "preflop")
    hole = state.get("your_cards", []) or []
    board = state.get("community_cards", []) or []
    pos = _position(state)
    n_opp = _n_live_opponents(state)
    rng = _spot_rng(state)        # deterministic for this exact spot

    # ---- PREFLOP ---- (v6: chart-based; v4.1 logic kept as fallback)
    if street == "preflop":
        # First try the chart-based path. _preflop_v6 returns None ONLY when
        # position detection fails (action_log doesn't contain blind posts
        # yet). In that case fall through to the v4.1 path below.
        v6_action = _preflop_v6(state)
        if v6_action is not None:
            return v6_action

        # ---- v4.1 fallback (byte-identical to the original) ----
        cls = _preflop_class(hole)
        facing = _facing_raise(state)
        owed = state.get("amount_owed", 0)
        stack = state.get("your_stack", 0)

        # Facing an all-in (or a bet that would commit a big share of stack):
        # use an equity filter instead of the range chart.
        committing = owed >= 0.6 * (stack + state.get("your_bet_this_street", 0))
        if facing and committing and owed > 0:
            # v7: range-aware (this fallback path is only hit when position
            # detection failed, which is rare — but the upgrade is free).
            eq = _hero_equity(hole, board, state, n_opp, rng)
            if eq is None:
                eq = _preflop_equity_guess(cls, n_opp)
            req = _required_equity(state)
            if eq >= req + 0.06:               # margin for being dominated
                return {"action": "call"}
            return {"action": "fold"}

        if not facing:
            open_floor = "playable" if pos > 0.5 else "strong"
            if _at_least(cls, open_floor):
                return {"action": "raise", "amount": _raise_to(state, 1.0)}
            if can_check:
                return {"action": "check"}
            if _at_least(cls, "speculative") and _required_equity(state) <= 0.18:
                return {"action": "call"}
            return {"action": "fold"}

        # Facing a normal raise.
        if cls == "premium":
            return {"action": "raise", "amount": _raise_to(state, 1.0)}
        if cls == "strong":
            return {"action": "call"}
        if cls == "playable" and (pos > 0.5 or _required_equity(state) <= 0.12):
            return {"action": "call"}
        if can_check:
            return {"action": "check"}
        return {"action": "fold"}

    # ---- POSTFLOP ----
    # v7: range-aware equity replaces the random sampler. _hero_equity falls
    # back to equity_vs_random transparently if range estimation is off /
    # eval7 is missing / every opponent is UNKNOWN.
    eq = _hero_equity(hole, board, state, n_opp, rng)
    if eq is not None:
        return _postflop_by_equity(state, eq, pos, can_check, board, rng)
    # Fallback: V1 heuristic buckets.
    return _postflop_by_heuristic(state, hole, board, pos, can_check, rng)


def _postflop_by_equity(state, eq, pos, can_check, board, rng):
    req = _required_equity(state)

    # Strong value: bet/raise.
    if eq >= 0.72:
        if can_check:
            return {"action": "raise", "amount": _raise_to(state, 0.7)}
        if eq >= 0.85 and rng.random() < 0.6:
            return {"action": "raise", "amount": _raise_to(state, 0.9)}
        return {"action": "call"}

    # Good but not nutted: bet for value / call profitably.
    if eq >= 0.55:
        if can_check:
            if pos > 0.5 and rng.random() < 0.65:
                return {"action": "raise", "amount": _raise_to(state, 0.55)}
            return {"action": "check"}
        if eq >= req + 0.05:
            return {"action": "call"}
        return {"action": "fold"}

    # Marginal showdown / draws: pot-odds call, occasional thin probe.
    if eq >= 0.42:
        if can_check:
            return {"action": "check"}
        if eq >= req:
            return {"action": "call"}
        return {"action": "fold"}

    # Weak: mostly give up; rare dry-board steal in position.
    if can_check:
        if pos > 0.6 and _board_is_dry(board) and rng.random() < 0.18:
            return {"action": "raise", "amount": _raise_to(state, 0.5)}
        return {"action": "check"}
    return {"action": "fold"}


def _postflop_by_heuristic(state, hole, board, pos, can_check, rng):
    bucket, fd, oesd = _postflop_strength(hole, board)
    drawy = fd or oesd
    req = _required_equity(state)
    if bucket == "strong":
        if can_check:
            return {"action": "raise", "amount": _raise_to(state, 0.66)}
        if rng.random() < 0.55:
            return {"action": "raise", "amount": _raise_to(state, 0.9)}
        return {"action": "call"}
    if bucket == "medium":
        if can_check:
            if pos > 0.55 and rng.random() < 0.6:
                return {"action": "raise", "amount": _raise_to(state, 0.5)}
            return {"action": "check"}
        return {"action": "call"} if req <= 0.33 else {"action": "fold"}
    if drawy:
        if can_check:
            if rng.random() < 0.25:
                return {"action": "raise", "amount": _raise_to(state, 0.5)}
            return {"action": "check"}
        return {"action": "call"} if req <= 0.30 else {"action": "fold"}
    if can_check:
        if pos > 0.6 and _board_is_dry(board) and rng.random() < 0.2:
            return {"action": "raise", "amount": _raise_to(state, 0.5)}
        return {"action": "check"}
    return {"action": "fold"}


# ===========================================================================
# Safety wrapper + entry point (V0)
# ===========================================================================

def _sanitize(action, state):
    if not isinstance(action, dict):
        action = {"action": "fold"}
    a = str(action.get("action", "fold")).lower().strip()
    can_check = state.get("can_check", False)
    if a == "check":
        return {"action": "check"} if can_check else {"action": "call"}
    if a == "call":
        return {"action": "check"} if can_check else {"action": "call"}
    if a == "all_in":
        return {"action": "all_in"}
    if a == "raise":
        try:
            amt = int(action.get("amount"))
        except (TypeError, ValueError):
            amt = state.get("min_raise_to", 0)
        min_to = state.get("min_raise_to", 0)
        cap = state.get("your_stack", 0) + state.get("your_bet_this_street", 0)
        amt = max(amt, min_to)
        return {"action": "all_in"} if amt >= cap else {"action": "raise", "amount": amt}
    return {"action": "fold"}


def _emergency(state):
    try:
        if state.get("can_check", False):
            return {"action": "check"}
        owed = state.get("amount_owed", 0)
        pot = state.get("pot", 1)
        if 0 < owed < pot * 0.15:
            return {"action": "call"}
    except Exception:
        pass
    return {"action": "fold"}


# ===========================================================================
# V4 — classifiers (implementation + behavior), confidence, exploit weight
# ===========================================================================
# COLLECTION ONLY in V4: these read PLAYER_STATS and produce posteriors but do
# not change the action. V5 will consume LAST_READ to drive the exploit policy.

IMPLEMENTATION_PRIORS = {
    "simple_tag":      0.30,
    "monte_carlo":     0.25,
    "neural_net_like": 0.10,
    "rule_shark":      0.10,
    "calling_bot":     0.10,
    "random":          0.07,
    "perma_all_in":    0.05,
    "folding_bot":     0.03,
}

# Expected Bernoulli rates per archetype over a fixed signal set. Each signal is
# (successes y, opportunities n); MAP weights by n automatically, so cold-start
# opponents collapse to the prior. Keys must match _signals() below.
# Bucket signals are call-rate when facing a SMALL / MEDIUM / BIG bet (bet/pot
# <=1/3, 1/3..3/4, >3/4). A pot-odds (monte_carlo) bot calls small bets far more
# than big ones -> steep cheap>medium>expensive gradient; a station calls all
# sizes; a folding bot calls none; a nit/TAG is in between with a gentler slope.
#
# Retuned (6-max probe fix): fold_vs_bet is down-weighted because a tight/TAG or
# pot-odds bot folds to bets a LOT at an aggressive table, which previously made
# raw fold rate dominate and collapse them into folding_bot. The discriminating
# signal between "folds because the price was bad" (monte_carlo/TAG) and "folds
# everything" (folding_bot) is the bucket GRADIENT: whether the opponent still
# calls CHEAP bets while folding expensive ones. We add an explicit gradient
# signal (9th) = P(the call landed on a cheap bet | it called at all), which is
# high for price-sensitive bots and ~flat for true folders/stations.
#                       allin foldb callb  vpip  pfr  cheap  med   exp   grad
_IMPL_PROFILE = {
    "simple_tag":      (0.04, 0.55, 0.30, 0.26, 0.20, 0.55, 0.35, 0.18, 0.60),
    "rule_shark":      (0.04, 0.52, 0.30, 0.22, 0.18, 0.52, 0.33, 0.20, 0.58),
    "monte_carlo":     (0.05, 0.45, 0.45, 0.50, 0.22, 0.80, 0.45, 0.12, 0.75),
    "calling_bot":     (0.05, 0.08, 0.85, 0.80, 0.10, 0.95, 0.92, 0.88, 0.36),
    "folding_bot":     (0.02, 0.80, 0.12, 0.25, 0.10, 0.28, 0.12, 0.05, 0.45),
    "random":          (0.20, 0.50, 0.38, 0.60, 0.40, 0.50, 0.50, 0.50, 0.40),
    "perma_all_in":    (0.60, 0.10, 0.12, 0.90, 0.80, 0.50, 0.50, 0.50, 0.40),
    "neural_net_like": (0.08, 0.42, 0.40, 0.45, 0.30, 0.55, 0.45, 0.38, 0.48),
}
# Weights per signal. fold_vs_bet down-weighted 1.0 -> 0.4 (was dominating);
# the price buckets and the new gradient signal carry the discrimination load.
#                  allin foldb callb vpip  pfr  cheap med  exp  grad
_IMPL_SIGNAL_W = (1.0,  0.4,  0.8,  0.8, 0.8, 1.3, 1.2, 1.4, 1.6)
_EPS = 1e-6


def _signals(st):
    """Return list of (y, n) per signal, in the order matching _IMPL_PROFILE."""
    cheap_c = st.get("cheap_called", 0)
    exp_c = st.get("expensive_called", 0)
    # Gradient signal: among calls made facing CHEAP or EXPENSIVE bets, what
    # fraction landed on the cheap side? Price-sensitive bots (monte_carlo, TAG)
    # call cheap and fold expensive -> ratio ~1.0. A true folding_bot folds both
    # cheap and expensive so it has few calls either way (low n -> contributes
    # little, correctly), and a station calls both -> ratio near the faced base
    # rate. This separates "folds because price was bad" from "folds everything".
    grad_y = cheap_c
    grad_n = cheap_c + exp_c
    return [
        (st.get("allins", 0),          st.get("actions", 0)),
        (st.get("fold_vs_bet", 0),     st.get("faced_bet", 0)),
        (st.get("call_vs_bet", 0),     st.get("faced_bet", 0)),
        (st.get("vpip_yes", 0),        st.get("vpip_opp", 0)),
        (st.get("pfr_yes", 0),         st.get("pfr_opp", 0)),
        (cheap_c,                      st.get("cheap_faced", 0)),
        (st.get("medium_called", 0),   st.get("medium_faced", 0)),
        (exp_c,                        st.get("expensive_faced", 0)),
        (grad_y,                       grad_n),
    ]


def _softmax(scores):
    """scores: dict key->logit. Returns dict key->prob."""
    if not scores:
        return {}
    m = max(scores.values())
    exps = {k: math.exp(v - m) for k, v in scores.items()}
    z = sum(exps.values()) or 1.0
    return {k: v / z for k, v in exps.items()}


def classify_implementation(st):
    """MAP posterior over implementation archetypes (framework Layer 1).
    Returns a dict summing to 1. Sample-size handling is implicit: signals with
    n=0 contribute nothing, so a fresh opponent's posterior ≈ the prior."""
    sig = _signals(st)
    logits = {}
    for k, profile in _IMPL_PROFILE.items():
        s = math.log(IMPLEMENTATION_PRIORS.get(k, _EPS) + _EPS)
        for (y, n), p, w in zip(sig, profile, _IMPL_SIGNAL_W):
            if n <= 0:
                continue
            pc = min(1 - _EPS, max(_EPS, p))
            s += w * (y * math.log(pc) + (n - y) * math.log(1 - pc))
        logits[k] = s
    post = _softmax(logits)

    # ---- folding_bot evidence guard (req 3 & 4) -------------------------------
    # A TRUE folding_bot overfolds across ALL sizes — it folds even cheap bets.
    # A tight/TAG or pot-odds bot folds a lot OVERALL but still calls cheap bets,
    # which raw fold_vs_bet alone can't distinguish. So before letting
    # folding_bot win with high confidence, require bucket evidence of overfold
    # across every size. Specifically: if the opponent calls cheap bets at a
    # non-trivial rate, it is NOT a pure folder — cap and redistribute its mass.
    cheap_f = st.get("cheap_faced", 0)
    med_f = st.get("medium_faced", 0)
    exp_f = st.get("expensive_faced", 0)
    cheap_r = st.get("cheap_called", 0) / cheap_f if cheap_f else None
    med_r = st.get("medium_called", 0) / med_f if med_f else None

    if post.get("folding_bot", 0) > 0:
        # Do we have enough bucket data to judge overfolding at all?
        have_bucket_evidence = (cheap_f + med_f + exp_f) >= 8
        # True overfold: low call rate at cheap AND medium (the sizes a folder
        # must still be folding to qualify). Unknown buckets don't count as folds.
        calls_cheap = (cheap_r is not None and cheap_r >= 0.25)
        calls_medium = (med_r is not None and med_r >= 0.30)
        confirms_overfold = (have_bucket_evidence
                             and (cheap_r is None or cheap_r < 0.20)
                             and (med_r is None or med_r < 0.20))

        if calls_cheap or calls_medium:
            # Opponent still calls cheap/medium bets -> not a pure folder. Strip
            # most of folding_bot's mass and redistribute to the price-sensitive
            # archetypes (monte_carlo / TAG) that explain "folds big, calls small".
            fb = post["folding_bot"]
            post["folding_bot"] = fb * 0.05
            freed = fb * 0.95
            # Weight redistribution by current relative support among the
            # price-sensitive types so we don't invent a winner arbitrarily.
            targets = ("monte_carlo", "simple_tag", "rule_shark")
            base = sum(post.get(t, 0.0) for t in targets) or _EPS
            for t in targets:
                post[t] = post.get(t, 0.0) + freed * (post.get(t, 0.0) / base)
        elif not confirms_overfold:
            # Not enough evidence to confirm true overfolding across sizes: keep
            # folding_bot as a candidate but cap its confidence so it can't hit
            # ~1.00 on raw fold rate alone (req 4).
            cap = 0.60
            if post["folding_bot"] > cap:
                excess = post["folding_bot"] - cap
                post["folding_bot"] = cap
                others = sum(v for k, v in post.items() if k != "folding_bot") or _EPS
                for k in post:
                    if k != "folding_bot":
                        post[k] += excess * (post[k] / others)

        z = sum(post.values()) or 1.0
        post = {k: v / z for k, v in post.items()}
    # ---------------------------------------------------------------------------

    # Fast override: an obvious perma-jam bot is identified after only a few
    # actions (framework's exception to the slow-build rule). Guard against
    # firing on tiny/noisy samples: need enough actions AND multiple jams.
    actions = st.get("actions", 0)
    allins = st.get("allins", 0)
    if actions >= 15 and allins >= 4 and allins / actions > 0.25:
        for k in post:
            post[k] *= 0.15
        post["perma_all_in"] = post.get("perma_all_in", 0) + 0.85
        z = sum(post.values()) or 1.0
        post = {k: v / z for k, v in post.items()}
    return post


# Behavior archetypes scored by distance to a target feature vector, then
# softmaxed. Features: [vpip, pfr, fold_vs_bet, call_vs_bet, aggression, allin].
_BEHAV_TARGET = {
    "nit":     (0.12, 0.08, 0.75, 0.15, 0.12, 0.02),
    "tag":     (0.26, 0.20, 0.55, 0.28, 0.30, 0.03),
    "station": (0.55, 0.12, 0.12, 0.80, 0.12, 0.02),
    "aggro":   (0.45, 0.35, 0.30, 0.25, 0.55, 0.08),
    "maniac":  (0.75, 0.55, 0.15, 0.25, 0.75, 0.35),
}
_BEHAV_TEMP = 0.05      # softmax temperature on squared distance


def _rate(st, num, den, default):
    d = st.get(den, 0)
    return st.get(num, 0) / d if d else default


def classify_behavior(st):
    """Posterior over poker behavior styles (framework Layer 2). Low sample is
    folded in afterwards as an 'unknown' mass."""
    vpip = _rate(st, "vpip_yes", "vpip_opp", 0.3)
    pfr = _rate(st, "pfr_yes", "pfr_opp", 0.15)
    foldb = _rate(st, "fold_vs_bet", "faced_bet", 0.5)
    callb = _rate(st, "call_vs_bet", "faced_bet", 0.3)
    aggr = (st.get("raises", 0) + st.get("allins", 0)) / st["actions"] \
        if st.get("actions", 0) else 0.2
    allin = _rate(st, "allins", "actions", 0.03)
    obs = (vpip, pfr, foldb, callb, aggr, allin)

    logits = {}
    for k, tgt in _BEHAV_TARGET.items():
        d2 = sum((o - t) ** 2 for o, t in zip(obs, tgt))
        logits[k] = -d2 / _BEHAV_TEMP
    post = _softmax(logits)

    # Blend toward 'unknown' when we have little data.
    n = st.get("actions", 0)
    known_w = min(1.0, n / 25.0)
    post = {k: v * known_w for k, v in post.items()}
    post["unknown"] = 1.0 - known_w
    z = sum(post.values()) or 1.0
    return {k: v / z for k, v in post.items()}


def get_relevant_sample_size(st):
    return st.get("actions", 0)


def compute_confidence(impl_post, behav_post, n=None):
    """Top-class probability of the implementation read, lightly rewarded when
    the behavior read also agrees. Crucially, damped by sample size: with little
    data the posterior can look sharp (a couple of folds 'looks like' a folder),
    so we scale confidence by min(1, n/40). This keeps the discovery regime safe
    — low n => low confidence => lambda 0 => pure SafeTAG."""
    if not impl_post:
        return 0.0
    c_impl = max(impl_post.values())
    b = {k: v for k, v in behav_post.items() if k != "unknown"}
    c_behav = max(b.values()) if b else 0.0
    raw = 0.8 * c_impl + 0.2 * c_behav
    if n is not None:
        raw *= min(1.0, n / 40.0)
    return raw


def exploit_weight(n, c):
    """λ = min(1, n/60) · max(0, (c-0.50)/0.30), clamped to [0,1].
    Framework's confidence-weighted exploit gate."""
    sample_term = min(1.0, n / 60.0)
    conf_term = max(0.0, (c - 0.50) / 0.30)
    return max(0.0, min(1.0, sample_term * conf_term))


def identify_main_villain(state):
    """Pick the opponent to model: the most-observed still-live opponent,
    tie-broken by largest stack (biggest threat). Returns a bot_id or None."""
    me = state.get("seat_to_act")
    best = None
    best_key = (-1, -1)
    for p in state.get("players") or []:
        try:
            if p["seat"] == me:
                continue
            if p.get("is_folded") or p.get("state") == "busted":
                continue
            bid = p["bot_id"]
        except (KeyError, TypeError):
            continue
        seen = PLAYER_STATS.get(bid, {}).get("actions", 0)
        key = (seen, p.get("stack", 0))
        if key > best_key:
            best_key = key
            best = bid
    return best


# Most recent read, exposed for V5 / debugging. Never consumed by V4 itself.
LAST_READ = {}


def compute_reads(state):
    """Build the full classification picture for the main villain and stash it
    in LAST_READ. Pure computation — does not influence the returned action."""
    vid = identify_main_villain(state)
    if vid is None:
        return None
    st = PLAYER_STATS.get(vid)
    if not st:
        return None
    impl = classify_implementation(st)
    behav = classify_behavior(st)
    n = get_relevant_sample_size(st)
    c = compute_confidence(impl, behav, n)
    lam = exploit_weight(n, c)
    read = {
        "villain": vid,
        "implementation": impl,
        "behavior": behav,
        "n": n,
        "confidence": c,
        "lambda": lam,
        "top_impl": max(impl, key=impl.get) if impl else None,
        "top_behav": max(behav, key=behav.get) if behav else None,
    }
    LAST_READ.clear()
    LAST_READ.update(read)
    return read


def decide(game_state: dict) -> dict:
    if isinstance(game_state, dict) and game_state.get("type") == "warmup":
        # Touch eval7 once so its first real call isn't paying import/JIT costs.
        if _HAVE_EVAL7:
            try:
                equity_vs_random(["As", "Kh"], ["2c", "7d", "9s"], 1,
                                 time_budget=0.05)
            except Exception:
                pass
        return {"action": "fold"}
    try:
        # V3: always observe first (collection only; never alters the action).
        # update_stats returns True only when new opponent actions were folded
        # into PLAYER_STATS — so we can skip the (more expensive) classifier when
        # nothing changed since the last decision.
        stats_changed = False
        try:
            stats_changed = update_stats(game_state)
        except Exception:
            stats_changed = False
        # V4: classify the main villain into LAST_READ only if stats moved.
        # Still collection-only — the action below does NOT use it yet (V5).
        # LAST_READ persists from the previous compute when unchanged.
        if stats_changed:
            try:
                compute_reads(game_state)
            except Exception:
                pass
        return _sanitize(_safetag(game_state), game_state)
    except Exception:
        return _emergency(game_state)
