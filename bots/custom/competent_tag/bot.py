"""
competent_tag — Fullhouse test opponent (REFERENCE FOR STRATEGIC EDGE).

Archetype: a hackathon entry that did the FIRST 80% of things well but skipped
the polish. Position-aware chart preflop opens, eval7 Monte-Carlo equity vs
random opponents postflop (same assumption as v4.1/v6), pot-sized bets, no
exploit modules, no opponent modeling, no bluffing beyond rare in-position
steals. Think "v6/v7-level baseline anyone could ship in a long weekend."

THESIS: if our hero beats this by ~5-10 bb/100 paired, our cumulative gains are
real strategy. If it's near zero, those gains were mostly "punish weak bots".

Determinism: spot-seeded RNG (hand_id + street + seat + hole + board +
action_log length) is used for BOTH the MC equity samples and the 3-bet mixing
decision, so A and B see identical actions in paired matches.

Legal/safe: stdlib + optional eval7 only, no file/network, always returns a
valid action.
"""

import random
import zlib

try:
    import eval7
    _HAVE_EVAL7 = True
except Exception:
    eval7 = None
    _HAVE_EVAL7 = False

BOT_NAME = "CompetentTAG"
BOT_AVATAR = "robot_1"

_RANKS = "23456789TJQKA"
_SUITS = "shdc"
_VAL = {r: i for i, r in enumerate(_RANKS, start=2)}

if _HAVE_EVAL7:
    try:
        _FULL_DECK = [eval7.Card(r + s) for r in _RANKS for s in _SUITS]
        _CARD_STR = {c: str(c) for c in _FULL_DECK}
    except Exception:
        _HAVE_EVAL7 = False
        _FULL_DECK = []
        _CARD_STR = {}
else:
    _FULL_DECK = []
    _CARD_STR = {}


# ---------------------------------------------------------------------------
# Spot-seeded RNG — same spot always resolves the same way
# ---------------------------------------------------------------------------

def _spot_rng(state):
    try:
        parts = [
            str(state.get("hand_id", "")),
            str(state.get("street", "")),
            str(state.get("seat_to_act", "")),
            "".join(state.get("your_cards", []) or []),
            "".join(state.get("community_cards", []) or []),
            str(len(state.get("action_log", []) or [])),
        ]
        seed = zlib.crc32("|".join(parts).encode("utf-8")) & 0xffffffff
    except Exception:
        seed = 0
    return random.Random(seed)


# ---------------------------------------------------------------------------
# Hand class — coarse strength buckets used by preflop chart logic
# ---------------------------------------------------------------------------

_TIER_ORDER = {"premium": 4, "strong": 3, "playable": 2, "speculative": 1, "trash": 0}


def _preflop_class(cards):
    try:
        r1, s1 = cards[0][0], cards[0][1]
        r2, s2 = cards[1][0], cards[1][1]
    except (IndexError, TypeError):
        return "trash"
    v1, v2 = _VAL.get(r1, 0), _VAL.get(r2, 0)
    hi, lo = max(v1, v2), min(v1, v2)
    pair = (r1 == r2)
    suited = (s1 == s2)
    if pair and hi >= 12:                              return "premium"   # QQ+
    if hi == 14 and lo == 13:                          return "premium"   # AK
    if pair and hi >= 10:                              return "strong"    # TT-JJ
    if hi == 14 and lo == 12:                          return "strong"    # AQ
    if hi == 14 and lo == 11 and suited:               return "strong"    # AJs
    if hi == 13 and lo == 12 and suited:               return "strong"    # KQs
    if pair and hi >= 7:                               return "playable"  # 77-99
    if hi == 14 and lo == 11:                          return "playable"  # AJo
    if hi == 14 and lo == 10 and suited:               return "playable"  # ATs
    if hi == 13 and lo == 12:                          return "playable"  # KQo
    if hi == 13 and lo == 11 and suited:               return "playable"  # KJs
    if hi == 12 and lo == 11 and suited:               return "playable"  # QJs
    if pair:                                           return "speculative"
    if suited and hi == 14:                            return "speculative"
    if suited and (hi - lo) <= 2 and lo >= 5:          return "speculative"
    return "trash"


def _position(state):
    """Rough 0..1: 0=early, 1=late."""
    try:
        n = len(state.get("players") or [])
        seat = state.get("seat_to_act", 0)
        return seat / max(n - 1, 1)
    except Exception:
        return 0.5


def _should_open(cls, pos):
    if pos >= 0.7:   return _TIER_ORDER[cls] >= _TIER_ORDER["speculative"]
    if pos >= 0.45:  return _TIER_ORDER[cls] >= _TIER_ORDER["playable"]
    return _TIER_ORDER[cls] >= _TIER_ORDER["strong"]


# ---------------------------------------------------------------------------
# Equity vs random — eval7 MC, RNG seeded from the spot
# ---------------------------------------------------------------------------

def _eval7_cards(strs):
    if not _HAVE_EVAL7:
        return []
    out = []
    for s in strs:
        if not s or len(s) < 2:
            continue
        try:
            out.append(eval7.Card(s))
        except Exception:
            continue
    return out


