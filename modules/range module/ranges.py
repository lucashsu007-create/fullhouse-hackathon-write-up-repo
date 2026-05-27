"""
Preflop GTO ranges — 100bb MTT chipEV, 8-max.

Source: GTO Wizard charts (uploaded). Each position covers the "everyone folded
to me" (RFI) spot, except SB which covers "folded to SB heads-up vs BB."

Encoding:
    Each position maps hand -> {action_name: frequency} where frequencies sum
    to 1.0. Mixed strategies are common — e.g. A8s at UTG raises ~85% of the
    time and folds ~15%. Use `gto.strategy.sample_action()` to draw an action.

Open sizing per the charts:
    UTG     2.1bb
    UTG1    2.1bb
    LJ      2.1bb
    HJ      2.1bb
    CO      2.2bb
    BTN     2.5bb
    SB      3.5bb (when raising)

NOTE ON MIXED FREQUENCIES: these were read by eye from the chart screenshots,
so cells that look clearly "all orange" are encoded at 1.0 and clearly "all
blue" cells are omitted (= 100% fold). Cells with visible blue+orange split
got a frequency estimate. If you want surgical accuracy, paste your own
numbers from the Wizard's export view in here — the schema doesn't change.
"""

from .hand_notation import all_169_hands

# Action sizings — these come from the chart selections, in big blinds.
OPEN_SIZE_BB = {
    "UTG": 2.1,
    "UTG1": 2.1,
    "LJ": 2.1,
    "HJ": 2.1,
    "CO": 2.2,
    "BTN": 2.5,
    "SB": 3.5,
}


def _r(freq=1.0):
    """Helper: raise `freq` of the time, fold the rest."""
    return {"raise": freq, "fold": 1.0 - freq}


def _rc(raise_freq, call_freq):
    """Helper for SB: raise/call/fold mix."""
    fold = max(0.0, 1.0 - raise_freq - call_freq)
    return {"raise": raise_freq, "call": call_freq, "fold": fold}


# ---------------------------------------------------------------------------
# UTG — opens 16.9%
# ---------------------------------------------------------------------------
UTG = {
    # Pairs
    "AA": _r(), "KK": _r(), "QQ": _r(), "JJ": _r(),
    "TT": _r(), "99": _r(), "88": _r(),
    "77": _r(0.95), "66": _r(0.70), "55": _r(0.55),
    "44": _r(0.40), "33": _r(0.35), "22": _r(0.30),
    # Suited aces
    "AKs": _r(), "AQs": _r(), "AJs": _r(), "ATs": _r(), "A9s": _r(),
    "A8s": _r(0.95), "A7s": _r(0.85), "A6s": _r(0.75),
    "A5s": _r(0.95), "A4s": _r(0.85), "A3s": _r(0.75), "A2s": _r(0.60),
    # Offsuit aces
    "AKo": _r(), "AQo": _r(), "AJo": _r(0.95), "ATo": _r(0.60),
    # Kings
    "KQs": _r(), "KJs": _r(), "KTs": _r(0.95),
    "K9s": _r(0.55), "K8s": _r(0.25), "K7s": _r(0.10),
    "KQo": _r(0.85), "KJo": _r(0.40),
    # Queens
    "QJs": _r(), "QTs": _r(0.90), "Q9s": _r(0.40),
    "QJo": _r(0.30),
    # Jacks
    "JTs": _r(), "J9s": _r(0.45), "J8s": _r(0.05),
    # Tens
    "T9s": _r(0.95), "T8s": _r(0.25),
    # Connectors / suited gappers
    "98s": _r(0.75), "87s": _r(0.55), "76s": _r(0.40),
    "65s": _r(0.35), "54s": _r(0.25),
}


