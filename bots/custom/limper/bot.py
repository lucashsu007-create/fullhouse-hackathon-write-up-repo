"""
limper — Fullhouse test opponent (PASSIVE LIMP-FOLD).

Archetype: the classic loose-passive limper. Limps (calls the big blind) a wide
~30% range instead of raising, almost never opens with a raise, folds to
iso-raises with the bottom of its range, and plays fit-or-fold postflop —
betting only when it connects, checking and folding when it misses.

The leak it targets: a hero that fails to PUNISH limpers. Against a limp-fold
player the correct response is to iso-raise wide (the limper folds too much
preflop) and to stab at pots postflop (the limper check-folds when it misses).
A hero that just checks behind limps, or only iso-raises premiums, leaves a lot
of dead money on the table. Conversely, a hero that stabs into the limper's
"fit" range (when the limper actually connected and calls/raises) can get
punished — so the limper is not a pure ATM, it does continue when it hits.

Distinct from calling_station (calls everything, never folds) and nit_folder
(folds preflop AND postflop, very tight): limper enters MANY pots cheaply then
folds postflop unless it connects. High VPIP, low PFR, high postflop fold.

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

BOT_NAME = "Limper"
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


_TIER_ORDER = {"premium": 4, "strong": 3, "playable": 2, "speculative": 1,
               "weak": 0.5, "trash": 0}


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
    if pair:                                           return "playable"
    if hi == 14:                                       return "playable"   # any ace
    if hi >= 12 and lo >= 8:                           return "playable"   # broadways
    if suited and gap <= 2 and lo >= 4:                return "playable"   # SC
    if suited and hi >= 10:                            return "speculative"
    if hi >= 11 and lo >= 7:                           return "speculative"
    if suited and gap <= 3:                            return "weak"
    if hi >= 10 and lo >= 6:                           return "weak"
    return "trash"


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


def _connected(hole, board):
    """fit-or-fold read: 'strong' / 'pair' / 'draw' / 'air'."""
    if len(board) < 3:
        return "preflop"
    has_draw = False
    try:
        suit_counts = {}
        for c in hole + board:
            suit_counts[c[1]] = suit_counts.get(c[1], 0) + 1
        if max(suit_counts.values(), default=0) == 4:
            has_draw = True
        vals_all = sorted(set(_VAL.get(c[0], 0) for c in hole + board))
        if len(vals_all) >= 4:
            for i in range(len(vals_all) - 3):
                if vals_all[i + 3] - vals_all[i] == 3:
                    has_draw = True
                    break
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
                    # any pair counts as a 'fit' for a limper (it's loose)
                    return "pair"
            except Exception:
                pass
    else:
        # heuristic
        try:
            ranks = [c[0] for c in hole + board]
            counts = {}
            for r in ranks:
                counts[r] = counts.get(r, 0) + 1
            cv = sorted(counts.values(), reverse=True)
            if (cv and cv[0] >= 3) or cv.count(2) >= 2:
                return "strong"
            if cv and cv[0] == 2:
                return "pair"
        except Exception:
            pass
    return "draw" if has_draw else "air"


def _someone_raised(state):
    for a in state.get("action_log", []) or []:
        if a.get("action") in ("raise", "all_in"):
            return True
    return False


def _price(state):
    owed = state.get("amount_owed", 0)
    pot = state.get("pot", 0)
    if owed <= 0:
        return 0.0
    return owed / (pot + owed)


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
    cls = _preflop_class(hole)
    raised = _someone_raised(state)
    rng = _spot_rng(state)

    # ---- PREFLOP ----
    if street == "preflop":
        rank = _TIER_ORDER[cls]
        if not raised:
            # No raise yet — LIMP wide rather than raise.
            # Only "raise" (open) with the very top, and even then rarely;
            # the defining trait is limping.
            if rank >= _TIER_ORDER["premium"]:
                # Occasionally raise premiums, usually limp to trap (passive).
                if rng.random() < 0.35:
                    return {"action": "raise", "amount": _bet_to(state, 1.0)}
                return {"action": "call"} if owed > 0 else (
                    {"action": "check"} if can_check else {"action": "call"})
            if rank >= _TIER_ORDER["weak"]:
                # Limp the wide range (call the BB / complete the SB)
                if owed > 0:
                    return {"action": "call"}
                return {"action": "check"} if can_check else {"action": "call"}
            # trash: check if free (BB), else fold
            return {"action": "check"} if can_check else {"action": "fold"}

        # Facing a raise — limper folds too much (the leak), continues only
        # with decent hands at a cheap price.
        if rank >= _TIER_ORDER["premium"]:
            return {"action": "call"}
        if rank >= _TIER_ORDER["strong"] and _price(state) <= 0.35:
            return {"action": "call"}
        if rank >= _TIER_ORDER["playable"] and _price(state) <= 0.22:
            return {"action": "call"}
        return {"action": "fold"}

    # ---- POSTFLOP — fit or fold ----
    fit = _connected(hole, board)
    price = _price(state)

    if owed > 0:
        # Facing a bet: continue only when connected
        if fit == "strong":
            # passive: mostly call, occasional raise
            if rng.random() < 0.30:
                return {"action": "raise", "amount": _bet_to(state, 0.8)}
            return {"action": "call"}
        if fit == "pair":
            return {"action": "call"} if price <= 0.42 else {"action": "fold"}
        if fit == "draw":
            return {"action": "call"} if price <= 0.30 else {"action": "fold"}
        # air: check-fold (the punishable trait)
        return {"action": "fold"}

    # No bet to call — passive: bet only strong, check everything else
    if fit == "strong":
        return {"action": "raise", "amount": _bet_to(state, 0.6)}
    if fit == "pair":
        # sometimes bet a pair for thin value, usually check
        if rng.random() < 0.25:
            return {"action": "raise", "amount": _bet_to(state, 0.4)}
        return {"action": "check"} if can_check else {"action": "fold"}
    # draw / air: check (and will fold to a stab) — the fit-or-fold leak
    return {"action": "check"} if can_check else {"action": "fold"}


def decide(game_state):
    if isinstance(game_state, dict) and game_state.get("type") == "warmup":
        return {"action": "fold"}
    try:
        return _sanitize(_play(game_state), game_state)
    except Exception:
        return {"action": "fold"}
