"""
overbet_polar — Fullhouse test opponent (PROBES FIXED-THRESHOLD LEAK, high end).

Archetype: normal lines until the river, then bets BIG (1.3-2.0x pot) with a
POLARIZED range — the nuts or air, nothing medium. Standard sizing pre-river.

The leak it targets: a hero with FIXED equity-bucket call thresholds that don't
scale with bet size. Facing a 1.5x-pot overbet, a hero needs ~60% equity to
call profitably, but a fixed-threshold hero may still call on its 0.55 bucket —
paying off the nut half of the range. Because the range is polarized (also
contains air), the hero can't just always fold either: that surrenders the pot
to the bluffs. The correct response is a specific bluff-catch frequency, which
a fixed-threshold hero won't find.

Pre-river it plays a reasonable TAG so it reaches rivers with a real (if
simplified) range, making the overbet credible rather than spew.

Determinism: spot-seeded RNG so paired A/B tests stay deterministic.
Legal/safe: stdlib + optional eval7 only.
"""

import random
import zlib

try:
    import eval7
    _HAVE_EVAL7 = True
except Exception:
    eval7 = None
    _HAVE_EVAL7 = False

BOT_NAME = "OverbetPolar"
BOT_AVATAR = "robot_1"

_RANKS = "23456789TJQKA"
_VAL = {r: i for i, r in enumerate(_RANKS, start=2)}


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
    if pair and hi >= 12:                              return "premium"
    if hi == 14 and lo == 13:                          return "premium"
    if pair and hi >= 10:                              return "strong"
    if hi == 14 and lo == 12:                          return "strong"
    if hi == 14 and lo == 11 and suited:               return "strong"
    if hi == 13 and lo == 12 and suited:               return "strong"
    if pair and hi >= 7:                               return "playable"
    if hi == 14 and lo == 11:                          return "playable"
    if hi == 13 and lo == 12:                          return "playable"
    if hi == 13 and lo == 11 and suited:               return "playable"
    if pair:                                           return "speculative"
    if suited and hi == 14:                            return "speculative"
    if suited and (hi - lo) <= 2 and lo >= 5:          return "speculative"
    return "trash"


def _position(state):
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


def _strength(hole, board):
    """nuts / strong / medium / weak_pair / draw / air."""
    if len(board) < 3:
        return "preflop"
    if not _HAVE_EVAL7:
        try:
            ranks = [c[0] for c in hole + board]
            counts = {}
            for r in ranks:
                counts[r] = counts.get(r, 0) + 1
            cv = sorted(counts.values(), reverse=True)
            if cv and cv[0] >= 3:                return "strong"
            if cv.count(2) >= 2:                 return "strong"
            if cv and cv[0] == 2:                return "medium"
        except Exception:
            return "air"
        return "air"
    h = _eval7_cards(hole)
    b = _eval7_cards(board)
    if len(h) != 2 or len(b) < 3:
        return "air"
    try:
        ht = eval7.handtype(eval7.evaluate(h + b))
    except Exception:
        return "air"
    if ht in ("Full House", "Quads", "Straight Flush"): return "nuts"
    if ht in ("Trips", "Straight", "Flush"):            return "strong"
    # Two pair is deliberately MEDIUM for this bot: it is the top of the
    # check-back range, the "gap" between the overbet-value hands (sets+) and
    # the overbet-bluffs (air). That gap is what makes the river range polar.
    if ht == "Two Pair":                                return "medium"
    if ht == "Pair":
        board_vals = sorted((_VAL.get(c[0], 0) for c in board), reverse=True)
        hole_vals = sorted((_VAL.get(c[0], 0) for c in hole), reverse=True)
        if board_vals and hole_vals:
            top = board_vals[0]
            if hole_vals[0] == hole_vals[1] and hole_vals[0] > top:
                return "medium"
            if hole_vals[0] == top and hole_vals[1] >= 12:
                return "medium"
            return "weak_pair"
    # draw?
    try:
        suit_counts = {}
        for c in hole + board:
            suit_counts[c[1]] = suit_counts.get(c[1], 0) + 1
        if max(suit_counts.values(), default=0) == 4:
            return "draw"
    except Exception:
        pass
    return "air"


def _facing_raise(state):
    for a in state.get("action_log", []) or []:
        if a.get("action") in ("raise", "all_in"):
            return True
    return False


def _i_was_aggressor_last(state):
    me = state.get("seat_to_act")
    last_act = None
    for a in state.get("action_log", []) or []:
        if a.get("seat") == me:
            last_act = a.get("action")
    return last_act in ("raise", "all_in")


def _bet_to(state, pot_fraction):
    pot = state.get("pot", 0)
    cur = state.get("current_bet", 0)
    my = state.get("your_bet_this_street", 0)
    stack = state.get("your_stack", 0)
    min_to = state.get("min_raise_to", 0)
    target = cur + int(pot * pot_fraction)
    cap = my + stack
    return min(max(target, min_to), cap)