# ---------------------------------------------------------------------------
# UTG1 — opens 19.6%
# ---------------------------------------------------------------------------
UTG1 = {
    "AA": _r(), "KK": _r(), "QQ": _r(), "JJ": _r(),
    "TT": _r(), "99": _r(), "88": _r(),
    "77": _r(), "66": _r(0.95), "55": _r(0.80),
    "44": _r(0.65), "33": _r(0.55), "22": _r(0.50),
    "AKs": _r(), "AQs": _r(), "AJs": _r(), "ATs": _r(),
    "A9s": _r(), "A8s": _r(), "A7s": _r(),
    "A6s": _r(0.90), "A5s": _r(), "A4s": _r(0.95),
    "A3s": _r(0.85), "A2s": _r(0.70),
    "AKo": _r(), "AQo": _r(), "AJo": _r(), "ATo": _r(0.70),
    "A9o": _r(0.10),
    "KQs": _r(), "KJs": _r(), "KTs": _r(),
    "K9s": _r(0.75), "K8s": _r(0.40), "K7s": _r(0.20),
    "KQo": _r(0.95), "KJo": _r(0.55), "KTo": _r(0.15),
    "QJs": _r(), "QTs": _r(), "Q9s": _r(0.60),
    "Q8s": _r(0.10),
    "QJo": _r(0.45), "QTo": _r(0.05),
    "JTs": _r(), "J9s": _r(0.65), "J8s": _r(0.10),
    "JTo": _r(0.05),
    "T9s": _r(), "T8s": _r(0.45),
    "98s": _r(0.90), "87s": _r(0.75), "76s": _r(0.55),
    "65s": _r(0.45), "54s": _r(0.35),
    "64s": _r(0.05), "53s": _r(0.05),
}


# ---------------------------------------------------------------------------
# LJ — opens 23.2%
# ---------------------------------------------------------------------------
LJ = {
    "AA": _r(), "KK": _r(), "QQ": _r(), "JJ": _r(),
    "TT": _r(), "99": _r(), "88": _r(), "77": _r(),
    "66": _r(), "55": _r(0.95), "44": _r(0.85),
    "33": _r(0.75), "22": _r(0.70),
    "AKs": _r(), "AQs": _r(), "AJs": _r(), "ATs": _r(),
    "A9s": _r(), "A8s": _r(), "A7s": _r(),
    "A6s": _r(0.95), "A5s": _r(), "A4s": _r(),
    "A3s": _r(0.95), "A2s": _r(0.85),
    "AKo": _r(), "AQo": _r(), "AJo": _r(), "ATo": _r(0.90),
    "A9o": _r(0.30), "A8o": _r(0.05),
    "KQs": _r(), "KJs": _r(), "KTs": _r(),
    "K9s": _r(0.90), "K8s": _r(0.55), "K7s": _r(0.35),
    "K6s": _r(0.15), "K5s": _r(0.05),
    "KQo": _r(), "KJo": _r(0.80), "KTo": _r(0.35),
    "QJs": _r(), "QTs": _r(), "Q9s": _r(0.80),
    "Q8s": _r(0.25), "Q7s": _r(0.05),
    "QJo": _r(0.65), "QTo": _r(0.15),
    "JTs": _r(), "J9s": _r(0.85), "J8s": _r(0.25),
    "JTo": _r(0.10),
    "T9s": _r(), "T8s": _r(0.65), "T7s": _r(0.05),
    "98s": _r(), "87s": _r(0.90), "76s": _r(0.70),
    "65s": _r(0.55), "54s": _r(0.45),
    "64s": _r(0.15), "53s": _r(0.15),
    "43s": _r(0.05),
}


