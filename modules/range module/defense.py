"""
Preflop defense ranges.

When someone has raised before us, we need to choose between 3-bet, call,
or fold. The decision depends on:
    - Opener's position (early = tight, late = loose)
    - Our position (in-position vs OOP changes calling threshold)
    - Stack depth (only 100bb covered here)

We approximate with three "opener profiles":
    EARLY  : UTG, UTG1 — tight range, mostly value
    MIDDLE : LJ, HJ    — medium
    LATE   : CO, BTN   — wide, often steals
And three defender profiles based on relative position:
    IP     : we're in position vs the opener
    OOP    : we're out of position
    BB     : closing the action with a pot-odds discount

The result is six (opener × defender) tables. Each table maps hand →
{"3bet": freq, "call": freq, "fold": freq}.

These are simplified versus a full solver — they're meant to play sensibly
and avoid being exploited too easily by aggression. Plug in proper solver
outputs when you have them.
"""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _d(three_bet=0.0, call=0.0):
    """Build an action distribution. Whatever's left is fold."""
    fold = max(0.0, 1.0 - three_bet - call)
    return {"3bet": three_bet, "call": call, "fold": fold}


# ---------------------------------------------------------------------------
# vs EARLY position opener (UTG / UTG1 in 8-max, UTG in 6-max)
# Opener's range is tight, so we play tight back.
# ---------------------------------------------------------------------------
VS_EARLY_IP = {
    # Always 3bet for value
    "AA": _d(1.0, 0.0), "KK": _d(1.0, 0.0),
    "QQ": _d(0.85, 0.15), "JJ": _d(0.30, 0.70),
    "AKs": _d(0.80, 0.20), "AKo": _d(0.80, 0.20),
    "AQs": _d(0.30, 0.70),
    # Calls — set-mining and broadways
    "TT": _d(0.0, 1.0), "99": _d(0.0, 1.0), "88": _d(0.0, 1.0),
    "77": _d(0.0, 1.0), "66": _d(0.0, 0.8), "55": _d(0.0, 0.5),
    "AQo": _d(0.0, 0.85), "AJs": _d(0.0, 1.0), "ATs": _d(0.0, 0.8),
    "KQs": _d(0.0, 1.0), "KJs": _d(0.0, 0.7),
    "QJs": _d(0.0, 0.6), "JTs": _d(0.0, 0.7),
    "T9s": _d(0.0, 0.5), "98s": _d(0.0, 0.3),
    # Bluffs
    "A5s": _d(0.25, 0.0), "A4s": _d(0.20, 0.0),
}

VS_EARLY_OOP = {
    "AA": _d(1.0, 0.0), "KK": _d(1.0, 0.0),
    "QQ": _d(0.95, 0.05), "JJ": _d(0.45, 0.55),
    "AKs": _d(0.85, 0.15), "AKo": _d(0.85, 0.15),
    "AQs": _d(0.50, 0.50),
    # OOP we call less, fold more
    "TT": _d(0.0, 0.90), "99": _d(0.0, 0.7), "88": _d(0.0, 0.5),
    "77": _d(0.0, 0.4), "66": _d(0.0, 0.3),
    "AQo": _d(0.0, 0.4), "AJs": _d(0.0, 0.6),
    "KQs": _d(0.0, 0.6), "KJs": _d(0.0, 0.3),
    "QJs": _d(0.0, 0.3), "JTs": _d(0.0, 0.3),
    # Bluffs (carefully — OOP bluffs are expensive)
    "A5s": _d(0.20, 0.0),
}

