"""
nit_folder — Fullhouse test opponent.

Archetype: the extreme "nit". Folds far too much, only continues with strong
holdings, almost never bluffs. Target for the 'nit' / 'folding_bot' read:
fold_vs_bet very high, low VPIP/PFR, tight continuance.

Strategy (deterministic — no RNG):
  - preflop: only play premium pairs/aces & AK; raise them, fold everything else
  - postflop: only continue with a strong made hand (top pair top kicker or
    better, by a cheap heuristic); otherwise check-or-fold
  - facing a bet without a strong hand -> fold

Legal/safe: stdlib only, no file/network, always returns a valid action.
"""

BOT_NAME = "NitFolder"
BOT_AVATAR = "robot_1"

_RANKS = "23456789TJQKA"
_VAL = {r: i for i, r in enumerate(_RANKS, start=2)}


def _is_premium(cards):
    try:
        r1, r2 = cards[0][0], cards[1][0]
    except (IndexError, TypeError):
        return False
    v1, v2 = _VAL.get(r1, 0), _VAL.get(r2, 0)
    hi, lo = max(v1, v2), min(v1, v2)
    pair = r1 == r2
    # Premium only: TT+ , AK, AQ.
    if pair and hi >= 10:
        return True
    if hi == 14 and lo >= 12:   # AK, AQ
        return True
    return False


def _strong_made(hole, board):
    """Cheap 'is this at least top pair, strong kicker / overpair or better'."""
    try:
        cards = hole + board
        ranks = [c[0] for c in cards]
    except (TypeError, IndexError):
        return False
    counts = {}
    for r in ranks:
        counts[r] = counts.get(r, 0) + 1
    cvals = sorted(counts.values(), reverse=True)
    # two pair or better (set, trips, full house, quads) -> strong
    if cvals and cvals[0] >= 3:
        return True
    if cvals.count(2) >= 2:
        return True
    # one pair: only strong if it's an overpair or top pair with high kicker
    if cvals and cvals[0] == 2:
        try:
            board_vals = [_VAL.get(c[0], 0) for c in board]
            hole_vals = sorted((_VAL.get(c[0], 0) for c in hole), reverse=True)
        except (TypeError, IndexError):
            return False
        paired = next((_VAL.get(r, 0) for r, c in counts.items() if c == 2), 0)
        top_board = max(board_vals) if board_vals else 0
        # overpair (pocket pair above board) or top pair with A/K kicker
        if hole_vals and hole_vals[0] == hole_vals[1] and hole_vals[0] > top_board:
            return True
        if paired >= top_board and hole_vals and hole_vals[0] >= 13:
            return True
    return False


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

        if street == "preflop":
            if _is_premium(hole):
                # Raise premiums to ~3x the min legal raise total.
                target = min(min_to * 3, stack + my_bet)
                target = max(target, min_to)
                return {"action": "raise", "amount": target}
            # Non-premium: take a free check if possible, else fold.
            return {"action": "check"} if can_check else {"action": "fold"}

        # Postflop: only continue with a strong made hand.
        strong = _strong_made(hole, board)
        if can_check:
            if strong:
                bet = min(int(game_state.get("pot", 0) * 0.6), stack + my_bet)
                bet = max(bet, min_to)
                return {"action": "raise", "amount": bet}
            return {"action": "check"}
        # Facing a bet: continue only when strong, otherwise fold (the nit leak).
        return {"action": "call"} if strong else {"action": "fold"}

    except Exception:
        return {"action": "fold"}
