"""
balanced_lag — Fullhouse test opponent (LAG archetype).

Loose-aggressive: opens ~40% of hands, 3-bets ~10%, c-bets ~65%, double-barrels
on equity-gain turns, calls lighter than a TAG (any pair down to half-pot bets,
draws to good prices). Different from multi_barrel which has TAG preflop +
maniac postflop — this one is wide-pre too.

Determinism: spot-seeded RNG so paired A/B tests stay deterministic.
"""

import random
import zlib

try:
    import eval7
    _HAVE_EVAL7 = True
except Exception:
    eval7 = None
    _HAVE_EVAL7 = False

BOT_NAME = "BalancedLAG"
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


# Wider tier definition for LAG opens
_TIER_ORDER = {"premium": 5, "strong": 4, "playable": 3,
               "speculative": 2, "weak": 1, "trash": 0}


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
    gap = hi - lo
    if pair and hi >= 12:                              return "premium"
    if hi == 14 and lo == 13:                          return "premium"
    if pair and hi >= 9:                               return "strong"
    if hi == 14 and lo >= 11:                          return "strong"
    if hi == 13 and lo == 12 and suited:               return "strong"
    if pair:                                           return "playable"
    if hi == 14:                                       return "playable"   # any ace
    if hi >= 12 and lo >= 9:                           return "playable"   # broadway
    if suited and gap <= 2 and lo >= 4:                return "playable"   # SC/1g
    if suited and hi >= 11:                            return "speculative"
    if hi >= 12 and lo >= 7:                           return "speculative"
    if suited and gap <= 3 and lo >= 3:                return "weak"
    if hi >= 11 and lo >= 6:                           return "weak"
    return "trash"


def _position(state):
    try:
        n = len(state.get("players") or [])
        seat = state.get("seat_to_act", 0)
        return seat / max(n - 1, 1)
    except Exception:
        return 0.5


def _should_open(cls, pos, rng):
    """LAG opens: very wide late, moderate early."""
    rank = _TIER_ORDER[cls]
    if pos >= 0.7:    return rank >= _TIER_ORDER["weak"]            # ~50% range
    if pos >= 0.45:   return rank >= _TIER_ORDER["speculative"]     # ~35%
    if pos >= 0.2:    return rank >= _TIER_ORDER["playable"]        # ~22%
    # UTG — still wider than TAG, mix in some speculative for balance
    if rank >= _TIER_ORDER["playable"]:
        return True
    if rank == _TIER_ORDER["speculative"] and rng.random() < 0.4:
        return True
    return False


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


