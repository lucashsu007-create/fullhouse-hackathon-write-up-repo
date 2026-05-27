"""
Short-stack push/fold strategy.

In MTTs, when effective stacks drop below ~20 BB, open-jamming dominates
min-raising because there's no fold equity for any smaller sizing. Below
~12 BB, calling jams becomes the main decision instead of raising.

This module encodes simplified Nash-style push/fold tables for:
    - Open-jamming when first-in
    - Calling an all-in raise

Tables are indexed by effective stack depth (BB) and position. Each entry
is a set of hands that should jam / call at that depth or shorter. We
linearly widen as stacks shrink.

These tables are approximations — they're tighter than full Nash to leave
margin for non-ICM ChipEV play, but loose enough to attack short-stack
spots where most opponents will be too passive.
"""

# Hands listed are inclusive — we jam THIS hand and everything stronger
# in the same family. Each set is checked directly though, so just list
# what we want.

# ---------------------------------------------------------------------------
# OPEN-JAM (we're first in)
# ---------------------------------------------------------------------------
# At ~20bb we jam tight; we get progressively looser as stack shrinks.

JAM_20BB = {
    "UTG": {
        "AA", "KK", "QQ", "JJ", "TT", "99", "88",
        "AKs", "AKo", "AQs", "AQo", "AJs",
    },
    "HJ": {
        "AA", "KK", "QQ", "JJ", "TT", "99", "88", "77", "66",
        "AKs", "AKo", "AQs", "AQo", "AJs", "AJo", "ATs", "KQs",
    },
    "CO": {
        "AA", "KK", "QQ", "JJ", "TT", "99", "88", "77", "66", "55",
        "AKs", "AKo", "AQs", "AQo", "AJs", "AJo", "ATs", "ATo",
        "A9s", "A8s", "A7s", "A5s", "KQs", "KQo", "KJs", "QJs",
    },
    "BTN": {
        "AA", "KK", "QQ", "JJ", "TT", "99", "88", "77", "66", "55", "44", "33", "22",
        "AKs", "AKo", "AQs", "AQo", "AJs", "AJo", "ATs", "ATo", "A9s", "A9o",
        "A8s", "A8o", "A7s", "A6s", "A5s", "A4s", "A3s", "A2s",
        "KQs", "KQo", "KJs", "KJo", "KTs", "KTo", "K9s", "K8s",
        "QJs", "QJo", "QTs", "Q9s", "JTs", "J9s", "T9s", "98s", "87s",
    },
    "SB": {
        "AA", "KK", "QQ", "JJ", "TT", "99", "88", "77", "66", "55", "44", "33", "22",
        "AKs", "AKo", "AQs", "AQo", "AJs", "AJo", "ATs", "ATo", "A9s", "A9o",
        "A8s", "A8o", "A7s", "A7o", "A6s", "A5s", "A5o", "A4s", "A3s", "A2s",
        "KQs", "KQo", "KJs", "KJo", "KTs", "KTo", "K9s", "K9o", "K8s", "K7s",
        "QJs", "QJo", "QTs", "QTo", "Q9s", "Q8s",
        "JTs", "JTo", "J9s", "T9s", "T8s", "98s", "87s", "76s", "65s",
    },
}

JAM_12BB = {
    # At 12bb we widen substantially.
    "UTG":  JAM_20BB["UTG"] | {"77", "66", "AJo", "KQs"},
    "HJ":   JAM_20BB["HJ"]  | {"55", "44", "ATo", "KQo", "KJs", "QJs"},
    "CO":   JAM_20BB["CO"]  | {"44", "33", "22", "KJo", "KTs", "K9s", "QJo", "QTs", "JTs", "T9s"},
    "BTN":  JAM_20BB["BTN"] | {"KTo", "K9o", "K8s", "K7s", "Q8s", "Q7s",
                                "J8s", "T8s", "97s", "86s", "76s", "65s", "54s"},
    "SB":   JAM_20BB["SB"]  | {"K8o", "K7o", "Q8o", "Q7s", "J8s", "T7s", "97s", "86s", "75s", "54s"},
}

