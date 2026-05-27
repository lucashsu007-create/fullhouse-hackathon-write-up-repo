"""
================================================================================
FROZEN VERSION: v4.1 — classifier-retuned snapshot (strategy UNCHANGED).
Frozen from safetag_eq_bot.py after the implementation-classifier retune.

What this freeze contains, relative to plain V4:
  * fold_vs_bet signal down-weighted (1.0 -> 0.4) so raw fold rate no longer
    dominates the implementation posterior;
  * an added bucket-GRADIENT signal (calls-cheap-vs-expensive) that separates
    "folds because the price was bad" (monte_carlo / TAG) from "folds at every
    size" (a true folder);
  * a post-softmax folding_bot evidence guard: folding_bot cannot reach high
    confidence unless bucket data confirms overfolding across ALL sizes (low
    cheap AND medium call rates); if the opponent still calls cheap/medium bets,
    most folding_bot mass is redistributed to the price-sensitive archetypes.

IMPORTANT — interpretation of the `folding_bot` label in this version:
  `folding_bot` is now an OBSERVED OVERFOLDING BEHAVIOR, not necessarily an
  implementation identity. A high-confidence `folding_bot` read means only that
  the opponent, AS OBSERVED, folds to bets across all price tiers at this table
  — it does NOT assert the opponent was *implemented* as a dedicated fold-bot.
  A tight/TAG bot with a narrow preflop range whiffs most flops and therefore
  overfolds in aggregate, so it can legitimately read `folding_bot` here even
  though its design is "tight-aggressive". Downstream consumers (V5 exploit
  layer) should treat `folding_bot` as the signal "this opponent is currently
  overfolding -> applying fold pressure (steal / c-bet / barrel) is +EV",
  rather than as a claim about the opponent's internal algorithm. (See the
  6-max probe results: simple_tag and balanced_tag read folding_bot because
  they genuinely overfold; trap_tag and mc_pot_odds do NOT, because they keep
  calling/raising and so are pulled out of the folder class.)

NO STRATEGY CHANGE: decide(), _safetag(), action selection, bet sizing, the
equity engine, and SafeTAG logic are byte-for-byte identical to the working bot.
Only the classifier OUTPUTS differ. This module remains collection-only at V4 —
LAST_READ is populated but does not influence the returned action.
================================================================================

SafeTAG + Equity + Opponent Stats — Fullhouse Hackathon.

Build order coverage:
  V0  legal skeleton   : never crash, always legal, fold fallback
  V1  SafeTAG          : TAG preflop ranges, pot odds, value-heavy, low bluff
  V2  equity engine    : eval7 Monte Carlo (river-exact path) with a hard time
                         cap; graceful fallback to the V1 heuristic if eval7 is
                         unavailable. Drives postflop value/call decisions and
                         the preflop all-in filter.
  V3  opponent stats   : RAM-only PLAYER_STATS, rebuilt from the public action
                         log every decision. Reconstructs street, pot, and the
                         price each opponent faced -> VPIP/PFR, fold/call/raise
                         vs bet, price buckets (cheap/medium/expensive), street
                         aggression, all-in rate. COLLECTION ONLY — it does not
                         change decisions yet (that is V4/V5). The baseline EV is
                         therefore unchanged and the A/B harness can measure the
                         exploit layer cleanly when it lands.

stdlib + eval7 only. No threads/signals: the equity cap is a perf_counter loop.
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

BOT_NAME = "SafeTAG+EQ"
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

    # ---- PREFLOP ----
    if street == "preflop":
        cls = _preflop_class(hole)
        facing = _facing_raise(state)
        owed = state.get("amount_owed", 0)
        stack = state.get("your_stack", 0)

        # Facing an all-in (or a bet that would commit a big share of stack):
        # use an equity filter instead of the range chart.
        committing = owed >= 0.6 * (stack + state.get("your_bet_this_street", 0))
        if facing and committing and owed > 0:
            eq = equity_vs_random(hole, board, n_opp, rng=rng)
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
    eq = equity_vs_random(hole, board, n_opp, rng=rng)
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
    if eq >= 0.38:
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
