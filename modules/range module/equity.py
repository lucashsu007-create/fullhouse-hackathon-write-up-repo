"""
Hand-equity estimation via Monte Carlo simulation.

Tries `eval7` first (the tournament environment has it), falls back to
`treys`, and finally to a hard-coded approximate equity table. The API is
the same regardless of backend, so the rest of the bot doesn't care.

Performance budget: ~2000 iterations costs ~50ms on the hackathon hardware,
so calling `equity_vs_random(..., n_iters=2000)` per decision leaves ~1.9s
of margin on the 2-second wall clock.
"""

import random

# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------
_BACKEND = None

try:
    import eval7  # type: ignore
    _BACKEND = "eval7"
except ImportError:
    try:
        from treys import Card as _TCard, Evaluator as _TEval, Deck as _TDeck  # type: ignore
        _BACKEND = "treys"
        _TREYS_EV = _TEval()
        _TREYS_FULL_DECK = _TDeck.GetFullDeck()
    except ImportError:
        _BACKEND = None


# Pre-computed approximate preflop equities vs random (for fallback only —
# values from standard poker references; only used when no evaluator is
# available, e.g. during validator dry runs).
_PREFLOP_EQUITY_APPROX = {
    "AA": 0.85, "KK": 0.82, "QQ": 0.80, "JJ": 0.77, "TT": 0.75,
    "99": 0.72, "88": 0.69, "77": 0.66, "66": 0.63, "55": 0.60,
    "44": 0.57, "33": 0.54, "22": 0.50,
    "AKs": 0.67, "AKo": 0.65, "AQs": 0.66, "AQo": 0.64,
    "AJs": 0.65, "AJo": 0.63, "ATs": 0.64, "ATo": 0.61,
    "KQs": 0.63, "KQo": 0.61, "KJs": 0.62, "KJo": 0.59,
    "QJs": 0.60, "JTs": 0.58, "T9s": 0.54, "98s": 0.51,
    "87s": 0.48, "76s": 0.46, "65s": 0.44, "54s": 0.42,
}


# ---------------------------------------------------------------------------
# Backend-specific helpers
# ---------------------------------------------------------------------------
def _eval7_equity(hole_strs, board_strs, n_opp, n_iters):
    """Monte Carlo equity using eval7."""
    hole = [eval7.Card(c) for c in hole_strs]
    board = [eval7.Card(c) for c in board_strs]
    known = set(hole) | set(board)
    deck = [c for c in eval7.Deck().cards if c not in known]

    needed_board = 5 - len(board)
    wins = ties = 0
    for _ in range(n_iters):
        random.shuffle(deck)
        idx = 0
        opp_hands = []
        for _ in range(n_opp):
            opp_hands.append(deck[idx:idx + 2])
            idx += 2
        runout = deck[idx:idx + needed_board]
        full_board = board + runout
        my_rank = eval7.evaluate(hole + full_board)
        win = True
        tie = False
        for opp in opp_hands:
            opp_rank = eval7.evaluate(opp + full_board)
            if opp_rank > my_rank:
                win = False
                break
            elif opp_rank == my_rank:
                tie = True
        if win and not tie:
            wins += 1
        elif win and tie:
            ties += 1
    return (wins + ties * 0.5) / n_iters


def _treys_equity(hole_strs, board_strs, n_opp, n_iters):
    """Monte Carlo equity using treys (lower rank == better)."""
    hole = [_TCard.new(_treys_card(c)) for c in hole_strs]
    board = [_TCard.new(_treys_card(c)) for c in board_strs]
    known = set(hole) | set(board)
    deck = [c for c in _TREYS_FULL_DECK if c not in known]

    needed_board = 5 - len(board)
    wins = ties = 0
    for _ in range(n_iters):
        random.shuffle(deck)
        idx = 0
        opp_hands = []
        for _ in range(n_opp):
            opp_hands.append(deck[idx:idx + 2])
            idx += 2
        runout = deck[idx:idx + needed_board]
        full_board = board + runout
        my_rank = _TREYS_EV.evaluate(full_board, hole)
        win = True
        tie = False
        for opp in opp_hands:
            opp_rank = _TREYS_EV.evaluate(full_board, opp)
            if opp_rank < my_rank:  # treys: smaller is better
                win = False
                break
            elif opp_rank == my_rank:
                tie = True
        if win and not tie:
            wins += 1
        elif win and tie:
            ties += 1
    return (wins + ties * 0.5) / n_iters


def _treys_card(c):
    """Normalize 'As' / 'as' / '7d' to the casing treys wants ('As', '7d')."""
    return c[0].upper() + c[1].lower()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def equity_vs_random(hole_cards, board, n_opponents=1, n_iters=1500):
    """Estimate equity of `hole_cards` vs `n_opponents` random hands.

    Args:
        hole_cards: list of 2 card strings, e.g. ["As", "Kh"].
        board: 0-5 card strings.
        n_opponents: how many random opponents to simulate.
        n_iters: Monte Carlo iterations.

    Returns: equity in [0.0, 1.0]. Falls back to 0.5 (or an approximate
    preflop table value) if no evaluator backend is available.
    """
    if _BACKEND == "eval7":
        return _eval7_equity(hole_cards, board, n_opponents, n_iters)
    if _BACKEND == "treys":
        return _treys_equity(hole_cards, board, n_opponents, n_iters)
    # No backend — return a coarse guess.
    if not board and len(hole_cards) == 2:
        from .hand_notation import canonical
        h = canonical(hole_cards)
        return _PREFLOP_EQUITY_APPROX.get(h, 0.45)
    return 0.5


def hand_strength_class(equity):
    """Bucket an equity number into a coarse strength class.

    Returns one of: 'monster', 'strong', 'medium', 'weak_made', 'draw',
    'air'. Useful for branching in the postflop logic without writing a
    bunch of magic numbers inline.
    """
    if equity >= 0.85:
        return "monster"
    if equity >= 0.65:
        return "strong"
    if equity >= 0.50:
        return "medium"
    if equity >= 0.35:
        return "weak_made"
    if equity >= 0.22:
        return "draw"
    return "air"


def backend_name():
    """Return the active evaluator backend (debugging)."""
    return _BACKEND or "none"
