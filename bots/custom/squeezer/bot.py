"""
squeezer — Fullhouse test opponent (MULTIWAY 3-BET AGGRESSOR).

Archetype: a squeeze specialist. Plays tight-ish heads-up, but when there's an
open AND at least one caller in front of it, it 3-bets ("squeezes") LARGE and
WIDE — exploiting the dead money and the capped ranges of the limp/callers.
Squeeze range is value + bluffs (polarized), sized bigger than a normal 3-bet
because there are more callers' chips to attack.

The leak it targets: a hero that defends its open the same way regardless of
whether players have flatted behind. When the hero opens, someone calls, and
then squeezer blasts a big 3-bet, the hero is now in a bloated multiway-turned-
heads-up pot out of position with a range built for a single caller. A hero
that doesn't tighten its continue range vs squeezes over-defends and bleeds.

When NO squeeze spot exists (heads-up to it, or first in), it plays a
straightforward TAG so it isn't just spew.

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

BOT_NAME = "Squeezer"
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


def _hand_key(cards):
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
    return f"{r1}{r2}{'s' if s1 == s2 else 'o'}"


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


# Squeeze ranges (polarized): value + bluffs, gap in between
_SQUEEZE_VALUE = {"AA", "KK", "QQ", "JJ", "AKs", "AKo", "AQs"}
_SQUEEZE_BLUFF = {"A5s", "A4s", "A3s", "KJs", "QTs", "JTs", "T9s", "98s", "87s"}


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


def _made_strength(hole, board):
    """strong / medium / weak_pair / draw / air."""
    if len(board) < 3:
        return "preflop"
    has_draw = False
    try:
        suit_counts = {}
        for c in hole + board:
            suit_counts[c[1]] = suit_counts.get(c[1], 0) + 1
        if max(suit_counts.values(), default=0) == 4:
            has_draw = True
    except Exception:
        pass
    if _HAVE_EVAL7:
        h = _eval7_cards(hole)
        b = _eval7_cards(board)
        if len(h) == 2 and len(b) >= 3:
            try:
                ht = eval7.handtype(eval7.evaluate(h + b))
                if ht in ("Two Pair", "Trips", "Straight", "Flush",
                          "Full House", "Quads", "Straight Flush"):
                    return "strong"
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
            except Exception:
                pass
    return "draw" if has_draw else "air"


def _facing_raise(state):
    for a in state.get("action_log", []) or []:
        if a.get("action") in ("raise", "all_in"):
            return True
    return False


def _squeeze_spot(state):
    """True if there is exactly one open-raise AND at least one caller in front
    of us this preflop (the classic squeeze configuration)."""
    log = state.get("action_log") or []
    n_raises = 0
    n_callers_after_raise = 0
    seen_raise = False
    for a in log:
        act = a.get("action")
        if act in ("raise", "all_in"):
            n_raises += 1
            seen_raise = True
            n_callers_after_raise = 0  # reset; callers must follow the latest raise
        elif act == "call" and seen_raise:
            n_callers_after_raise += 1
    return (n_raises == 1 and n_callers_after_raise >= 1)


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
    key = _hand_key(hole)
    rng = _spot_rng(state)

    # ---- PREFLOP ----
    if street == "preflop":
        if not facing:
            # First in — standard TAG open
            if _should_open(cls, pos):
                return {"action": "raise", "amount": _bet_to(state, 1.0)}
            return {"action": "check"} if can_check else {"action": "fold"}

        # Facing a raise — is this a squeeze spot (open + caller in front)?
        if _squeeze_spot(state):
            # Big polarized squeeze. Sizing larger than normal 3-bet because of
            # the extra dead money from the caller(s).
            if key in _SQUEEZE_VALUE:
                return {"action": "raise", "amount": _bet_to(state, 4.0)}
            if key in _SQUEEZE_BLUFF:
                return {"action": "raise", "amount": _bet_to(state, 4.0)}
            # Hands not in squeeze range: fold the trash, occasionally flat
            # a strong-but-not-squeeze hand to keep the caller line balanced.
            if cls in ("premium", "strong"):
                return {"action": "call"}
            return {"action": "fold"}

        # Non-squeeze: straightforward 3-bet/call game
        if cls == "premium":
            return {"action": "raise", "amount": _bet_to(state, 2.7)}
        if cls == "strong":
            if rng.random() < 0.3:
                return {"action": "raise", "amount": _bet_to(state, 2.7)}
            return {"action": "call"}
        if cls == "playable" and owed <= pot * 0.5:
            return {"action": "call"}
        return {"action": "fold"}

    # ---- POSTFLOP ----
    strength = _made_strength(hole, board)
    # After squeezing, squeezer often has initiative as the preflop aggressor.
    if owed > 0:
        if strength == "strong":
            return {"action": "raise", "amount": _bet_to(state, 1.0)}
        if strength == "medium":
            return {"action": "call"} if owed <= pot * 0.7 else {"action": "fold"}
        if strength == "weak_pair":
            return {"action": "call"} if owed <= pot * 0.35 else {"action": "fold"}
        if strength == "draw":
            price = owed / (pot + owed) if (pot + owed) > 0 else 1.0
            return {"action": "call"} if price <= 0.32 else {"action": "fold"}
        return {"action": "fold"}

    # No bet to call — c-bet the squeezed pot at high frequency
    if strength == "strong":
        return {"action": "raise", "amount": _bet_to(state, 0.7)}
    if strength == "medium":
        return {"action": "raise", "amount": _bet_to(state, 0.55)}
    if strength == "draw":
        if rng.random() < 0.6:
            return {"action": "raise", "amount": _bet_to(state, 0.6)}
        return {"action": "check"} if can_check else {"action": "fold"}
    # air: continuation-bet a squeezed pot ~60% (we represented strength pre)
    if rng.random() < 0.6 and len(board) == 3:
        return {"action": "raise", "amount": _bet_to(state, 0.55)}
    return {"action": "check"} if can_check else {"action": "fold"}


def decide(game_state):
    if isinstance(game_state, dict) and game_state.get("type") == "warmup":
        return {"action": "fold"}
    try:
        return _sanitize(_play(game_state), game_state)
    except Exception:
        return {"action": "fold"}
