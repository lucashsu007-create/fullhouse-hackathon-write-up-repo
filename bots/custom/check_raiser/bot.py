"""
check_raiser — Fullhouse test opponent (PROBES RESPONSE-TO-AGGRESSION).

Archetype: a flop check-raise specialist. When out of position vs the preflop
raiser, it CHECKS to induce a c-bet, then RAISES ~35% of the time with a
BALANCED range: value (sets, two pair, strong top pair), draws (semibluff), and
pure air (bluff). It is NOT a tight trapper — it raises with bluffs too.

The leak it targets: a hero's response to a raise after the hero takes
initiative. Two failure modes:
  - OVER-FOLD: hero c-bets, gets raised, folds its whole c-bet range. Loses to
    the ~25% air portion of the check-raise.
  - OVER-CONTINUE: hero calls/jams too wide vs the raise. Loses to the ~45%
    value portion.
The correct response is a calibrated continue frequency. A hero with a
board-conditioned NUTTED filter tuned only for confirmed-NIT raisers won't have
the right answer vs a balanced raiser.

Distinct from trap_tag: trap_tag check-raises only with the nuts, from a tight
preflop range, at low frequency. check_raiser raises with bluffs and draws too,
from a normal range, at high frequency.

If the hero called the flop check-raise, on the turn the raiser barrels its
value/draws and gives up most air (so a hero that floats the flop raise wide
can correctly fold the turn — another thing to measure).

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

BOT_NAME = "CheckRaiser"
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
    if pair and hi >= 6:                               return "playable"   # set-mine wide
    if hi == 14 and lo == 11:                          return "playable"
    if hi == 13 and lo == 12:                          return "playable"
    if hi == 13 and lo == 11 and suited:               return "playable"
    if hi == 12 and lo == 11 and suited:               return "playable"
    if pair:                                           return "speculative"
    if suited and hi == 14:                            return "speculative"
    if suited and (hi - lo) <= 2 and lo >= 4:          return "speculative"  # SC for draws
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


def _strength(hole, board):
    """value / strong_tp / draw / air (+ 'preflop')."""
    if len(board) < 3:
        return "preflop"
    # draw detection first (needed even when we have a weak pair)
    has_draw = False
    try:
        suit_counts = {}
        for c in hole + board:
            suit_counts[c[1]] = suit_counts.get(c[1], 0) + 1
        flush_draw = max(suit_counts.values(), default=0) == 4
        vals_all = sorted(set(_VAL.get(c[0], 0) for c in hole + board))
        oesd = False
        if len(vals_all) >= 4:
            for i in range(len(vals_all) - 3):
                if vals_all[i + 3] - vals_all[i] == 3:
                    oesd = True
                    break
        has_draw = flush_draw or oesd
    except Exception:
        has_draw = False

    if _HAVE_EVAL7:
        h = _eval7_cards(hole)
        b = _eval7_cards(board)
        if len(h) == 2 and len(b) >= 3:
            try:
                ht = eval7.handtype(eval7.evaluate(h + b))
                if ht in ("Two Pair", "Trips", "Straight", "Flush",
                          "Full House", "Quads", "Straight Flush"):
                    return "value"
                if ht == "Pair":
                    board_vals = sorted((_VAL.get(c[0], 0) for c in board), reverse=True)
                    hole_vals = sorted((_VAL.get(c[0], 0) for c in hole), reverse=True)
                    if board_vals and hole_vals:
                        top = board_vals[0]
                        if hole_vals[0] == hole_vals[1] and hole_vals[0] > top:
                            return "value"          # overpair
                        if hole_vals[0] == top and hole_vals[1] >= 12:
                            return "strong_tp"      # TPTK
                    return "draw" if has_draw else "air"
            except Exception:
                pass
    # Fallback
    try:
        ranks = [c[0] for c in hole + board]
        counts = {}
        for r in ranks:
            counts[r] = counts.get(r, 0) + 1
        cv = sorted(counts.values(), reverse=True)
        if (cv and cv[0] >= 3) or cv.count(2) >= 2:
            return "value"
        if cv and cv[0] == 2:
            return "draw" if has_draw else "strong_tp"
    except Exception:
        pass
    return "draw" if has_draw else "air"


def _facing_raise(state):
    for a in state.get("action_log", []) or []:
        if a.get("action") in ("raise", "all_in"):
            return True
    return False


def _i_raised_this_street(state):
    """Did we already raise on the current street? (avoid re-raising loops)"""
    me = state.get("seat_to_act")
    # crude: look at the tail of the log since the last street boundary is not
    # tagged; just check if our last action was a raise.
    last = None
    for a in state.get("action_log", []) or []:
        if a.get("seat") == me:
            last = a.get("action")
    return last in ("raise", "all_in")


def _villain_bet_into_us(state):
    """True if there's a bet we owe (i.e. villain c-bet after we checked)."""
    return state.get("amount_owed", 0) > 0


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

    # ---- PREFLOP — call to set up OOP check-raise spots ----
    if street == "preflop":
        if not facing:
            # Open a normal range; we WANT to be the caller OOP often, so we
            # also call rather than always raising from the blinds.
            if _TIER_ORDER[cls] >= _TIER_ORDER["strong"]:
                return {"action": "raise", "amount": _bet_to(state, 1.0)}
            if _TIER_ORDER[cls] >= _TIER_ORDER["playable"] and can_check:
                return {"action": "check"}
            return {"action": "check"} if can_check else {"action": "fold"}
        # Facing a raise: flat wide (to realize check-raise equity postflop)
        if cls == "premium":
            return {"action": "raise", "amount": _bet_to(state, 2.5)}
        if cls in ("strong", "playable") and owed <= pot * 0.6:
            return {"action": "call"}
        if cls == "speculative" and owed <= pot * 0.4:
            return {"action": "call"}
        return {"action": "fold"}

    strength = _strength(hole, board)
    n_board = len(board)

    # ---- FLOP CHECK-RAISE LOGIC ----
    if n_board == 3:
        if _villain_bet_into_us(state):
            # We checked, villain bet — decide whether to check-raise.
            already_raised = _i_raised_this_street(state)
            if already_raised:
                # We already raised and got re-raised; only continue with value
                if strength == "value":
                    return {"action": "call"}
                return {"action": "fold"}
            # Balanced check-raise frequencies by hand class
            if strength == "value":
                # raise ~80% of value (rest slowplay-call)
                if rng.random() < 0.80:
                    return {"action": "raise", "amount": _bet_to(state, 1.0)}
                return {"action": "call"}
            if strength == "strong_tp":
                # mostly call, occasionally raise for protection
                if rng.random() < 0.25:
                    return {"action": "raise", "amount": _bet_to(state, 1.0)}
                return {"action": "call"}
            if strength == "draw":
                # semibluff raise ~50%
                if rng.random() < 0.50:
                    return {"action": "raise", "amount": _bet_to(state, 1.0)}
                price = owed / (pot + owed) if (pot + owed) > 0 else 1.0
                return {"action": "call"} if price <= 0.33 else {"action": "fold"}
            # air — pure bluff raise ~30%, else fold
            if rng.random() < 0.30:
                return {"action": "raise", "amount": _bet_to(state, 1.0)}
            return {"action": "fold"}
        # No bet into us on flop: check to induce (the whole point), unless we
        # are IP and everyone checked to us — then bet value, check rest.
        if can_check:
            return {"action": "check"}
        # facing nothing but cannot check (rare): act on strength
        if strength in ("value", "strong_tp"):
            return {"action": "raise", "amount": _bet_to(state, 0.6)}
        return {"action": "fold"}

    # ---- TURN / RIVER follow-through after a flop check-raise ----
    if owed > 0:
        if strength == "value":
            return {"action": "raise", "amount": _bet_to(state, 1.0)}
        if strength == "strong_tp":
            return {"action": "call"} if owed <= pot * 0.7 else {"action": "fold"}
        if strength == "draw":
            price = owed / (pot + owed) if (pot + owed) > 0 else 1.0
            return {"action": "call"} if price <= 0.32 else {"action": "fold"}
        return {"action": "fold"}

    # We have the lead post-check-raise: barrel value/draws, give up air.
    if strength == "value":
        return {"action": "raise", "amount": _bet_to(state, 0.75)}
    if strength == "strong_tp":
        return {"action": "raise", "amount": _bet_to(state, 0.6)}
    if strength == "draw":
        if rng.random() < 0.55:
            return {"action": "raise", "amount": _bet_to(state, 0.65)}
        return {"action": "check"} if can_check else {"action": "fold"}
    # air — mostly give up on later streets (don't keep firing like multi_barrel)
    if rng.random() < 0.25:
        return {"action": "raise", "amount": _bet_to(state, 0.6)}
    return {"action": "check"} if can_check else {"action": "fold"}


def decide(game_state):
    if isinstance(game_state, dict) and game_state.get("type") == "warmup":
        return {"action": "fold"}
    try:
        return _sanitize(_play(game_state), game_state)
    except Exception:
        return {"action": "fold"}
