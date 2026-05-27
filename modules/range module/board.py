"""
Board texture analysis.

A "wet" board has lots of draws (flush draws, straight draws), so even
strong-but-vulnerable hands like top pair should bet bigger to charge
draws. A "dry" board can be c-bet small or even checked back with showdown
value.

We classify each board into a small set of features and a single 0..1
"wetness" score that callers can threshold on.
"""

from .hand_notation import RANK_VAL


def analyze(board):
    """Return a dict describing `board` (a list of 0-5 card strings).

    Keys:
        ncards:        len(board)
        suits:         {"s": int, "h": int, "d": int, "c": int}
        flush_present: True if 5+ of one suit
        flush_draw:    True if 4 of one suit (turn) or 3 of one suit on flop
        backdoor_fd:   True if 2 of one suit on flop only
        monotone:      True if all 3+ board cards same suit
        rainbow:       True if no two cards share a suit
        paired:        True if board has any pair (including trips)
        trips:         True if board has trips
        quads:         True if board has quads
        connected:     "high" / "medium" / "low" — how close ranks are
        straight_draw: True if open-ender / gutshot possible on board
        high_card:     numeric rank value of the highest card (0=2, 12=A)
        wetness:       float in [0, 1] — overall texture coordination
    """
    out = {
        "ncards": len(board),
        "suits": {"s": 0, "h": 0, "d": 0, "c": 0},
        "flush_present": False,
        "flush_draw": False,
        "backdoor_fd": False,
        "monotone": False,
        "rainbow": False,
        "paired": False,
        "trips": False,
        "quads": False,
        "connected": "low",
        "straight_draw": False,
        "high_card": 0,
        "wetness": 0.0,
    }
    if not board:
        return out

    ranks = []
    for c in board:
        r, s = c[0].upper(), c[1].lower()
        out["suits"][s] = out["suits"].get(s, 0) + 1
        ranks.append(RANK_VAL[r])
    ranks.sort()
    out["high_card"] = ranks[-1]

    max_suit = max(out["suits"].values())
    if max_suit >= 5:
        out["flush_present"] = True
    elif max_suit == 4:
        out["flush_draw"] = True
    elif max_suit == 3 and len(board) == 3:
        out["monotone"] = True
        out["flush_draw"] = True
    elif max_suit == 3 and len(board) >= 4:
        out["flush_draw"] = True
    elif max_suit == 2 and len(board) == 3:
        out["backdoor_fd"] = True

    nonzero_suits = sum(1 for c in out["suits"].values() if c > 0)
    if nonzero_suits == len(board) and len(board) >= 3:
        out["rainbow"] = True

    # Pairs / trips / quads
    rank_counts = {}
    for r in ranks:
        rank_counts[r] = rank_counts.get(r, 0) + 1
    counts = sorted(rank_counts.values(), reverse=True)
    if counts and counts[0] == 4:
        out["quads"] = True
        out["paired"] = True
    elif counts and counts[0] == 3:
        out["trips"] = True
        out["paired"] = True
    elif counts and counts[0] == 2:
        out["paired"] = True

    # Connectedness: gap between min and max.
    span = ranks[-1] - ranks[0] if len(ranks) >= 2 else 0
    if span <= 4:
        out["connected"] = "high"
    elif span <= 8:
        out["connected"] = "medium"
    else:
        out["connected"] = "low"

    # Straight-draw potential: any 3 cards within a 5-rank window?
    if len(ranks) >= 3:
        sr = sorted(set(ranks))
        for i in range(len(sr) - 2):
            if sr[i + 2] - sr[i] <= 4:
                out["straight_draw"] = True
                break

    # Wetness score: combine the above signals.
    w = 0.0
    if out["monotone"]:
        w += 0.45
    elif out["flush_draw"]:
        w += 0.35
    elif out["backdoor_fd"]:
        w += 0.10
    if out["straight_draw"]:
        w += 0.25
    if out["connected"] == "high":
        w += 0.20
    elif out["connected"] == "medium":
        w += 0.05
    if out["paired"]:
        w -= 0.05  # paired boards usually play "dryer" for ranges
    out["wetness"] = max(0.0, min(1.0, w))

    return out
