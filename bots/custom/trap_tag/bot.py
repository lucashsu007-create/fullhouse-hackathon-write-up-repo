"""
trap_tag — Fullhouse test opponent.

Archetype: a trapper / slowplayer. Plays tight preflop, but with STRONG hands it
frequently checks and calls (or check-raises) instead of betting out — so an
opponent who barrels into it gets punished. The point of this bot is to be a
hand that disguises strength: it calls and check-raises rather than folding,
which a classifier should NOT read as a passive folder.

Behaviorally it produces: a healthy call_vs_bet and raise_vs_bet (especially the
check-raise), low fold_vs_bet when it has anything, and it traps rather than
folds — the opposite of overfolding.

Strategy (spot-seeded RNG for reproducible mixed lines):
  preflop:
    strong (TT+, AK, AQ)   -> often just CALL/limp to trap, sometimes raise
    medium (pairs/broadway) -> call cheap, else fold
    junk                    -> check/fold
  postflop:
    strong made hand:
        - facing a bet -> mostly CALL, sometimes CHECK-RAISE (the trap)
        - checked to   -> often CHECK (slowplay), sometimes value bet
    medium made -> check-call reasonable prices
    draw        -> call good prices
    air         -> check / fold

Legal/safe: stdlib only, no file/network, always returns a valid action.
"""

import random
import zlib

BOT_NAME = "TrapTAG"
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


def _preflop_tier(cards):
    try:
        r1, s1 = cards[0][0], cards[0][1]
        r2, s2 = cards[1][0], cards[1][1]
    except (IndexError, TypeError):
        return "junk"
    v1, v2 = _VAL.get(r1, 0), _VAL.get(r2, 0)
    hi, lo = max(v1, v2), min(v1, v2)
    pair = r1 == r2
    suited = s1 == s2
    if pair and hi >= 10:
        return "strong"
    if hi == 14 and lo >= 12:
        return "strong"
    if pair or (hi >= 12 and lo >= 10) or (hi == 14 and suited):
        return "medium"
    return "junk"


def _made_strength(hole, board):
    try:
        cards = hole + board
        ranks = [c[0] for c in cards]
        suits = [c[1] for c in cards]
        hole_vals = sorted((_VAL.get(c[0], 0) for c in hole), reverse=True)
        board_vals = [_VAL.get(c[0], 0) for c in board]
    except (TypeError, IndexError):
        return "air"
    counts = {}
    for r in ranks:
        counts[r] = counts.get(r, 0) + 1
    cvals = sorted(counts.values(), reverse=True)
    suit_counts = {}
    for s in suits:
        suit_counts[s] = suit_counts.get(s, 0) + 1
    uniq = sorted(set(_VAL.get(r, 0) for r in ranks))
    if 14 in uniq:
        uniq = sorted(set(uniq + [1]))
    if (cvals and cvals[0] >= 3) or cvals.count(2) >= 2 \
            or _has_run(uniq, 5) or max(suit_counts.values(), default=0) >= 5:
        return "strong"
    if cvals and cvals[0] == 2:
        paired = next((_VAL.get(r, 0) for r, c in counts.items() if c == 2), 0)
        top_board = max(board_vals) if board_vals else 0
        if hole_vals and hole_vals[0] == hole_vals[1] and hole_vals[0] > top_board:
            return "strong"
        if paired >= top_board and hole_vals and hole_vals[0] >= 12:
            return "strong"
        return "medium"
    # draw as a 'medium' continue
    flush_draw = max(suit_counts.values(), default=0) == 4
    if flush_draw or (_has_run(uniq, 4) and not _has_run(uniq, 5)):
        return "draw"
    return "air"


def _has_run(sorted_vals, length):
    if len(sorted_vals) < length:
        return False
    run = 1
    for i in range(1, len(sorted_vals)):
        if sorted_vals[i] == sorted_vals[i - 1] + 1:
            run += 1
            if run >= length:
                return True
        else:
            run = 1
    return False


def _price(game_state):
    owed = game_state.get("amount_owed", 0)
    pot = game_state.get("pot", 0)
    if owed <= 0:
        return 0.0
    return owed / (pot + owed)


def decide(game_state: dict) -> dict:
    try:
        if not isinstance(game_state, dict):
            return {"action": "fold"}
        if game_state.get("type") == "warmup":
            return {"action": "fold"}

        street = game_state.get("street", "preflop")
        can_check = game_state.get("can_check", False)
        hole = game_state.get("your_cards", []) or []
        board = game_state.get("community_cards", []) or []
        stack = game_state.get("your_stack", 0)
        my_bet = game_state.get("your_bet_this_street", 0)
        min_to = game_state.get("min_raise_to", 0)
        pot = game_state.get("pot", 0)
        rng = _spot_rng(game_state)

        if street == "preflop":
            tier = _preflop_tier(hole)
            if tier == "strong":
                # Trap: often just call/limp to disguise, sometimes raise.
                if can_check:
                    return {"action": "check"}
                if rng.random() < 0.55:
                    return {"action": "call"}            # flat the raise (trap)
                target = min(min_to * 3, stack + my_bet)
                return {"action": "raise", "amount": max(target, min_to)}
            if tier == "medium":
                if can_check:
                    return {"action": "check"}
                return {"action": "call"} if _price(game_state) <= 0.28 else {"action": "fold"}
            return {"action": "check"} if can_check else {"action": "fold"}

        # Postflop
        strength = _made_strength(hole, board)
        price = _price(game_state)

        if strength == "strong":
            if can_check:
                # Slowplay: usually check the monster, occasionally value bet.
                if rng.random() < 0.7:
                    return {"action": "check"}
                bet = min(int(pot * 0.6) + min_to, stack + my_bet)
                return {"action": "raise", "amount": max(bet, min_to)}
            # Facing a bet with a strong hand: mostly call, sometimes
            # CHECK-RAISE-style reraise to punish aggression (the trap).
            if rng.random() < 0.35:
                rz = min(int(pot * 1.0) + min_to + my_bet, stack + my_bet)
                return {"action": "raise", "amount": max(rz, min_to)}
            return {"action": "call"}

        if strength == "medium":
            if can_check:
                return {"action": "check"}
            return {"action": "call"} if price <= 0.38 else {"action": "fold"}

        if strength == "draw":
            if can_check:
                return {"action": "check"}
            return {"action": "call"} if price <= 0.33 else {"action": "fold"}

        # air
        return {"action": "check"} if can_check else {"action": "fold"}

    except Exception:
        return {"action": "fold"}