def equity_vs_random(hole, board, n_opp, rng, n_iters=300):
    if not _HAVE_EVAL7 or n_opp < 1:
        return 0.5
    hole_c = _eval7_cards(hole)
    board_c = _eval7_cards(board)
    if len(hole_c) != 2:
        return 0.5
    known = set(_CARD_STR.get(c, str(c)) for c in hole_c + board_c)
    deck = [c for c in _FULL_DECK if _CARD_STR[c] not in known]
    needed_board = 5 - len(board_c)
    sample_size = 2 * n_opp + needed_board
    if len(deck) < sample_size:
        return 0.5
    wins = ties = 0
    for _ in range(n_iters):
        sample = rng.sample(deck, sample_size)
        opp_holes = [sample[2 * i:2 * i + 2] for i in range(n_opp)]
        sim_board = board_c + sample[2 * n_opp:]
        hero = eval7.evaluate(hole_c + sim_board)
        best_opp = max(eval7.evaluate(h + sim_board) for h in opp_holes)
        if hero > best_opp:    wins += 1
        elif hero == best_opp: ties += 1
    return (wins + 0.5 * ties) / max(n_iters, 1)


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def _n_live_opponents(state):
    try:
        ps = state.get("players") or []
        me = state.get("seat_to_act")
        live = 0
        for p in ps:
            if p.get("seat") == me:
                continue
            if p.get("is_folded") or p.get("state") == "busted":
                continue
            live += 1
        return max(1, live)
    except Exception:
        return 1


def _facing_raise(state):
    for a in state.get("action_log", []) or []:
        if a.get("action") in ("raise", "all_in"):
            return True
    return False


def _bet_to(state, pot_fraction):
    pot = state.get("pot", 0)
    cur = state.get("current_bet", 0)
    my = state.get("your_bet_this_street", 0)
    stack = state.get("your_stack", 0)
    min_to = state.get("min_raise_to", 0)
    target = cur + int(pot * pot_fraction)
    cap = my + stack
    return min(max(target, min_to), cap)


def _required_equity(state):
    owed = state.get("amount_owed", 0)
    pot = state.get("pot", 0)
    if owed <= 0:
        return 0.0
    return owed / (pot + owed)


# ---------------------------------------------------------------------------
# Output safety
# ---------------------------------------------------------------------------

def _sanitize(action, state):
    if not isinstance(action, dict) or "action" not in action:
        return {"action": "fold"}
    act = action["action"]
    owed = state.get("amount_owed", 0)
    can_check = state.get("can_check", False)
    if act == "check":
        if not can_check or owed > 0:
            return {"action": "fold"}
        return {"action": "check"}
    if act == "fold":
        return {"action": "fold"}
    if act == "call":
        if owed <= 0 and can_check:
            return {"action": "check"}
        return {"action": "call"}
    if act in ("raise", "all_in"):
        amt = int(action.get("amount", 0))
        min_to = state.get("min_raise_to", 0)
        my = state.get("your_bet_this_street", 0)
        stack = state.get("your_stack", 0)
        cap = my + stack
        amt = min(max(amt, min_to), cap)
        if amt <= state.get("current_bet", 0):
            if owed > 0:
                return {"action": "call"}
            return {"action": "check"} if can_check else {"action": "fold"}
        if amt >= cap:
            return {"action": "all_in", "amount": amt}
        return {"action": "raise", "amount": amt}
    return {"action": "fold"}


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------

def _play(state):
    street = state.get("street", "preflop")
    hole = state.get("your_cards") or []
    board = state.get("community_cards") or []
    can_check = state.get("can_check", False)
    owed = state.get("amount_owed", 0)
    pot = state.get("pot", 0)
    pos = _position(state)
    facing = _facing_raise(state)
    rng = _spot_rng(state)

    # ---- PREFLOP ----
    if street == "preflop":
        cls = _preflop_class(hole)
        if not facing:
            if _should_open(cls, pos):
                return {"action": "raise", "amount": _bet_to(state, 1.0)}
            return {"action": "check"} if can_check else {"action": "fold"}

        # Facing a raise
        if cls == "premium":
            return {"action": "raise", "amount": _bet_to(state, 2.5)}
        if cls == "strong":
            if rng.random() < 0.25:
                return {"action": "raise", "amount": _bet_to(state, 2.5)}
            return {"action": "call"}
        if cls == "playable" and pos >= 0.5 and owed <= max(pot, 60):
            return {"action": "call"}
        if cls == "speculative" and pos >= 0.7 and owed <= max(pot * 0.6, 50):
            return {"action": "call"}
        return {"action": "fold"}

    # ---- POSTFLOP ----
    n_opp = _n_live_opponents(state)
    eq = equity_vs_random(hole, board, n_opp, rng, n_iters=300)

    if owed > 0:
        req = _required_equity(state)
        if eq >= 0.68:
            return {"action": "raise", "amount": _bet_to(state, 1.0)}
        if eq >= 0.55:
            return {"action": "call"}
        if eq >= req + 0.08:
            return {"action": "call"}
        return {"action": "fold"}

    # No bet to call
    if eq >= 0.62:
        return {"action": "raise", "amount": _bet_to(state, 0.75)}
    if eq >= 0.45 and pos >= 0.6:
        return {"action": "raise", "amount": _bet_to(state, 0.5)}
    return {"action": "check"} if can_check else {"action": "fold"}


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

def decide(game_state):
    if isinstance(game_state, dict) and game_state.get("type") == "warmup":
        if _HAVE_EVAL7:
            try:
                equity_vs_random(["As", "Kh"], ["2c", "7d", "9s"], 1,
                                 random.Random(0), n_iters=60)
            except Exception:
                pass
        return {"action": "fold"}
    try:
        return _sanitize(_play(game_state), game_state)
    except Exception:
        return {"action": "fold"}
