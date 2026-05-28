"""
min_ball — Fullhouse test opponent (PROBES FIXED-THRESHOLD LEAK, low end).

Archetype: a small-ball merchant. Bets and raises tiny (0.25-0.35 pot)
everywhere, every street. Never bets big. C-bets very frequently because the
price it lays is so cheap.

The leak it targets: a hero with FIXED equity-bucket call thresholds
(e.g. call at >=0.38, value at >=0.55) that DON'T move with bet size. A 0.3-pot
bet only needs ~23% equity to call profitably, but a fixed-threshold hero still
applies its static buckets and OVER-FOLDS to cheap bets — folding hands that
have a trivially +EV call. min_ball steals a steady stream of tiny pots from
any opponent who folds too much to small sizing.

It folds to raises (it has no big-pot game), so a hero that just raises back
beats it — which is exactly the correct counter and what we want to detect.

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

BOT_NAME = "MinBall"
BOT_AVATAR = "robot_1"

_RANKS = "23456789TJQKA"
_VAL = {r: i for i, r in enumerate(_RANKS, start=2)}

_SMALL = 0.30   # default tiny bet fraction of pot


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
    if pair and hi >= 9:                               return "strong"
    if hi == 14 and lo >= 11:                          return "strong"
    if pair:                                           return "playable"
    if hi == 14:                                       return "playable"
    if hi >= 12 and lo >= 9:                           return "playable"
    if suited and (hi - lo) <= 2 and lo >= 4:          return "speculative"
    if hi >= 11 and lo >= 8:                           return "speculative"
    return "trash"


def _position(state):
    try:
        n = len(state.get("players") or [])
        seat = state.get("seat_to_act", 0)
        return seat / max(n - 1, 1)
    except Exception:
        return 0.5


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


def _has_anything(hole, board):
    """Return 'made' (pair+), 'draw', or 'air' — min_ball stabs with everything
    but values knowing whether it has a hand."""
    if len(board) < 3:
        return "preflop"
    if _HAVE_EVAL7:
        h = _eval7_cards(hole)
        b = _eval7_cards(board)
        if len(h) == 2 and len(b) >= 3:
            try:
                ht = eval7.handtype(eval7.evaluate(h + b))
                if ht != "High Card":
                    return "made"
            except Exception:
                pass
    # draw / air via heuristic
    try:
        suits = [c[1] for c in hole + board]
        suit_max = max((suits.count(s) for s in set(suits)), default=0)
        if suit_max == 4:
            return "draw"
    except Exception:
        pass
    return "air"


def _facing_raise(state):
    for a in state.get("action_log", []) or []:
        if a.get("action") in ("raise", "all_in"):
            return True
    return False


def _facing_real_bet(state):
    """A bet we'd have to call that is NOT trivially small relative to pot."""
    owed = state.get("amount_owed", 0)
    return owed > 0


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

    # ---- PREFLOP — small opens, modest continues ----
    if street == "preflop":
        if not facing:
            # Open small (2x-ish) with a moderately wide range
            if _TIER_ORDER[cls] >= _TIER_ORDER["speculative"] or (
                    pos >= 0.6 and cls != "trash"):
                return {"action": "raise", "amount": _bet_to(state, 0.5)}
            return {"action": "check"} if can_check else {"action": "fold"}
        # Facing a raise: call cheap with reasonable hands, no big 3-bet game
        if cls == "premium":
            return {"action": "raise", "amount": _bet_to(state, 1.0)}
        if cls in ("strong", "playable") and owed <= pot * 0.6:
            return {"action": "call"}
        if cls == "speculative" and owed <= pot * 0.35:
            return {"action": "call"}
        return {"action": "fold"}

    # ---- POSTFLOP — the small-ball machine ----
    hand = _has_anything(hole, board)

    # Facing a bet: min_ball has no big-pot game. It calls cheap, folds to
    # anything sizeable, and only raises (small) with made hands.
    if owed > 0:
        price = owed / (pot + owed) if (pot + owed) > 0 else 1.0
        if hand == "made":
            # small raise for thin value, or call
            if rng.random() < 0.4:
                return {"action": "raise", "amount": _bet_to(state, _SMALL)}
            return {"action": "call"} if price <= 0.42 else {"action": "fold"}
        if hand == "draw":
            return {"action": "call"} if price <= 0.30 else {"action": "fold"}
        # air: fold unless basically free
        return {"action": "call"} if price <= 0.18 else {"action": "fold"}

    # No bet to call — stab small with almost everything (the leak probe)
    if hand == "made":
        return {"action": "raise", "amount": _bet_to(state, _SMALL)}
    if hand == "draw":
        return {"action": "raise", "amount": _bet_to(state, _SMALL)}
    # air: c-bet small ~75% of the time
    if rng.random() < 0.75:
        return {"action": "raise", "amount": _bet_to(state, _SMALL)}
    return {"action": "check"} if can_check else {"action": "fold"}


def decide(game_state):
    if isinstance(game_state, dict) and game_state.get("type") == "warmup":
        return {"action": "fold"}
    try:
        return _sanitize(_play(game_state), game_state)
    except Exception:
        return {"action": "fold"}
