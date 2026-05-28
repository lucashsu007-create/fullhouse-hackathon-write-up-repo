"""
mc_pot_odds — Fullhouse test opponent.

Archetype: a Monte-Carlo / pot-odds caller. Estimates hand equity and calls only
when the price is good enough (equity >= required equity, with a small margin).
Sensitive to bet size: it calls small bets far more than big ones, which is
exactly the cheap > medium > expensive call gradient the classifier's price
buckets are meant to detect.

Equity:
  - uses eval7 Monte Carlo when available (seeded from the spot -> reproducible)
  - falls back to a cheap preflop/postflop heuristic if eval7 is missing,
    so the bot is always legal and never crashes

Behavior:
  - preflop: raise strong hands, otherwise call iff equity covers the price
  - postflop: bet strong made hands, otherwise call iff equity covers the price
  - never bluffs; this is a pure price/equity machine

Legal/safe: stdlib + optional eval7 only, no file/network, always returns a
valid action.
"""

import random
import time
import zlib

try:
    import eval7
    _HAVE_EVAL7 = True
except Exception:
    eval7 = None
    _HAVE_EVAL7 = False

BOT_NAME = "MCPotOdds"
BOT_AVATAR = "robot_1"

_RANKS = "23456789TJQKA"
_SUITS = "shdc"
_VAL = {r: i for i, r in enumerate(_RANKS, start=2)}

_EQ_TIME_BUDGET = 0.25
_EQ_MIN_ITERS = 80
_EQ_MAX_ITERS = 800
_CALL_MARGIN = 0.02      # require equity to beat the price by this much

if _HAVE_EVAL7:
    try:
        _FULL_DECK = [eval7.Card(r + s) for r in _RANKS for s in _SUITS]
    except Exception:
        _FULL_DECK = []
else:
    _FULL_DECK = []


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


def _equity(hole, board, n_opp, rng):
    """Monte Carlo equity vs n random opponents; None on failure/unavailable."""
    if not _HAVE_EVAL7 or not _FULL_DECK:
        return None
    try:
        hole_c = [eval7.Card(c) for c in hole]
        board_c = [eval7.Card(c) for c in board]
    except Exception:
        return None
    if len(hole_c) != 2:
        return None
    n_opp = max(1, min(n_opp, 8))
    known = set(str(c) for c in hole_c + board_c)
    deck = [c for c in _FULL_DECK if str(c) not in known]
    need = 5 - len(board_c)
    if need < 0:
        return None
    draw_n = 2 * n_opp + need
    if draw_n > len(deck):
        return None

    wins = ties = iters = 0
    t0 = time.perf_counter()
    try:
        while iters < _EQ_MAX_ITERS:
            if iters >= _EQ_MIN_ITERS and (time.perf_counter() - t0) > _EQ_TIME_BUDGET:
                break
            drawn = rng.sample(deck, draw_n)
            opp = [drawn[i * 2:i * 2 + 2] for i in range(n_opp)]
            sim = board_c + drawn[2 * n_opp:2 * n_opp + need]
            hero = eval7.evaluate(hole_c + sim)
            best = max(eval7.evaluate(o + sim) for o in opp)
            if hero > best:
                wins += 1
            elif hero == best:
                ties += 1
            iters += 1
    except Exception:
        if iters < _EQ_MIN_ITERS:
            return None
    if iters == 0:
        return None
    return (wins + ties * 0.5) / iters


def _heuristic_equity(hole, board, n_opp):
    """Cheap fallback equity when eval7 is unavailable."""
    try:
        r1, s1 = hole[0][0], hole[0][1]
        r2, s2 = hole[1][0], hole[1][1]
    except (IndexError, TypeError):
        return 0.3
    v1, v2 = _VAL.get(r1, 0), _VAL.get(r2, 0)
    hi, lo = max(v1, v2), min(v1, v2)
    pair = r1 == r2
    suited = s1 == s2
    if not board:
        base = 0.5 + (hi + lo) / 56.0          # ~0.5..0.95 by card strength
        if pair:
            base += 0.12
        if suited:
            base += 0.03
        base = min(0.95, base)
    else:
        cards = hole + board
        ranks = [c[0] for c in cards]
        counts = {}
        for r in ranks:
            counts[r] = counts.get(r, 0) + 1
        cv = sorted(counts.values(), reverse=True)
        if cv and cv[0] >= 3:
            base = 0.85
        elif cv.count(2) >= 2:
            base = 0.75
        elif cv and cv[0] == 2:
            base = 0.55
        else:
            base = 0.35
    # decay for extra opponents
    return max(0.05, base * (0.92 ** (max(1, n_opp) - 1)))


def _required_equity(game_state):
    owed = game_state.get("amount_owed", 0)
    pot = game_state.get("pot", 0)
    if owed <= 0:
        return 0.0
    return owed / (pot + owed)


def _n_opp(game_state):
    me = game_state.get("seat_to_act")
    n = 0
    for p in game_state.get("players") or []:
        try:
            if p["seat"] == me:
                continue
            if not p.get("is_folded") and p.get("state") != "busted":
                n += 1
        except (KeyError, TypeError):
            continue
    return max(1, n)


def decide(game_state: dict) -> dict:
    try:
        if not isinstance(game_state, dict):
            return {"action": "fold"}
        if game_state.get("type") == "warmup":
            # Touch eval7 once so the first real call isn't paying import cost.
            if _HAVE_EVAL7 and _FULL_DECK:
                try:
                    _equity(["As", "Kh"], ["2c", "7d", "9s"], 1,
                            random.Random(0))
                except Exception:
                    pass
            return {"action": "fold"}

        can_check = game_state.get("can_check", False)
        hole = game_state.get("your_cards", []) or []
        board = game_state.get("community_cards", []) or []
        stack = game_state.get("your_stack", 0)
        my_bet = game_state.get("your_bet_this_street", 0)
        min_to = game_state.get("min_raise_to", 0)
        pot = game_state.get("pot", 0)
        rng = _spot_rng(game_state)
        n_opp = _n_opp(game_state)

        eq = _equity(hole, board, n_opp, rng)
        if eq is None:
            eq = _heuristic_equity(hole, board, n_opp)

        # Strong equity -> bet/raise for value (sizes ~2/3 pot).
        if eq >= 0.78:
            if can_check:
                bet = min(int(pot * 0.66) + min_to, stack + my_bet)
                return {"action": "raise", "amount": max(bet, min_to)}
            # facing a bet with a monster: just call (pure price machine, no
            # fancy raising) — keeps it a clean pot-odds caller.
            return {"action": "call"}

        # Otherwise it's purely a pot-odds decision.
        if can_check:
            return {"action": "check"}

        req = _required_equity(game_state)
        if eq >= req + _CALL_MARGIN:
            return {"action": "call"}
        return {"action": "fold"}

    except Exception:
        return {"action": "fold"}