VS_EARLY_BB = {
    # Closing action — call wider, 3-bet less (more passive)
    "AA": _d(1.0, 0.0), "KK": _d(1.0, 0.0),
    "QQ": _d(0.80, 0.20), "JJ": _d(0.30, 0.70),
    "TT": _d(0.0, 1.0), "99": _d(0.0, 1.0), "88": _d(0.0, 1.0),
    "77": _d(0.0, 1.0), "66": _d(0.0, 1.0), "55": _d(0.0, 0.9),
    "44": _d(0.0, 0.7), "33": _d(0.0, 0.5), "22": _d(0.0, 0.4),
    "AKs": _d(0.70, 0.30), "AKo": _d(0.60, 0.40),
    "AQs": _d(0.30, 0.70), "AQo": _d(0.0, 0.9),
    "AJs": _d(0.0, 1.0), "AJo": _d(0.0, 0.7),
    "ATs": _d(0.0, 1.0), "A9s": _d(0.0, 0.8), "A8s": _d(0.0, 0.5),
    "A5s": _d(0.20, 0.4), "A4s": _d(0.0, 0.4),
    "KQs": _d(0.0, 1.0), "KQo": _d(0.0, 0.6),
    "KJs": _d(0.0, 0.9), "KTs": _d(0.0, 0.7),
    "QJs": _d(0.0, 0.9), "QTs": _d(0.0, 0.5),
    "JTs": _d(0.0, 0.9), "T9s": _d(0.0, 0.7),
    "98s": _d(0.0, 0.5), "87s": _d(0.0, 0.4), "76s": _d(0.0, 0.3),
}


# ---------------------------------------------------------------------------
# vs MIDDLE position opener (LJ, HJ)
# Opener's range a touch wider — we widen too.
# ---------------------------------------------------------------------------
VS_MIDDLE_IP = {
    "AA": _d(1.0, 0.0), "KK": _d(1.0, 0.0),
    "QQ": _d(0.90, 0.10), "JJ": _d(0.50, 0.50),
    "TT": _d(0.20, 0.75),
    "AKs": _d(0.80, 0.20), "AKo": _d(0.80, 0.20),
    "AQs": _d(0.50, 0.50), "AQo": _d(0.20, 0.65),
    "AJs": _d(0.10, 0.85),
    "99": _d(0.0, 1.0), "88": _d(0.0, 1.0), "77": _d(0.0, 1.0),
    "66": _d(0.0, 0.9), "55": _d(0.0, 0.7), "44": _d(0.0, 0.5),
    "ATs": _d(0.0, 1.0), "A9s": _d(0.0, 0.5),
    "KQs": _d(0.0, 1.0), "KJs": _d(0.0, 0.9), "KTs": _d(0.0, 0.6),
    "KQo": _d(0.0, 0.6),
    "QJs": _d(0.0, 0.9), "QTs": _d(0.0, 0.6),
    "JTs": _d(0.0, 0.9), "T9s": _d(0.0, 0.7),
    "98s": _d(0.0, 0.5), "87s": _d(0.0, 0.4),
    # Bluffs
    "A5s": _d(0.40, 0.0), "A4s": _d(0.30, 0.0), "A3s": _d(0.20, 0.0),
    "K9s": _d(0.20, 0.0),
}

VS_MIDDLE_OOP = {
    "AA": _d(1.0, 0.0), "KK": _d(1.0, 0.0),
    "QQ": _d(0.95, 0.05), "JJ": _d(0.65, 0.35),
    "TT": _d(0.30, 0.50),
    "AKs": _d(0.85, 0.15), "AKo": _d(0.85, 0.15),
    "AQs": _d(0.60, 0.30), "AQo": _d(0.30, 0.40),
    "99": _d(0.0, 0.7), "88": _d(0.0, 0.5),
    "77": _d(0.0, 0.4), "66": _d(0.0, 0.3),
    "AJs": _d(0.0, 0.5),
    "KQs": _d(0.0, 0.6), "KJs": _d(0.0, 0.3),
    "QJs": _d(0.0, 0.3), "JTs": _d(0.0, 0.3),
    "A5s": _d(0.30, 0.0), "A4s": _d(0.20, 0.0),
}