JAM_8BB = {
    # At ≤8bb we jam very wide.
    "UTG":  JAM_12BB["UTG"] | {"55", "44", "ATo", "KJs", "QJs"},
    "HJ":   JAM_12BB["HJ"]  | {"33", "22", "A6o", "K9s", "JTs"},
    "CO":   JAM_12BB["CO"]  | {"K7s", "Q8s", "J8s", "T8s", "97s", "76s", "65s"},
    "BTN":  JAM_12BB["BTN"] | {"K6s", "K5s", "Q6s", "J7s", "T7s", "96s", "85s", "75s", "64s", "53s", "43s"},
    "SB":   JAM_12BB["SB"]  | {"K6s", "K5s", "K4s", "Q6s", "Q5s", "Q4s", "J7s", "J6s", "T6s", "96s",
                                "85s", "74s", "63s", "52s", "32s", "43s"},
}


def open_jam_hands(effective_bb, position):
    """Return the set of hands we should open-jam at `effective_bb` from `position`."""
    pos = position if position in JAM_20BB else _map_position(position)
    if effective_bb <= 8:
        return JAM_8BB.get(pos, set())
    if effective_bb <= 12:
        return JAM_12BB.get(pos, set())
    if effective_bb <= 20:
        return JAM_20BB.get(pos, set())
    return set()


# ---------------------------------------------------------------------------
# CALL-A-JAM
# ---------------------------------------------------------------------------
# Calling all-in is fundamentally about pot odds & blockers. These ranges
# are calibrated for facing a typical short-stack jam — they should
# probably tighten vs a known nit and loosen vs a known maniac.

CALL_JAM_VS_LATE = {
    # vs a CO/BTN jam — they're wider, so we call wider.
    "AA", "KK", "QQ", "JJ", "TT", "99", "88", "77",
    "AKs", "AKo", "AQs", "AQo", "AJs", "AJo", "ATs",
    "KQs", "KQo", "KJs", "QJs",
}

CALL_JAM_VS_MIDDLE = {
    # vs HJ/LJ jam — slightly tighter.
    "AA", "KK", "QQ", "JJ", "TT", "99", "88",
    "AKs", "AKo", "AQs", "AQo", "AJs", "ATs",
    "KQs",
}

CALL_JAM_VS_EARLY = {
    # vs UTG/UTG1 jam — very tight.
    "AA", "KK", "QQ", "JJ", "TT",
    "AKs", "AKo", "AQs",
}

CALL_JAM_VS_BLIND = {
    # vs SB jam (or limp+jam) — quite wide.
    "AA", "KK", "QQ", "JJ", "TT", "99", "88", "77", "66", "55",
    "AKs", "AKo", "AQs", "AQo", "AJs", "AJo", "ATs", "ATo", "A9s",
    "KQs", "KQo", "KJs", "KJo", "KTs", "QJs", "QTs", "JTs",
}


def call_jam_range(jammer_position):
    """Set of hands that call an all-in from `jammer_position`."""
    if jammer_position in ("UTG", "UTG1"):
        return CALL_JAM_VS_EARLY
    if jammer_position in ("LJ", "HJ"):
        return CALL_JAM_VS_MIDDLE
    if jammer_position in ("CO", "BTN"):
        return CALL_JAM_VS_LATE
    if jammer_position == "SB":
        return CALL_JAM_VS_BLIND
    return CALL_JAM_VS_MIDDLE


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------
def effective_stack_bb(state):
    """Compute the effective stack in big blinds.

    Uses our stack against the largest opponent stack (since that's the
    most we can lose/win in a single hand). Falls back to our stack alone
    if opponent data isn't available.
    """
    bb = state.get("big_blind") or 1
    my_stack = state.get("your_stack", 0)

    opp_stacks = []
    for p in state.get("players", []):
        if not isinstance(p, dict):
            continue
        if p.get("is_me") or p.get("is_self") or p.get("hero"):
            continue
        if p.get("folded") or p.get("status") == "folded":
            continue
        stack = p.get("stack") or p.get("chips") or 0
        if stack > 0:
            opp_stacks.append(stack)

    if opp_stacks:
        eff = min(my_stack, max(opp_stacks))
    else:
        eff = my_stack
    return eff / bb if bb else eff


def _map_position(pos):
    """Map 6-max-only positions to nearest jam-table key."""
    # We have UTG/HJ/CO/BTN/SB in the tables; 6-max position is one of these
    # already after the chart_position() mapping. Default to BTN if unknown.
    if pos in ("UTG", "UTG1"):
        return "UTG"
    if pos in ("LJ", "HJ", "MP"):
        return "HJ"
    return pos if pos in JAM_20BB else "BTN"
