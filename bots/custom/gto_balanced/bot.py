"""
gto_balanced — Fullhouse test opponent (GTO-INSPIRED).

Plays a balanced, hard-to-exploit strategy. Not actual GTO (that requires a
solver) but the right SHAPE:
  - position-aware open ranges (tighter EP, wider LP)
  - 3-bet ranges that MIX polarised and linear by board type expectation
  - check-back range on the flop (~25% of strong hands trap; gives villain no
    "they always c-bet" read)
  - balanced value/bluff on bet streets (not pure value, not pure bluff)
  - sizing varies with board texture: small on static, big on dynamic
  - folds correctly to large bets, but doesn't over-fold to small ones

The defining quality: any single counter-strategy a hero might use (over-fold
to c-bets, call light vs 3-bets, raise small bets) gets punished by the
balancing branch.

Determinism: spot-seeded RNG so paired A/B tests stay deterministic. All
"mixed" decisions are driven by rng.random() against fixed thresholds.

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

BOT_NAME = "GTOBalanced"
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


# 3-bet ranges — split into value and bluff (the "polarised" part)
_VALUE_3BET = {"AA", "KK", "QQ", "JJ", "AKs", "AKo"}
_BLUFF_3BET = {"A5s", "A4s", "K9s", "Q9s", "T9s", "76s"}
# "Linear" 3-bet additions (TT, AQ) — only mix in vs tight openers; we
# represent that here by adding them to value 3bet 50% of the time via RNG.
_LINEAR_3BET = {"TT", "AQs", "AQo"}


# Eval7 strength
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


def _strength_and_texture(hole, board):
    """Return (strength, texture).
    strength: monster/strong/medium/weak_pair/draw/overcards/air
    texture:  dry/semi_wet/wet (more wet = more dynamic)
    """
    texture = "dry"
    try:
        suits = [c[1] for c in board]
        vals = sorted(set(_VAL.get(c[0], 0) for c in board))
        # flush threat
        suit_max = max((suits.count(s) for s in set(suits)), default=0)
        if suit_max >= 3:
            texture = "wet"
        elif suit_max == 2:
            texture = "semi_wet"
        # connector threat
        if len(vals) >= 2:
            gaps = [vals[i+1] - vals[i] for i in range(len(vals)-1)]
            if min(gaps) <= 2 and texture != "wet":
                texture = "semi_wet"
            if min(gaps) == 1 and texture == "semi_wet":
                texture = "wet"
    except Exception:
        pass

    if not _HAVE_EVAL7 or len(board) < 3:
        return ("air", texture)
    h = _eval7_cards(hole)
    b = _eval7_cards(board)
    if len(h) != 2 or len(b) < 3:
        return ("air", texture)
    try:
        rank = eval7.evaluate(h + b)
        ht = eval7.handtype(rank)
    except Exception:
        return ("air", texture)

    if ht in ("Full House", "Quads", "Straight Flush"): return ("monster", texture)
    if ht in ("Trips", "Straight", "Flush"):            return ("strong", texture)
    if ht == "Two Pair":                                return ("strong", texture)
    if ht == "Pair":
        board_vals = sorted((_VAL.get(c[0], 0) for c in board), reverse=True)
        hole_vals = sorted((_VAL.get(c[0], 0) for c in hole), reverse=True)
        if board_vals and hole_vals:
            top = board_vals[0]
            if hole_vals[0] == hole_vals[1] and hole_vals[0] > top:
                return ("medium", texture)             # overpair
            if hole_vals[0] == top and hole_vals[1] >= 12:
                return ("medium", texture)             # TPTK / TPGK
            if hole_vals[0] == top:
                return ("weak_pair", texture)          # TP weak kicker
            return ("weak_pair", texture)              # middle/bottom pair
        return ("weak_pair", texture)
    # No made hand — check for draws
    try:
        suit_counts = {}
        for c in hole + board:
            suit_counts[c[1]] = suit_counts.get(c[1], 0) + 1
        flush_draw = max(suit_counts.values(), default=0) == 4
        vals_all = sorted(set(_VAL.get(c[0], 0) for c in hole + board))
        oesd = False
        if len(vals_all) >= 4:
            for i in range(len(vals_all)-3):
                if vals_all[i+3] - vals_all[i] == 3:
                    oesd = True
                    break
        if flush_draw or oesd:
            return ("draw", texture)
    except Exception:
        pass

    # Overcards
    try:
        bv = [_VAL.get(c[0], 0) for c in board]
        hv = [_VAL.get(c[0], 0) for c in hole]
        if bv and hv and min(hv) > max(bv):
            return ("overcards", texture)
        if bv and hv and max(hv) > max(bv):
            return ("overcards", texture)
    except Exception:
        pass
    return ("air", texture)


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
    key = _hand_key(hole)
    cls = _preflop_class(hole)
    rng = _spot_rng(state)

    # ---- PREFLOP ----
    if street == "preflop":
        raises = sum(1 for a in (state.get("action_log") or [])
                     if a.get("action") in ("raise", "all_in"))

        if not facing:
            if _should_open(cls, pos):
                # Sizing varies slightly with position (smaller from BTN, bigger EP)
                size = 1.0 if pos < 0.5 else 0.85
                return {"action": "raise", "amount": _bet_to(state, size)}
            return {"action": "check"} if can_check else {"action": "fold"}

        if raises == 1:
            # Polarised + occasional linear 3-bets
            if key in _VALUE_3BET:
                return {"action": "raise", "amount": _bet_to(state, 3.0)}
            if key in _BLUFF_3BET and pos >= 0.5 and rng.random() < 0.65:
                # ~65% of bluff candidates 3-bet; rest fold/call
                return {"action": "raise", "amount": _bet_to(state, 3.0)}
            if key in _LINEAR_3BET and rng.random() < 0.45:
                # Linear additions ~45% of the time (mixed strategy)
                return {"action": "raise", "amount": _bet_to(state, 2.7)}
            # Otherwise call/fold based on hand
            if cls in ("premium", "strong", "playable"):
                return {"action": "call"}
            if cls == "speculative" and pos >= 0.6 and owed <= pot * 0.5:
                return {"action": "call"}
            return {"action": "fold"}

        # Facing 3-bet or worse: tight
        if key in {"AA", "KK"}:
            return {"action": "raise", "amount": _bet_to(state, 2.2)}
        if cls == "premium":
            return {"action": "call"}
        if cls == "strong" and owed <= pot * 0.5:
            # mix call/fold to balance
            if rng.random() < 0.55:
                return {"action": "call"}
        return {"action": "fold"}

    # ---- POSTFLOP ----
    strength, texture = _strength_and_texture(hole, board)
    had_init = _i_was_aggressor_last(state)
    n_board = len(board)

    if owed > 0:
        # Facing a bet — balanced calling range, doesn't over-fold
        if strength in ("monster",):
            # Sometimes raise sometimes call (slowplay for balance)
            if rng.random() < 0.45:
                return {"action": "raise", "amount": _bet_to(state, 1.0)}
            return {"action": "call"}
        if strength == "strong":
            # Raise more often, but slowplay sometimes
            if rng.random() < 0.65:
                return {"action": "raise", "amount": _bet_to(state, 1.0)}
            return {"action": "call"}
        if strength == "medium":
            if owed <= pot * 0.75:                     return {"action": "call"}
            if owed <= pot * 1.0 and rng.random() < 0.5:
                return {"action": "call"}
            return {"action": "fold"}
        if strength == "weak_pair":
            if owed <= pot * 0.4:                      return {"action": "call"}
            return {"action": "fold"}
        if strength == "draw":
            # call good price always; semi-raise occasionally
            if owed <= pot * 0.5:
                if rng.random() < 0.20:
                    return {"action": "raise", "amount": _bet_to(state, 1.1)}
                return {"action": "call"}
            return {"action": "fold"}
        if strength == "overcards":
            if owed <= pot * 0.25 and pos >= 0.5 and rng.random() < 0.6:
                return {"action": "call"}
            return {"action": "fold"}
        # air — fold mostly, very rare float
        if owed <= pot * 0.20 and pos >= 0.7 and rng.random() < 0.15:
            return {"action": "call"}
        return {"action": "fold"}

    # No bet to call
    if strength == "monster":
        # CHECK-BACK RANGE: trap with monsters 30%
        if rng.random() < 0.30 and can_check:
            return {"action": "check"}
        # Bet sizing: smaller on dry boards, larger on wet
        size = 0.55 if texture == "dry" else (0.75 if texture == "semi_wet" else 0.95)
        return {"action": "raise", "amount": _bet_to(state, size)}

    if strength == "strong":
        # Mostly bet, sometimes check
        if rng.random() < 0.20 and can_check:
            return {"action": "check"}
        size = 0.50 if texture == "dry" else 0.70
        return {"action": "raise", "amount": _bet_to(state, size)}

    if strength == "medium":
        # Mix: bet for value/protection, check sometimes
        if can_check and rng.random() < 0.40:
            return {"action": "check"}
        return {"action": "raise", "amount": _bet_to(state, 0.5)}

    if strength == "draw":
        # Semibluff often
        if can_check and rng.random() < 0.55:
            size = 0.5 if texture == "dry" else 0.7
            return {"action": "raise", "amount": _bet_to(state, size)}
        return {"action": "check"} if can_check else {"action": "fold"}

    # With initiative + air: balanced c-bet/check
    if had_init:
        if n_board == 3:
            # Flop c-bet ~60% on dry, ~50% on wet (range disadvantage on wet)
            cbet_freq = 0.60 if texture == "dry" else (0.50 if texture == "semi_wet" else 0.40)
            if rng.random() < cbet_freq:
                size = 0.4 if texture == "dry" else 0.7
                return {"action": "raise", "amount": _bet_to(state, size)}
            return {"action": "check"} if can_check else {"action": "fold"}
        if n_board == 4:
            # Turn: barrel only with equity-gain (we don't model exactly; ~35%)
            if rng.random() < 0.35:
                return {"action": "raise", "amount": _bet_to(state, 0.65)}
            return {"action": "check"} if can_check else {"action": "fold"}
        if n_board == 5:
            # River: balanced bluff frequency ~25% (paired with value)
            if rng.random() < 0.25:
                return {"action": "raise", "amount": _bet_to(state, 0.8)}
            return {"action": "check"} if can_check else {"action": "fold"}

    # No initiative + air
    if strength == "overcards" and can_check:
        return {"action": "check"}
    # Stab in position occasionally
    if pos >= 0.7 and rng.random() < 0.20 and can_check:
        return {"action": "raise", "amount": _bet_to(state, 0.45)}
    return {"action": "check"} if can_check else {"action": "fold"}


def decide(game_state):
    if isinstance(game_state, dict) and game_state.get("type") == "warmup":
        return {"action": "fold"}
    try:
        return _sanitize(_play(game_state), game_state)
    except Exception:
        return {"action": "fold"}