VS_MIDDLE_BB = {
    "AA": _d(1.0, 0.0), "KK": _d(1.0, 0.0),
    "QQ": _d(0.85, 0.15), "JJ": _d(0.45, 0.55),
    "TT": _d(0.0, 1.0), "99": _d(0.0, 1.0), "88": _d(0.0, 1.0),
    "77": _d(0.0, 1.0), "66": _d(0.0, 1.0), "55": _d(0.0, 1.0),
    "44": _d(0.0, 0.9), "33": _d(0.0, 0.7), "22": _d(0.0, 0.6),
    "AKs": _d(0.75, 0.25), "AKo": _d(0.65, 0.35),
    "AQs": _d(0.40, 0.60), "AQo": _d(0.10, 0.85),
    "AJs": _d(0.0, 1.0), "AJo": _d(0.0, 0.85),
    "ATs": _d(0.0, 1.0), "ATo": _d(0.0, 0.7),
    "A9s": _d(0.0, 0.95), "A8s": _d(0.0, 0.7),
    "A7s": _d(0.0, 0.6), "A6s": _d(0.0, 0.5),
    "A5s": _d(0.30, 0.5), "A4s": _d(0.20, 0.5), "A3s": _d(0.0, 0.4),
    "KQs": _d(0.0, 1.0), "KQo": _d(0.0, 0.8),
    "KJs": _d(0.0, 0.95), "KJo": _d(0.0, 0.5),
    "KTs": _d(0.0, 0.9), "K9s": _d(0.0, 0.6),
    "QJs": _d(0.0, 1.0), "QTs": _d(0.0, 0.8), "Q9s": _d(0.0, 0.4),
    "JTs": _d(0.0, 1.0), "T9s": _d(0.0, 0.9),
    "98s": _d(0.0, 0.7), "87s": _d(0.0, 0.6),
    "76s": _d(0.0, 0.5), "65s": _d(0.0, 0.4), "54s": _d(0.0, 0.3),
}


# ---------------------------------------------------------------------------
# vs LATE position opener (CO, BTN)
# Opener has a wide range — we widen aggressively and add bluffs.
# ---------------------------------------------------------------------------
VS_LATE_IP = {
    "AA": _d(1.0, 0.0), "KK": _d(1.0, 0.0),
    "QQ": _d(0.95, 0.05), "JJ": _d(0.75, 0.25),
    "TT": _d(0.50, 0.40),
    "AKs": _d(0.80, 0.20), "AKo": _d(0.80, 0.20),
    "AQs": _d(0.65, 0.30), "AQo": _d(0.45, 0.45),
    "AJs": _d(0.35, 0.60), "AJo": _d(0.20, 0.55),
    "99": _d(0.20, 0.70), "88": _d(0.10, 0.80),
    "77": _d(0.0, 0.85), "66": _d(0.0, 0.7),
    "ATs": _d(0.10, 0.85), "ATo": _d(0.0, 0.45),
    "KQs": _d(0.30, 0.60), "KQo": _d(0.20, 0.50),
    "KJs": _d(0.20, 0.60), "KTs": _d(0.0, 0.75),
    "QJs": _d(0.15, 0.65), "QTs": _d(0.0, 0.65),
    "JTs": _d(0.10, 0.75), "T9s": _d(0.0, 0.65),
    "98s": _d(0.0, 0.5), "87s": _d(0.0, 0.4), "76s": _d(0.0, 0.3),
    # Bluffs — suited aces & connectors as light 3-bets
    "A5s": _d(0.70, 0.0), "A4s": _d(0.55, 0.0),
    "A3s": _d(0.45, 0.0), "A2s": _d(0.30, 0.0),
    "K9s": _d(0.30, 0.0), "Q9s": _d(0.20, 0.0),
    "J9s": _d(0.15, 0.0), "T8s": _d(0.10, 0.0),
}