def _sanitize(action, state):
    if not isinstance(action, dict) or "action" not in action:
        return {"action": "fold"}
    act = action["action"]
    owed = state.get("amount_owed", 0)
    can_check = state.get("can_check", False)
    if act == "check":
        return {"action": "check"} if can_check and owed <= 0 else {"action": "fold"}
    if act == "fold":
        return {"action": "fold"}
    if act == "call":
        return {"action": "check"} if (owed <= 0 and can_check) else {"action": "call"}
    if act in ("raise", "all_in"):
        amt = int(action.get("amount", 0))
        min_to = state.get("min_raise_to", 0)
        my = state.get("your_bet_this_street", 0)
        stack = state.get("your_stack", 0)
        cap = my + stack
        amt = min(max(amt, min_to), cap)
        if amt <= state.get("current_bet", 0):
            return {"action": "call"} if owed > 0 else (
                {"action": "check"} if can_check else {"action": "fold"})
        if amt >= cap:
            return {"action": "all_in", "amount": amt}
        return {"action": "raise", "amount": amt}
    return {"action": "fold"}


def _play(state):
    street = state.get("street", "preflop")
    hole = state.get("your_cards") or []
    board = state.get("community_cards") or []
    can_check = state.get("can_check", False)
    owed = state.get("amount_owed", 0)
    pot = state.get("pot", 0)
    pos = _position(state)
    facing = _facing_raise(state)
    cls = _preflop_class(hole)
    rng = _spot_rng(state)

    # ---- PREFLOP — standard TAG ----
    if street == "preflop":
        if not facing:
            if _should_open(cls, pos):
                return {"action": "raise", "amount": _bet_to(state, 1.0)}
            return {"action": "check"} if can_check else {"action": "fold"}
        if cls == "premium":
            return {"action": "raise", "amount": _bet_to(state, 2.5)}
        if cls == "strong":
            return {"action": "call"}
        if cls == "playable" and pos >= 0.5 and owed <= pot * 0.5:
            return {"action": "call"}
        return {"action": "fold"}

    strength = _strength(hole, board)
    had_init = _i_was_aggressor_last(state)
    n_board = len(board)
    is_river = (n_board == 5)

    # ---- FACING A BET ----
    if owed > 0:
        if strength in ("nuts", "strong"):
            return {"action": "raise", "amount": _bet_to(state, 1.0)}
        if strength == "medium":
            return {"action": "call"} if owed <= pot * 0.8 else {"action": "fold"}
        if strength == "weak_pair":
            return {"action": "call"} if owed <= pot * 0.4 else {"action": "fold"}
        if strength == "draw":
            return {"action": "call"} if owed <= pot * 0.45 else {"action": "fold"}
        return {"action": "fold"}

    # ---- NO BET TO CALL ----

    # THE RIVER OVERBET — the probe
    if is_river and can_check:
        # Polarized: overbet the top of the range (nutted made hands = nuts OR
        # strong: sets/straights/flushes/full+) and air; everything in the
        # middle (one pair, two pair) checks back. That middle gap is what
        # makes the range polar and forces the hero into a true bluff-catch.
        if strength in ("nuts", "strong"):
            mult = 1.3 + 0.7 * rng.random()        # 1.3x .. 2.0x pot
            return {"action": "raise", "amount": _bet_to(state, mult)}
        if strength == "air":
            # bluff overbet ~45% of air rivers
            if rng.random() < 0.45:
                mult = 1.3 + 0.7 * rng.random()
                return {"action": "raise", "amount": _bet_to(state, mult)}
            return {"action": "check"}
        # medium / weak_pair: check (the polar gap — no medium-strength bets)
        return {"action": "check"}

    # Pre-river betting — standard sizing
    if strength in ("nuts", "strong"):
        return {"action": "raise", "amount": _bet_to(state, 0.7)}
    if strength == "medium":
        if can_check and rng.random() < 0.4:
            return {"action": "check"}
        return {"action": "raise", "amount": _bet_to(state, 0.5)}
    if strength == "draw":
        if rng.random() < 0.5:
            return {"action": "raise", "amount": _bet_to(state, 0.6)}
        return {"action": "check"} if can_check else {"action": "fold"}

    # Air with initiative — standard c-bet flop/turn
    if had_init and n_board == 3 and rng.random() < 0.6:
        return {"action": "raise", "amount": _bet_to(state, 0.55)}
    if had_init and n_board == 4 and rng.random() < 0.4:
        return {"action": "raise", "amount": _bet_to(state, 0.6)}
    return {"action": "check"} if can_check else {"action": "fold"}


def decide(game_state):
    if isinstance(game_state, dict) and game_state.get("type") == "warmup":
        return {"action": "fold"}
    try:
        return _sanitize(_play(game_state), game_state)
    except Exception:
        return {"action": "fold"}
