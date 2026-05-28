"""
polar_3bettor — Fullhouse test opponent (TARGETS LINEAR-RANGE ASSUMPTION).

Archetype: a TAG that 3-bets POLARIZED. When facing an open it mixes top value
(AA/KK/QQ/AKs/AKo) with explicit bluff hands (A5s/A4s/A3s/K9s/Q9s/T9s/76s/65s)
and almost nothing in between. The point: most hero bots assume a 3-bettor's
range is LINEAR (top ~4% of hands). That assumption fails here:
  - On low/blank boards we have a lot of air, so a thinking opponent should
    catch our c-bet bluffs
  - On A-high boards we have nearly all aces, so a thinking opponent should
    fold bluff-catchers

Postflop is intentionally simple: value hands bet for value, bluff hands c-bet
the flop then give up if not improved. Not optimal — a faithful representation
of the polarized strategy.

Determinism: spot-seeded RNG (hand_id + street + seat + hole + board +
action_log length) is used for the value-vs-call mixing so A and B see
identical actions in paired matches.

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

BOT_NAME = "Polar3Bettor"
BOT_AVATAR = "robot_1"

_RANKS = "23456789TJQKA"
_SUITS = "shdc"
_VAL = {r: i for i, r in enumerate(_RANKS, start=2)}


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
# Hand identity — canonical class keys for range membership
# ---------------------------------------------------------------------------

def _hand_key(cards):
    """Return a canonical key like 'AKs', 'AKo', 'QQ', '76s'."""
    try:
        r1, s1 = cards[0][0], cards[0][1]
        r2, s2 = cards[1][0], cards[1][1]
    except (IndexError, TypeError):
        return ""
    v1, v2 = _VAL.get(r1, 0), _VAL.get(r2, 0)
    if v1 == v2:
        return r1 + r2
    if v1 < v2:
        r1, r2 = r2, r1
    suited = (s1 == s2)
    return f"{r1}{r2}{'s' if suited else 'o'}"


_VALUE_3BET = {"AA", "KK", "QQ", "AKs", "AKo"}
_BLUFF_3BET = {"A5s", "A4s", "A3s", "K9s", "Q9s", "T9s", "76s", "65s"}
_VALUE_4BET = {"AA", "KK"}


# ---------------------------------------------------------------------------
# Open range — standard TAG
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
    if pair and hi >= 12:                              return "premium"
    if hi == 14 and lo == 13:                          return "premium"
    if pair and hi >= 10:                              return "strong"
    if hi == 14 and lo == 12:                          return "strong"
    if hi == 14 and lo == 11 and suited:               return "strong"
    if hi == 13 and lo == 12 and suited:               return "strong"
    if pair and hi >= 7:                               return "playable"
    if hi == 14 and lo == 11:                          return "playable"
    if hi == 14 and lo == 10 and suited:               return "playable"
    if hi == 13 and lo == 12:                          return "playable"
    if hi == 13 and lo == 11 and suited:               return "playable"
    if hi == 12 and lo == 11 and suited:               return "playable"
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


# ---------------------------------------------------------------------------
# Postflop strength — via eval7 if available, fallback to heuristic
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


def _is_strong_made(hole, board):
    """True if at least two-pair or strong top-pair / overpair."""
    if not _HAVE_EVAL7:
        # Heuristic fallback
        try:
            ranks = [c[0] for c in hole + board]
            counts = {}
            for r in ranks:
                counts[r] = counts.get(r, 0) + 1
            cv = sorted(counts.values(), reverse=True)
            return (cv and cv[0] >= 3) or cv.count(2) >= 2
        except Exception:
            return False
    h = _eval7_cards(hole)
    b = _eval7_cards(board)
    if len(h) != 2 or len(b) < 3:
        return False
    try:
        rank = eval7.evaluate(h + b)
        ht = eval7.handtype(rank)
        if ht in ("Two Pair", "Trips", "Straight", "Flush", "Full House",
                  "Quads", "Straight Flush"):
            return True
        if ht == "Pair":
            board_vals = sorted((_VAL.get(c[0], 0) for c in board), reverse=True)
            hole_vals = sorted((_VAL.get(c[0], 0) for c in hole), reverse=True)
            if not board_vals or not hole_vals:
                return False
            top_board = board_vals[0]
            if hole_vals[0] == hole_vals[1] and hole_vals[0] > top_board:
                return True   # overpair
            if hole_vals[0] == top_board and hole_vals[1] >= 12:
                return True   # TPTK
            return False
    except Exception:
        return False
    return False


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

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
# Strategy — polarized 3-bet pattern
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
    key = _hand_key(hole)
    cls = _preflop_class(hole)
    rng = _spot_rng(state)

    # ---- PREFLOP ----
    if street == "preflop":
        raises = sum(1 for a in (state.get("action_log") or [])
                     if a.get("action") in ("raise", "all_in"))

        if not facing:
            if _should_open(cls, pos):
                return {"action": "raise", "amount": _bet_to(state, 1.0)}
            return {"action": "check"} if can_check else {"action": "fold"}

        if raises == 1:
            # Facing a single open — the polarized 3-bet spot
            if key in _VALUE_3BET:
                return {"action": "raise", "amount": _bet_to(state, 3.0)}
            if key in _BLUFF_3BET and pos >= 0.5:
                return {"action": "raise", "amount": _bet_to(state, 3.0)}
            # Calls with reasonable hands
            if cls in ("premium", "strong", "playable"):
                return {"action": "call"}
            if cls == "speculative" and pos >= 0.6 and owed <= pot * 0.5:
                return {"action": "call"}
            return {"action": "fold"}

        # Facing a 3-bet (or worse): tighten up
        if key in _VALUE_4BET:
            return {"action": "raise", "amount": _bet_to(state, 2.2)}
        if cls == "premium":
            return {"action": "call"}
        if cls == "strong" and owed <= pot * 0.5:
            return {"action": "call"}
        return {"action": "fold"}

    # ---- POSTFLOP ----
    was_bluff_3bet = key in _BLUFF_3BET
    was_value_3bet = key in _VALUE_3BET or key in _VALUE_4BET
    strong_made = _is_strong_made(hole, board)

    if owed > 0:
        # Facing a bet
        if strong_made:
            if rng.random() < 0.55:
                return {"action": "raise", "amount": _bet_to(state, 1.0)}
            return {"action": "call"}
        if was_bluff_3bet:
            # The polarized signature — bluffs miss and fold
            return {"action": "fold"}
        if cls in ("premium", "strong") and owed <= pot * 0.5:
            return {"action": "call"}
        if owed <= pot * 0.3 and cls != "trash":
            return {"action": "call"}
        return {"action": "fold"}

    # No bet to call — should we bet?
    if strong_made:
        return {"action": "raise", "amount": _bet_to(state, 0.8)}
    if was_bluff_3bet and len(board) == 3:
        # Bluff c-bet on flop, give up turn/river
        return {"action": "raise", "amount": _bet_to(state, 0.6)}
    if was_value_3bet and len(board) == 3:
        return {"action": "raise", "amount": _bet_to(state, 0.7)}
    return {"action": "check"} if can_check else {"action": "fold"}


def decide(game_state):
    if isinstance(game_state, dict) and game_state.get("type") == "warmup":
        if _HAVE_EVAL7:
            try:
                eval7.evaluate([eval7.Card("As"), eval7.Card("Kh"),
                                eval7.Card("2c"), eval7.Card("7d"), eval7.Card("9s")])
            except Exception:
                pass
        return {"action": "fold"}
    try:
        return _sanitize(_play(game_state), game_state)
    except Exception:
        return {"action": "fold"}