VS_LATE_OOP = {
    "AA": _d(1.0, 0.0), "KK": _d(1.0, 0.0),
    "QQ": _d(1.0, 0.0), "JJ": _d(0.85, 0.15),
    "TT": _d(0.55, 0.30),
    "AKs": _d(0.90, 0.10), "AKo": _d(0.85, 0.15),
    "AQs": _d(0.75, 0.20), "AQo": _d(0.55, 0.30),
    "AJs": _d(0.40, 0.30), "AJo": _d(0.25, 0.30),
    "99": _d(0.25, 0.45), "88": _d(0.15, 0.45),
    "77": _d(0.0, 0.55), "66": _d(0.0, 0.4),
    "ATs": _d(0.0, 0.55), "ATo": _d(0.0, 0.20),
    "KQs": _d(0.30, 0.45), "KQo": _d(0.20, 0.30),
    "KJs": _d(0.20, 0.40), "QJs": _d(0.15, 0.40),
    "JTs": _d(0.10, 0.45), "T9s": _d(0.0, 0.35),
    # Bluffs (a bit fewer OOP)
    "A5s": _d(0.55, 0.0), "A4s": _d(0.45, 0.0),
    "A3s": _d(0.30, 0.0), "K9s": _d(0.20, 0.0),
}

VS_LATE_BB = {
    "AA": _d(1.0, 0.0), "KK": _d(1.0, 0.0),
    "QQ": _d(0.95, 0.05), "JJ": _d(0.65, 0.35),
    "TT": _d(0.30, 0.65),
    "AKs": _d(0.80, 0.20), "AKo": _d(0.75, 0.25),
    "AQs": _d(0.50, 0.50), "AQo": _d(0.30, 0.65),
    "AJs": _d(0.20, 0.80), "AJo": _d(0.0, 1.0),
    "ATs": _d(0.0, 1.0), "ATo": _d(0.0, 0.95),
    "99": _d(0.0, 1.0), "88": _d(0.0, 1.0), "77": _d(0.0, 1.0),
    "66": _d(0.0, 1.0), "55": _d(0.0, 1.0), "44": _d(0.0, 1.0),
    "33": _d(0.0, 0.95), "22": _d(0.0, 0.9),
    "A9s": _d(0.0, 1.0), "A8s": _d(0.0, 1.0), "A7s": _d(0.0, 1.0),
    "A6s": _d(0.0, 1.0), "A5s": _d(0.50, 0.5), "A4s": _d(0.40, 0.6),
    "A3s": _d(0.25, 0.7), "A2s": _d(0.0, 0.85),
    "A9o": _d(0.0, 0.9), "A8o": _d(0.0, 0.7), "A7o": _d(0.0, 0.5),
    "KQs": _d(0.20, 0.80), "KQo": _d(0.0, 1.0),
    "KJs": _d(0.0, 1.0), "KJo": _d(0.0, 0.9),
    "KTs": _d(0.0, 1.0), "KTo": _d(0.0, 0.7),
    "K9s": _d(0.30, 0.7), "K8s": _d(0.0, 0.8), "K7s": _d(0.0, 0.6),
    "K9o": _d(0.0, 0.5),
    "QJs": _d(0.0, 1.0), "QJo": _d(0.0, 0.85),
    "QTs": _d(0.0, 1.0), "QTo": _d(0.0, 0.6),
    "Q9s": _d(0.20, 0.8), "Q8s": _d(0.0, 0.7),
    "JTs": _d(0.0, 1.0), "JTo": _d(0.0, 0.7),
    "J9s": _d(0.10, 0.85), "J8s": _d(0.0, 0.5),
    "T9s": _d(0.0, 1.0), "T8s": _d(0.0, 0.7),
    "98s": _d(0.0, 1.0), "87s": _d(0.0, 0.95),
    "76s": _d(0.0, 0.85), "65s": _d(0.0, 0.7),
    "54s": _d(0.0, 0.6), "43s": _d(0.0, 0.3),
    "97s": _d(0.0, 0.5), "86s": _d(0.0, 0.4),
}