def _hand_strength(hole, board):
    """Coarse bucket: monster / strong / medium / weak_pair / draw / overcards / air."""
    if len(board) < 3:
        return "preflop"
    if _HAVE_EVAL7:
        h = _eval7_cards(hole)
        b = _eval7_cards(board)
        if len(h) == 2 and len(b) >= 3:
            try:
                rank = eval7.evaluate(h + b)
                ht = eval7.handtype(rank)
                if ht in ("Full House", "Quads", "Straight Flush"):  return "monster"
                if ht in ("Trips", "Straight", "Flush", "Two Pair"): return "strong"
                if ht == "Pair":
                    board_vals = sorted((_VAL.get(c[0], 0) for c in board), reverse=True)
                    hole_vals = sorted((_VAL.get(c[0], 0) for c in hole), reverse=True)
                    top = board_vals[0] if board_vals else 0
                    if hole_vals[0] == hole_vals[1] and hole_vals[0] > top:
                        return "medium"   # overpair
                    if hole_vals[0] == top:
                        return "medium"   # top pair
                    return "weak_pair"
            except Exception:
                pass
    # Heuristic / fallback
    try:
        ranks = [c[0] for c in hole + board]
        counts = {}
        for r in ranks:
            counts[r] = counts.get(r, 0) + 1
        cv = sorted(counts.values(), reverse=True)
        if (cv and cv[0] >= 3) or cv.count(2) >= 2:    return "strong"
        if cv and cv[0] == 2:                          return "medium"
        # draw check
        suits = [c[1] for c in hole + board]
        suit_max = max((suits.count(s) for s in set(suits)), default=0)
        if suit_max == 4:                              return "draw"
        # overcards
        bv = [_VAL.get(c[0], 0) for c in board]
        hv = [_VAL.get(c[0], 0) for c in hole]
        if bv and hv and max(hv) > max(bv):            return "overcards"
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

    # ---- PREFLOP ----
    if street == "preflop":
        if not facing:
            if _should_open(cls, pos, rng):
                return {"action": "raise", "amount": _bet_to(state, 1.0)}
            return {"action": "check"} if can_check else {"action": "fold"}

        # Facing a raise — wide 3-bet/call
        rank = _TIER_ORDER[cls]
        if rank >= _TIER_ORDER["strong"]:
            # 3-bet value heavy
            return {"action": "raise", "amount": _bet_to(state, 3.0)}
        if rank >= _TIER_ORDER["playable"]:
            # Mix 3-bet/call
            if rng.random() < 0.30:
                return {"action": "raise", "amount": _bet_to(state, 3.0)}
            return {"action": "call"}
        if rank == _TIER_ORDER["speculative"]:
            # Bluff 3-bet sometimes from late position
            if pos >= 0.6 and rng.random() < 0.20:
                return {"action": "raise", "amount": _bet_to(state, 3.0)}
            if owed <= pot * 0.5:
                return {"action": "call"}
            return {"action": "fold"}
        if rank == _TIER_ORDER["weak"] and pos >= 0.7 and owed <= pot * 0.4:
            return {"action": "call"}
        return {"action": "fold"}

    # ---- POSTFLOP ----
    strength = _hand_strength(hole, board)
    had_init = _i_was_aggressor_last(state)

    if owed > 0:
        # LAG calls down lighter than TAG
        if strength in ("monster", "strong"):
            return {"action": "raise", "amount": _bet_to(state, 1.0)}
        if strength == "medium":
            if owed <= pot * 0.85:                     return {"action": "call"}
            return {"action": "fold"}
        if strength == "weak_pair":
            if owed <= pot * 0.45:                     return {"action": "call"}
            return {"action": "fold"}
        if strength == "draw":
            if owed <= pot * 0.45:                     return {"action": "call"}
            return {"action": "fold"}
        if strength == "overcards":
            if owed <= pot * 0.25 and pos >= 0.5:      return {"action": "call"}
            return {"action": "fold"}
        # air — fold most, occasional float in position
        if owed <= pot * 0.20 and pos >= 0.6 and rng.random() < 0.25:
            return {"action": "call"}
        return {"action": "fold"}

    # No bet to call
    if strength in ("monster", "strong"):
        return {"action": "raise", "amount": _bet_to(state, 0.75)}
    if strength == "medium":
        return {"action": "raise", "amount": _bet_to(state, 0.6)}
    if strength == "draw":
        # semibluff often
        if rng.random() < 0.6:
            return {"action": "raise", "amount": _bet_to(state, 0.65)}
        return {"action": "check"} if can_check else {"action": "fold"}

    # With initiative + air, c-bet often (LAG signature)
    if had_init:
        n_board = len(board)
        if n_board == 3:
            # ~65% c-bet
            if rng.random() < 0.65:
                return {"action": "raise", "amount": _bet_to(state, 0.55)}
            return {"action": "check"} if can_check else {"action": "fold"}
        if n_board == 4:
            # double-barrel ~50%
            if rng.random() < 0.50:
                return {"action": "raise", "amount": _bet_to(state, 0.65)}
            return {"action": "check"} if can_check else {"action": "fold"}
        if n_board == 5:
            # triple-barrel ~25% — LAGs slow down rivers more than multi_barrel
            if rng.random() < 0.25:
                return {"action": "raise", "amount": _bet_to(state, 0.75)}
            return {"action": "check"} if can_check else {"action": "fold"}

    # No initiative, weak hand
    if pos >= 0.7 and rng.random() < 0.25:
        # float-stab in position
        return {"action": "raise", "amount": _bet_to(state, 0.5)}
    return {"action": "check"} if can_check else {"action": "fold"}


def decide(game_state):
    if isinstance(game_state, dict) and game_state.get("type") == "warmup":
        return {"action": "fold"}
    try:
        return _sanitize(_play(game_state), game_state)
    except Exception:
        return {"action": "fold"}