# ---------------------------------------------------------------------------
# HJ — opens 28.5%
# ---------------------------------------------------------------------------
HJ = {
    "AA": _r(), "KK": _r(), "QQ": _r(), "JJ": _r(),
    "TT": _r(), "99": _r(), "88": _r(), "77": _r(),
    "66": _r(), "55": _r(), "44": _r(0.95),
    "33": _r(0.85), "22": _r(0.80),
    "AKs": _r(), "AQs": _r(), "AJs": _r(), "ATs": _r(),
    "A9s": _r(), "A8s": _r(), "A7s": _r(),
    "A6s": _r(), "A5s": _r(), "A4s": _r(),
    "A3s": _r(), "A2s": _r(0.95),
    "AKo": _r(), "AQo": _r(), "AJo": _r(), "ATo": _r(),
    "A9o": _r(0.65), "A8o": _r(0.30), "A7o": _r(0.05),
    "A5o": _r(0.10),
    "KQs": _r(), "KJs": _r(), "KTs": _r(), "K9s": _r(),
    "K8s": _r(0.85), "K7s": _r(0.60), "K6s": _r(0.35),
    "K5s": _r(0.20), "K4s": _r(0.10),
    "KQo": _r(), "KJo": _r(), "KTo": _r(0.65),
    "K9o": _r(0.10),
    "QJs": _r(), "QTs": _r(), "Q9s": _r(),
    "Q8s": _r(0.55), "Q7s": _r(0.20), "Q6s": _r(0.05),
    "QJo": _r(0.85), "QTo": _r(0.45), "Q9o": _r(0.05),
    "JTs": _r(), "J9s": _r(), "J8s": _r(0.65),
    "J7s": _r(0.15),
    "JTo": _r(0.55), "J9o": _r(0.05),
    "T9s": _r(), "T8s": _r(0.90), "T7s": _r(0.30),
    "T9o": _r(0.05),
    "98s": _r(), "97s": _r(0.35),
    "87s": _r(), "86s": _r(0.20),
    "76s": _r(), "75s": _r(0.20),
    "65s": _r(), "64s": _r(0.35),
    "54s": _r(0.85), "53s": _r(0.30),
    "43s": _r(0.30), "42s": _r(0.05),
}


# ---------------------------------------------------------------------------
# CO — opens 37.1%
# ---------------------------------------------------------------------------
CO = {
    "AA": _r(), "KK": _r(), "QQ": _r(), "JJ": _r(),
    "TT": _r(), "99": _r(), "88": _r(), "77": _r(),
    "66": _r(), "55": _r(), "44": _r(), "33": _r(),
    "22": _r(),
    # All suited aces always in
    "AKs": _r(), "AQs": _r(), "AJs": _r(), "ATs": _r(),
    "A9s": _r(), "A8s": _r(), "A7s": _r(),
    "A6s": _r(), "A5s": _r(), "A4s": _r(),
    "A3s": _r(), "A2s": _r(),
    # Offsuit aces — A2o+ all play here
    "AKo": _r(), "AQo": _r(), "AJo": _r(), "ATo": _r(),
    "A9o": _r(), "A8o": _r(0.85), "A7o": _r(0.65),
    "A6o": _r(0.40), "A5o": _r(0.60), "A4o": _r(0.45),
    "A3o": _r(0.30), "A2o": _r(0.20),
    # Suited kings — all play, low ones mixed
    "KQs": _r(), "KJs": _r(), "KTs": _r(), "K9s": _r(),
    "K8s": _r(), "K7s": _r(0.95), "K6s": _r(0.80),
    "K5s": _r(0.65), "K4s": _r(0.45), "K3s": _r(0.35),
    "K2s": _r(0.25),
    # Offsuit kings
    "KQo": _r(), "KJo": _r(), "KTo": _r(0.95),
    "K9o": _r(0.55), "K8o": _r(0.10),
    # Queens — all suited play
    "QJs": _r(), "QTs": _r(), "Q9s": _r(),
    "Q8s": _r(0.95), "Q7s": _r(0.65), "Q6s": _r(0.45),
    "Q5s": _r(0.40), "Q4s": _r(0.25), "Q3s": _r(0.10),
    "Q2s": _r(0.05),
    "QJo": _r(), "QTo": _r(0.85), "Q9o": _r(0.40),
    "Q8o": _r(0.05),
    # Jacks
    "JTs": _r(), "J9s": _r(), "J8s": _r(0.95),
    "J7s": _r(0.55), "J6s": _r(0.10),
    "JTo": _r(0.85), "J9o": _r(0.35), "J8o": _r(0.05),
    # Tens
    "T9s": _r(), "T8s": _r(), "T7s": _r(0.85),
    "T6s": _r(0.20),
    "T9o": _r(0.55), "T8o": _r(0.05),
    # 9s and below — connectors/gappers
    "98s": _r(), "97s": _r(0.85), "96s": _r(0.30),
    "98o": _r(0.05),
    "87s": _r(), "86s": _r(0.75), "85s": _r(0.20),
    "76s": _r(), "75s": _r(0.65), "74s": _r(0.05),
    "65s": _r(), "64s": _r(0.65), "63s": _r(0.05),
    "54s": _r(), "53s": _r(0.55), "52s": _r(0.05),
    "43s": _r(0.65), "42s": _r(0.05),
    "32s": _r(0.20),
}


