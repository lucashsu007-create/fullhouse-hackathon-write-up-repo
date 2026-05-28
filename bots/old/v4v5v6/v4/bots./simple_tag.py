"""
simple_tag — Fullhouse test opponent.

Archetype: a tight-aggressive, value-heavy baseline. Honest aggression with
strong hands, folds the junk, low bluff. Target for the 'simple_tag' /
'rule_shark' read and the 'tag' behavior read.

This is intentionally a compact, readable TAG (NOT the full SafeTAG bot) so it
serves as a clean labelled opponent rather than a competitor to the hero.

Strategy (deterministic — no RNG):
  preflop:
    strong (premium pairs/aces, AK/AQ)  -> raise
    medium (broadway / pairs / suited A) -> call a cheap price, else fold
    junk                                 -> check if free, else fold
  postflop:
    strong made hand   -> value bet / call
    medium pair        -> check-call a small price
    nothing            -> check / fold

Legal/safe: stdlib only, no file/network, always returns a valid action.
"""

BOT_NAME = "SimpleTAG"
BOT_AVATAR = "robot_1"

_RANKS = "23456789TJQKA"
_VAL = {r: i for i, r in enumerate(_RANKS, start=2)}


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
    if pair and hi >= 10:          # TT+
        return "strong"
    if hi == 14 and lo >= 12:      # AK, AQ
        return "strong"
    if pair:                       # any pocket pair
        return "medium"
    if hi >= 12 and lo >= 10:      # two broadway (KQ, KJ, QJ, AT...)
        return "medium"
    if hi == 14 and suited:        # suited ace
        return "medium"
    return "junk"


def _made_strength(hole, board):
    """Return 'strong' (>= top pair good kicker / overpair), 'medium' (any pair),
    or 'weak'."""
    try:
        cards = hole + board
        ranks = [c[0] for c in cards]
    except (TypeError, IndexError):
        return "weak"
    counts = {}
    for r in ranks:
        counts[r] = counts.get(r, 0) + 1
    cvals = sorted(counts.values(), reverse=True)
    if (cvals and cvals[0] >= 3) or cvals.count(2) >= 2:
        return "strong"
    if cvals and cvals[0] == 2:
        try:
            board_vals = [_VAL.get(c[0], 0) for c in board]
            hole_vals = sorted((_VAL.get(c[0], 0) for c in hole), reverse=True)
        except (TypeError, IndexError):
            return "medium"
        paired = next((_VAL.get(r, 0) for r, c in counts.items() if c == 2), 0)
        top_board = max(board_vals) if board_vals else 0
        if hole_vals and hole_vals[0] == hole_vals[1] and hole_vals[0] > top_board:
            return "strong"                       # overpair
        if paired >= top_board and hole_vals and hole_vals[0] >= 12:
            return "strong"                       # top pair, Q+ kicker
        return "medium"
    return "weak"


def _cheap(game_state, frac):
    pot = game_state.get("pot", 0)
    owed = game_state.get("amount_owed", 0)
    return owed <= pot * frac


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

        if street == "preflop":
            tier = _preflop_tier(hole)
            if tier == "strong":
                target = min(min_to * 3, stack + my_bet)
                return {"action": "raise", "amount": max(target, min_to)}
            if tier == "medium":
                if can_check:
                    return {"action": "check"}
                return {"action": "call"} if _cheap(game_state, 0.15) else {"action": "fold"}
            return {"action": "check"} if can_check else {"action": "fold"}

        # Postflop
        strength = _made_strength(hole, board)
        if strength == "strong":
            if can_check:
                bet = min(int(pot * 0.66), stack + my_bet)
                return {"action": "raise", "amount": max(bet, min_to)}
            return {"action": "call"}
        if strength == "medium":
            if can_check:
                return {"action": "check"}
            return {"action": "call"} if _cheap(game_state, 0.25) else {"action": "fold"}
        # weak
        return {"action": "check"} if can_check else {"action": "fold"}

    except Exception:
        return {"action": "fold"}