# ---------------------------------------------------------------------------
# 4-bet defense: facing a 3-bet after we opened.
# Very tight at 100bb: jam AA-QQ and AKs, fold the rest by default.
# (KK gets a tiny call-or-4bet mix to balance.)
# ---------------------------------------------------------------------------
VS_3BET_DEFENSE = {
    "AA": _d(1.0, 0.0), "KK": _d(0.95, 0.05),
    "QQ": _d(0.50, 0.50), "JJ": _d(0.0, 0.85),
    "TT": _d(0.0, 0.65), "99": _d(0.0, 0.40),
    "88": _d(0.0, 0.25),
    "AKs": _d(0.85, 0.15), "AKo": _d(0.50, 0.50),
    "AQs": _d(0.0, 0.85), "AQo": _d(0.0, 0.30),
    "AJs": _d(0.0, 0.60), "ATs": _d(0.0, 0.35),
    "KQs": _d(0.0, 0.55), "KJs": _d(0.0, 0.20),
    "QJs": _d(0.0, 0.20), "JTs": _d(0.0, 0.20),
    # Small bluff 4-bets with blockers
    "A5s": _d(0.15, 0.0), "A4s": _d(0.10, 0.0),
}


# ---------------------------------------------------------------------------
# Routing helpers
# ---------------------------------------------------------------------------
OPENER_TIER = {
    "UTG": "EARLY",  "UTG1": "EARLY",
    "LJ":  "MIDDLE", "HJ":   "MIDDLE",
    "CO":  "LATE",   "BTN":  "LATE",
    "SB":  "LATE",
}


def defender_role(opener_pos, our_pos):
    """Decide which of our defense tables to use given seats.

    - If we're BB and closing the action  -> "BB"
    - If we'll be in position post-flop   -> "IP"
    - Otherwise                            -> "OOP"
    """
    if our_pos == "BB":
        return "BB"

    # Pre-flop seat order from earliest to latest (for action sequence):
    order = ["UTG", "UTG1", "LJ", "HJ", "CO", "BTN", "SB", "BB"]
    try:
        opp_idx = order.index(opener_pos)
        my_idx = order.index(our_pos)
    except ValueError:
        return "OOP"

    # Post-flop, BTN is most IP, SB/BB OOP. If our seat acts AFTER the
    # opener post-flop (closer to BTN cyclically), we're IP.
    # Simple rule: if our_pos is later in the action order pre-flop and
    # we're not in the blinds, we'll be IP post-flop too.
    if our_pos in ("SB", "BB"):
        return "BB" if our_pos == "BB" else "OOP"
    return "IP" if my_idx > opp_idx else "OOP"


# Lookup table
_DEFENSE_TABLES = {
    ("EARLY", "IP"):    VS_EARLY_IP,
    ("EARLY", "OOP"):   VS_EARLY_OOP,
    ("EARLY", "BB"):    VS_EARLY_BB,
    ("MIDDLE", "IP"):   VS_MIDDLE_IP,
    ("MIDDLE", "OOP"):  VS_MIDDLE_OOP,
    ("MIDDLE", "BB"):   VS_MIDDLE_BB,
    ("LATE", "IP"):     VS_LATE_IP,
    ("LATE", "OOP"):    VS_LATE_OOP,
    ("LATE", "BB"):     VS_LATE_BB,
}


def defense_action(opener_pos, our_pos, hand):
    """Return action distribution for our `hand` facing a raise from
    `opener_pos`.

    The returned dict has keys "3bet", "call", "fold" (summing to 1.0). It
    is up to the strategy layer to convert "3bet" into a bet sizing.
    """
    tier = OPENER_TIER.get(opener_pos, "MIDDLE")
    role = defender_role(opener_pos, our_pos)
    table = _DEFENSE_TABLES.get((tier, role), VS_MIDDLE_OOP)
    return table.get(hand, _d(0.0, 0.0))


def four_bet_defense(hand):
    """Return action distribution for `hand` facing a 3-bet after we opened.

    Keys: "3bet" (which here means 4-bet/jam), "call", "fold".
    """
    return VS_3BET_DEFENSE.get(hand, _d(0.0, 0.0))