# ---------------------------------------------------------------------------
# BTN — opens 37.1% with a wider mix and bigger sizing (2.5bb)
# ---------------------------------------------------------------------------
# NOTE: BTN's chart looks similar in raise % to CO but the actual hand set
# is wider — it folds even less from low suited combos. Re-tune if you have
# better data on this specific spot.
BTN = {
    "AA": _r(), "KK": _r(), "QQ": _r(), "JJ": _r(),
    "TT": _r(), "99": _r(), "88": _r(), "77": _r(),
    "66": _r(), "55": _r(), "44": _r(), "33": _r(), "22": _r(),
    # All suited aces
    "AKs": _r(), "AQs": _r(), "AJs": _r(), "ATs": _r(),
    "A9s": _r(), "A8s": _r(), "A7s": _r(),
    "A6s": _r(), "A5s": _r(), "A4s": _r(),
    "A3s": _r(), "A2s": _r(),
    # Offsuit aces
    "AKo": _r(), "AQo": _r(), "AJo": _r(), "ATo": _r(),
    "A9o": _r(), "A8o": _r(0.90), "A7o": _r(0.65),
    "A6o": _r(0.30), "A5o": _r(0.55), "A4o": _r(0.35),
    "A3o": _r(0.20), "A2o": _r(0.10),
    # All suited kings
    "KQs": _r(), "KJs": _r(), "KTs": _r(), "K9s": _r(),
    "K8s": _r(), "K7s": _r(), "K6s": _r(), "K5s": _r(0.90),
    "K4s": _r(0.65), "K3s": _r(0.45), "K2s": _r(0.35),
    "KQo": _r(), "KJo": _r(), "KTo": _r(),
    "K9o": _r(0.55), "K8o": _r(0.10),
    # Queens
    "QJs": _r(), "QTs": _r(), "Q9s": _r(), "Q8s": _r(),
    "Q7s": _r(0.70), "Q6s": _r(0.55), "Q5s": _r(0.50),
    "Q4s": _r(0.30), "Q3s": _r(0.15), "Q2s": _r(0.05),
    "QJo": _r(), "QTo": _r(0.90), "Q9o": _r(0.45),
    "Q8o": _r(0.05),
    # Jacks
    "JTs": _r(), "J9s": _r(), "J8s": _r(),
    "J7s": _r(0.55), "J6s": _r(0.20),
    "JTo": _r(0.90), "J9o": _r(0.40), "J8o": _r(0.05),
    # Tens
    "T9s": _r(), "T8s": _r(), "T7s": _r(0.85),
    "T6s": _r(0.25),
    "T9o": _r(0.50), "T8o": _r(0.05),
    # Connectors / suited gappers
    "98s": _r(), "97s": _r(0.85), "96s": _r(0.30),
    "98o": _r(0.10),
    "87s": _r(), "86s": _r(0.75), "85s": _r(0.20),
    "76s": _r(), "75s": _r(0.65), "74s": _r(0.05),
    "65s": _r(), "64s": _r(0.65), "63s": _r(0.05),
    "54s": _r(), "53s": _r(0.55), "52s": _r(0.05),
    "43s": _r(0.65), "42s": _r(0.05),
    "32s": _r(0.20),
}


