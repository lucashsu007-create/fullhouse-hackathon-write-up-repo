"""
multi_barrel — Fullhouse test opponent (TARGETS PER-STREET-FRESH EQUITY LEAK).

Archetype: standard TAG preflop, but when it takes initiative postflop (c-bets
the flop) it KEEPS FIRING on later streets, value or air, especially on scare
cards. C-bet flop ~85%, double-barrel turn ~60%, triple-barrel river ~40% with
bluff-flavor sizing that mimics value sizing.

The leak it targets: a hero bot that recomputes equity FRESH each street vs
random with no memory of villain's prior aggression. Calls TPTK on the flop
(~0.62 vs random), holds on through a thin turn equity (~0.50), and either
correctly folds river (loses initiative) or thin-calls a polarised bluff. A
smart triple-barreller pushes the hero off marginal made hands because each
street is treated as a fresh problem.

This bot intentionally over-bluffs (no balanced solver here), so it isn't
unbeatable — the question is the magnitude of the hero's loss vs a competent
defender's loss in the same spots.

Determinism: spot-seeded RNG (hand_id + street + seat + hole + board +
action_log length) is used for ALL barrel decisions, so A and B see identical
actions in paired matches.

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

BOT_NAME = "MultiBarrel"
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
# Hand class
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
# Postflop strength via eval7 if available
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


def _hand_strength_bucket(hole, board):
    """Return 'monster' | 'strong' | 'medium' | 'weak' | 'air'."""
    if not _HAVE_EVAL7:
        # Heuristic fallback
        try:
            ranks = [c[0] for c in hole + board]
            counts = {}
            for r in ranks:
                counts[r] = counts.get(r, 0) + 1
            cv = sorted(counts.values(), reverse=True)
            if cv and cv[0] >= 3:           return "strong"
            if cv.count(2) >= 2:            return "strong"
            if cv and cv[0] == 2:           return "medium"
        except Exception:
            return "air"
        return "air"
    h = _eval7_cards(hole)
    b = _eval7_cards(board)
    if len(h) != 2 or len(b) < 3:
        return "air"
    try:
        rank = eval7.evaluate(h + b)
        ht = eval7.handtype(rank)
    except Exception:
        return "air"
    if ht in ("Full House", "Quads", "Straight Flush"):
        return "monster"
    if ht in ("Trips", "Straight", "Flush"):
        return "strong"
    if ht == "Two Pair":
        return "strong"
    if ht == "Pair":
        try:
            board_vals = sorted((_VAL.get(c[0], 0) for c in board), reverse=True)
            hole_vals = sorted((_VAL.get(c[0], 0) for c in hole), reverse=True)
            if not board_vals or not hole_vals:
                return "weak"
            top_board = board_vals[0]
            if hole_vals[0] == hole_vals[1] and hole_vals[0] > top_board:
                return "medium"  # overpair
            if hole_vals[0] == top_board and hole_vals[1] >= 11:
                return "medium"  # top-pair-good-kicker
            return "weak"
        except Exception:
            return "weak"
    return "air"


# ---------------------------------------------------------------------------
# Board-texture helpers
# ---------------------------------------------------------------------------

def _board_overcards_since_flop(board):
    if len(board) < 4:
        return 0
    flop_vals = [_VAL.get(c[0], 0) for c in board[:3]]
    later_vals = [_VAL.get(c[0], 0) for c in board[3:]]
    flop_max = max(flop_vals) if flop_vals else 0
    return sum(1 for v in later_vals if v > flop_max and v >= 10)


def _is_scary_turn(board):
    if len(board) != 4:
        return False
    if _board_overcards_since_flop(board) > 0:
        return True
    suits = [c[1] for c in board]
    if any(suits.count(s) >= 3 for s in set(suits)):
        return True
    vals = sorted(set(_VAL.get(c[0], 0) for c in board))
    for i in range(len(vals) - 1):
        if vals[i + 1] - vals[i] == 1:
            return True
    return False


def _is_scary_river(board):
    if len(board) != 5:
        return False
    river_val = _VAL.get(board[4][0], 0)
    river_suit = board[4][1]
    earlier_suits = [c[1] for c in board[:4]]
    earlier_vals = [_VAL.get(c[0], 0) for c in board[:4]]
    if river_val > max(earlier_vals):
        return True
    if earlier_suits.count(river_suit) >= 2:
        return True
    return False


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def _facing_raise(state):
    for a in state.get("action_log", []) or []:
        if a.get("action") in ("raise", "all_in"):
            return True
    return False


def _i_was_aggressor_last(state):
    """Was our most recent action in the hand a raise/all_in?"""
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
# Strategy — the multi-street barreling pattern
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

    # ---- POSTFLOP ----
    strength = _hand_strength_bucket(hole, board)
    had_initiative = _i_was_aggressor_last(state)

    # Defending vs a bet
    if owed > 0:
        if strength in ("monster", "strong"):
            return {"action": "raise", "amount": _bet_to(state, 1.0)}
        if strength == "medium":
            if owed <= pot * 0.9:
                return {"action": "call"}
            return {"action": "fold"}
        if strength == "weak":
            if owed <= pot * 0.5:
                return {"action": "call"}
            return {"action": "fold"}
        if owed <= pot * 0.25 and pos >= 0.6:
            return {"action": "call"}
        return {"action": "fold"}

    # No bet to call
    if strength in ("monster", "strong"):
        return {"action": "raise", "amount": _bet_to(state, 0.75)}

    if not had_initiative:
        if strength == "medium":
            return {"action": "raise", "amount": _bet_to(state, 0.6)}
        return {"action": "check"} if can_check else {"action": "fold"}

    # We have initiative — barrel based on street + scare cards
    n_board = len(board)

    if n_board == 3:
        # FLOP: c-bet 85% regardless of equity
        if rng.random() < 0.85:
            return {"action": "raise", "amount": _bet_to(state, 0.65)}
        return {"action": "check"} if can_check else {"action": "fold"}

    if n_board == 4:
        # TURN: double-barrel
        if strength == "medium":
            return {"action": "raise", "amount": _bet_to(state, 0.7)}
        bluff_freq = 0.60 if _is_scary_turn(board) else 0.40
        if rng.random() < bluff_freq:
            return {"action": "raise", "amount": _bet_to(state, 0.7)}
        return {"action": "check"} if can_check else {"action": "fold"}

    if n_board == 5:
        # RIVER: triple-barrel
        if strength == "medium":
            if rng.random() < 0.5:
                return {"action": "raise", "amount": _bet_to(state, 0.6)}
            return {"action": "check"} if can_check else {"action": "fold"}
        bluff_freq = 0.40 if _is_scary_river(board) else 0.20
        if rng.random() < bluff_freq:
            return {"action": "raise", "amount": _bet_to(state, 0.85)}
        return {"action": "check"} if can_check else {"action": "fold"}

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