# ---------------------------------------------------------------------------
# SB — folded to SB heads-up vs BB. Raise/Call/Fold mix.
# Headline numbers from chart: Raise 9%, Call 81.4%, Fold 9.6%.
# Strategy is heavily limp-based at this stack/format.
# ---------------------------------------------------------------------------
# Encoding: only hands that DON'T do "always call" — everything missing
# defaults to call (since SB limps ~81%, that's the default action). Hands
# listed here override with raise / fold frequencies.
SB_OVERRIDES = {
    # Premium raises
    "AA": _rc(1.0, 0.0), "KK": _rc(1.0, 0.0),
    "QQ": _rc(0.95, 0.05), "JJ": _rc(0.85, 0.15),
    "AKs": _rc(0.95, 0.05), "AKo": _rc(0.95, 0.05),
    "AQs": _rc(0.80, 0.20), "AQo": _rc(0.70, 0.30),
    "AJs": _rc(0.50, 0.50), "ATs": _rc(0.30, 0.70),
    "KQs": _rc(0.40, 0.60),
    # Some semi-bluff raises that are pure-fold-on-3bet?  Not in this chart,
    # left as call.
    # Pure folds — the very weakest hands
    "83o": _rc(0.0, 0.0), "73o": _rc(0.0, 0.0),
    "63o": _rc(0.0, 0.0), "53o": _rc(0.0, 0.0),
    "93o": _rc(0.0, 0.0), "92o": _rc(0.0, 0.0),
    "82o": _rc(0.0, 0.0), "72o": _rc(0.0, 0.0),
    "62o": _rc(0.0, 0.0), "52o": _rc(0.0, 0.0),
    "42o": _rc(0.0, 0.0), "43o": _rc(0.0, 0.0),
    "32o": _rc(0.0, 0.0), "33":  _rc(0.0, 1.0),  # 33 mostly calls
}


def sb_action(hand):
    """SB's policy when folded to it heads-up vs BB."""
    if hand in SB_OVERRIDES:
        return SB_OVERRIDES[hand]
    # Default: call (limp).
    return {"raise": 0.0, "call": 1.0, "fold": 0.0}


# ---------------------------------------------------------------------------
# Position registry — one entry per supported chart.
# ---------------------------------------------------------------------------
RANGES_BY_POSITION = {
    "UTG": UTG,
    "UTG1": UTG1,
    "LJ": LJ,
    "HJ": HJ,
    "CO": CO,
    "BTN": BTN,
}


def get_open_action(position, hand):
    """Look up the unopened-pot action distribution for `hand` at `position`.

    Returns: dict like {"raise": 0.85, "fold": 0.15} (frequencies sum to 1.0).
    For positions we don't have a chart for, returns 100% fold.
    """
    if position == "SB":
        return sb_action(hand)
    table = RANGES_BY_POSITION.get(position)
    if table is None:
        return {"raise": 0.0, "fold": 1.0}
    return table.get(hand, {"raise": 0.0, "fold": 1.0})


def range_size_pct(position):
    """Return the approximate raise percentage of `position`'s RFI range."""
    if position == "SB":
        # 9% raise + 81.4% call from chart
        return 9.0
    table = RANGES_BY_POSITION.get(position)
    if table is None:
        return 0.0
    total_weight = 0.0
    total_combos = 0.0
    for hand in all_169_hands():
        combos = 6 if len(hand) == 2 else (4 if hand[2] == "s" else 12)
        raise_freq = table.get(hand, {}).get("raise", 0.0)
        total_weight += combos * raise_freq
        total_combos += combos
    return 100.0 * total_weight / total_combos
