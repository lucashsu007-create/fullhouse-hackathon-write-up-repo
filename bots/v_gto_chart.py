"""
v_gto_chart — chart-based GTO bot, engine-compatible port of useful_bot v2.

================================================================================
WHAT THIS IS

A drop-in candidate for paired A/B testing vs v4.1. Same submission format,
same engine schema, same spot-seeded determinism — so backtest.py ab can
compare it cleanly against the SafeTAG baseline.

Strategy layers (high to low priority):
  1. Short-stack push/fold (eff ≤ 20bb)        -> open-jam / call-jam tables
  2. Preflop unopened, ≥ 25bb                   -> GTO Wizard RFI charts
                                                   (UTG/UTG1/LJ/HJ/CO/BTN/SB)
  3. Preflop facing a raise                     -> 3-bet defense charts by
                                                   opener tier (early/mid/late)
                                                   x defender role (IP/OOP/BB)
  4. Preflop facing a 3-bet (we PFR'd)          -> 4-bet defense
  5. Postflop                                   -> Monte Carlo equity + texture-
                                                   driven sizing / c-bet / bluff

DIFFERENCES FROM v4.1
- Chart-based preflop instead of tier-based ("strong / playable / speculative")
- Real 3-bet defense ranges (incl. bluffs from late position)
- Short-stack push/fold tables (v4.1 doesn't have these)
- Texture-aware bet sizing (33% dry / 66% wet / 85% river-for-value)
- HU override for SB (the 8-max SB chart limps 81% — wrong heads-up)

KNOWN GAPS vs v4.1
- No opponent modelling (collection or exploit). Plays the same vs any field.
- No street-aware equity caching (recomputes per decision).
- Multiway postflop logic is mostly HU-tuned.

================================================================================
ENGINE COMPATIBILITY NOTES (these caused real bugs in the synthetic-state v2)

Player metadata: engine sends `is_folded` (not `folded`); no `is_me` flag;
hero is identified by `seat_to_act`. Dealer/button is NOT in the broadcast
state — we derive it from where the blinds were posted in action_log.

action_log entries: `{seat, action, amount}`. There is no `street` field and
no `player` field — we reconstruct streets like v4.1 does.

big_blind: not in state. We use the engine constant (100).

RNG: must be spot-seeded. The whole point of paired A/B is that variant A and
variant B make decisions on the same cards, in the same seat, with the same
random rolls — only the strategy can differ. random.random() breaks that.
================================================================================
"""

import math
import random
import time
import zlib

try:
    import eval7
    _HAVE_EVAL7 = True
except Exception:                                  # pragma: no cover
    eval7 = None
    _HAVE_EVAL7 = False

BOT_NAME = "GTOChart+EQ"
BOT_AVATAR = "robot_1"

# Engine constants. Hard-coded because the engine doesn't broadcast them.
BIG_BLIND = 100
STARTING_STACK = 10_000

_RANKS = "23456789TJQKA"
_SUITS = "shdc"
_VAL = {r: i for i, r in enumerate(_RANKS, start=2)}

# Equity engine budget (well under the 2s/action wall clock).
_EQ_MIN_ITERS = 100
_EQ_STREET_BUDGET = {
    5: (0.40, 1500),   # preflop  (5 board cards to come)
    3: (0.40, 1500),   # flop
    1: (0.25, 1000),   # turn
    0: (0.15, 800),    # river
}

if _HAVE_EVAL7:
    try:
        _FULL_DECK = [eval7.Card(r + s) for r in _RANKS for s in _SUITS]
        _CARD_STR = {c: str(c) for c in _FULL_DECK}
    except Exception:                              # pragma: no cover
        _FULL_DECK = []
        _CARD_STR = {}
else:
    _FULL_DECK = []
    _CARD_STR = {}


# ===========================================================================
# Spot RNG — required for paired A/B determinism
# ===========================================================================

def _spot_seed(state):
    """Stable 32-bit seed for THIS decision point. Same spot -> same seed
    regardless of what's happened in prior hands or how the global RNG was
    advanced. Without this, paired A/B has way too much noise.

    Uses crc32 instead of built-in hash() (which is salted per process)."""
    try:
        parts = [
            str(state.get("hand_id", "")),
            str(state.get("street", "")),
            str(state.get("seat_to_act", "")),
            "".join(state.get("your_cards", []) or []),
            "".join(state.get("community_cards", []) or []),
            str(len(state.get("action_log", []) or [])),
            str(state.get("current_bet", 0)),
            str(state.get("amount_owed", 0)),
        ]
        return zlib.crc32("|".join(parts).encode("utf-8")) & 0xffffffff
    except Exception:
        return 0


def _spot_rng(state):
    return random.Random(_spot_seed(state))


# ===========================================================================
# Hand notation — ["As", "Kh"] -> "AKo"
# ===========================================================================

def _canonical(cards):
    if len(cards) != 2:
        return None
    c1, c2 = cards[0], cards[1]
    try:
        r1, s1 = c1[0].upper(), c1[1].lower()
        r2, s2 = c2[0].upper(), c2[1].lower()
    except (IndexError, AttributeError):
        return None
    if r1 not in _VAL or r2 not in _VAL:
        return None
    if r1 == r2:
        return r1 + r2
    if _VAL[r1] < _VAL[r2]:
        r1, r2 = r2, r1
        s1, s2 = s2, s1
    return r1 + r2 + ("s" if s1 == s2 else "o")


# ===========================================================================
# Equity engine — Monte Carlo hero vs n random opponents
# ===========================================================================

def equity_vs_random(hole, board, n_opp, rng, time_budget=None, max_iters=None):
    """Monte Carlo equity. Returns float in [0,1] or None on failure."""
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
    deck = [c for c in _FULL_DECK if _CARD_STR.get(c, str(c)) not in known]
    need_board = 5 - len(board_c)
    if need_board < 0:
        return None
    draw_n = 2 * n_opp + need_board
    if draw_n > len(deck):
        return None

    s_time, s_iters = _EQ_STREET_BUDGET.get(need_board, (0.30, 1200))
    budget = time_budget if time_budget is not None else s_time
    cap = max_iters if max_iters is not None else s_iters

    wins = ties = iters = 0
    t0 = time.perf_counter()
    _sample = rng.sample
    _ev = eval7.evaluate

    try:
        while iters < cap:
            if iters >= _EQ_MIN_ITERS and (time.perf_counter() - t0) > budget:
                break
            drawn = _sample(deck, draw_n)
            opp_hands = [drawn[i * 2:i * 2 + 2] for i in range(n_opp)]
            sim_board = board_c + drawn[2 * n_opp:2 * n_opp + need_board]
            hero = _ev(hole_c + sim_board)
            best = max(_ev(oh + sim_board) for oh in opp_hands)
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


def _strength_class(eq):
    if eq is None:
        return "medium"
    if eq >= 0.85:
        return "monster"
    if eq >= 0.65:
        return "strong"
    if eq >= 0.50:
        return "medium"
    if eq >= 0.35:
        return "weak_made"
    if eq >= 0.22:
        return "draw"
    return "air"


# ===========================================================================
# Board texture
# ===========================================================================

def _texture(board):
    """Dict describing the board. `wetness` in [0,1] drives bet sizing."""
    out = {"wetness": 0.0, "monotone": False, "flush_draw": False,
           "paired": False, "straight_draw": False, "dry": True,
           "high_card": 0}
    if not board:
        return out
    suits = {"s": 0, "h": 0, "d": 0, "c": 0}
    ranks = []
    for c in board:
        try:
            r, s = c[0].upper(), c[1].lower()
        except (IndexError, AttributeError):
            continue
        suits[s] = suits.get(s, 0) + 1
        ranks.append(_VAL.get(r, 0))
    if not ranks:
        return out
    ranks.sort()
    out["high_card"] = ranks[-1]

    max_suit = max(suits.values())
    if max_suit >= 5:
        out["wetness"] = max(out["wetness"], 0.5)
    elif max_suit == 4:
        out["flush_draw"] = True
        out["wetness"] += 0.35
    elif max_suit == 3 and len(board) == 3:
        out["monotone"] = True
        out["flush_draw"] = True
        out["wetness"] += 0.45
    elif max_suit == 3 and len(board) >= 4:
        out["flush_draw"] = True
        out["wetness"] += 0.30

    # Pair?
    rcount = {}
    for r in ranks:
        rcount[r] = rcount.get(r, 0) + 1
    if max(rcount.values()) >= 2:
        out["paired"] = True
        out["wetness"] -= 0.05

    # Straight draw potential (any 3 cards within a 5-rank window)
    sr = sorted(set(ranks))
    for i in range(len(sr) - 2):
        if sr[i + 2] - sr[i] <= 4:
            out["straight_draw"] = True
            out["wetness"] += 0.25
            break

    # Connectedness
    span = ranks[-1] - ranks[0]
    if span <= 4 and len(ranks) >= 3:
        out["wetness"] += 0.15

    out["wetness"] = max(0.0, min(1.0, out["wetness"]))
    out["dry"] = out["wetness"] < 0.25
    return out


# ===========================================================================
# Preflop RFI ranges (from GTO Wizard charts; 100bb MTT chipEV 8-max)
# 6-max position mapping: UTG=LJ, HJ=HJ, CO=CO, BTN=BTN, SB=SB
# ===========================================================================

def _r(freq=1.0):
    return {"raise": freq, "fold": 1.0 - freq}


def _rc(rf, cf):
    return {"raise": rf, "call": cf, "fold": max(0.0, 1.0 - rf - cf)}


OPEN_SIZE_BB = {"UTG": 2.1, "UTG1": 2.1, "LJ": 2.1, "HJ": 2.1,
                "CO": 2.2, "BTN": 2.5, "SB": 3.5}

# Compact range encoding: hand -> raise freq (fold = 1 - raise unless noted).
# Hands not listed = 100% fold. These are read from the chart screenshots; they
# match RFI percentages to within 1-4 percentage points of the chart targets.

_LJ_RAISE = {
    "AA": 1, "KK": 1, "QQ": 1, "JJ": 1, "TT": 1, "99": 1, "88": 1, "77": 1,
    "66": 1, "55": 0.95, "44": 0.85, "33": 0.75, "22": 0.70,
    "AKs": 1, "AQs": 1, "AJs": 1, "ATs": 1, "A9s": 1, "A8s": 1, "A7s": 1,
    "A6s": 0.95, "A5s": 1, "A4s": 1, "A3s": 0.95, "A2s": 0.85,
    "AKo": 1, "AQo": 1, "AJo": 1, "ATo": 0.90, "A9o": 0.30, "A8o": 0.05,
    "KQs": 1, "KJs": 1, "KTs": 1, "K9s": 0.90, "K8s": 0.55, "K7s": 0.35,
    "K6s": 0.15, "K5s": 0.05,
    "KQo": 1, "KJo": 0.80, "KTo": 0.35,
    "QJs": 1, "QTs": 1, "Q9s": 0.80, "Q8s": 0.25, "Q7s": 0.05,
    "QJo": 0.65, "QTo": 0.15,
    "JTs": 1, "J9s": 0.85, "J8s": 0.25, "JTo": 0.10,
    "T9s": 1, "T8s": 0.65, "T7s": 0.05,
    "98s": 1, "87s": 0.90, "76s": 0.70, "65s": 0.55, "54s": 0.45,
    "64s": 0.15, "53s": 0.15, "43s": 0.05,
}

_HJ_RAISE = {
    "AA": 1, "KK": 1, "QQ": 1, "JJ": 1, "TT": 1, "99": 1, "88": 1, "77": 1,
    "66": 1, "55": 1, "44": 0.95, "33": 0.85, "22": 0.80,
    "AKs": 1, "AQs": 1, "AJs": 1, "ATs": 1, "A9s": 1, "A8s": 1, "A7s": 1,
    "A6s": 1, "A5s": 1, "A4s": 1, "A3s": 1, "A2s": 0.95,
    "AKo": 1, "AQo": 1, "AJo": 1, "ATo": 1, "A9o": 0.65, "A8o": 0.30,
    "A7o": 0.05, "A5o": 0.10,
    "KQs": 1, "KJs": 1, "KTs": 1, "K9s": 1, "K8s": 0.85, "K7s": 0.60,
    "K6s": 0.35, "K5s": 0.20, "K4s": 0.10,
    "KQo": 1, "KJo": 1, "KTo": 0.65, "K9o": 0.10,
    "QJs": 1, "QTs": 1, "Q9s": 1, "Q8s": 0.55, "Q7s": 0.20, "Q6s": 0.05,
    "QJo": 0.85, "QTo": 0.45, "Q9o": 0.05,
    "JTs": 1, "J9s": 1, "J8s": 0.65, "J7s": 0.15,
    "JTo": 0.55, "J9o": 0.05,
    "T9s": 1, "T8s": 0.90, "T7s": 0.30, "T9o": 0.05,
    "98s": 1, "97s": 0.35,
    "87s": 1, "86s": 0.20,
    "76s": 1, "75s": 0.20,
    "65s": 1, "64s": 0.35,
    "54s": 0.85, "53s": 0.30, "43s": 0.30, "42s": 0.05,
}

_CO_RAISE = {
    "AA": 1, "KK": 1, "QQ": 1, "JJ": 1, "TT": 1, "99": 1, "88": 1, "77": 1,
    "66": 1, "55": 1, "44": 1, "33": 1, "22": 1,
    "AKs": 1, "AQs": 1, "AJs": 1, "ATs": 1, "A9s": 1, "A8s": 1, "A7s": 1,
    "A6s": 1, "A5s": 1, "A4s": 1, "A3s": 1, "A2s": 1,
    "AKo": 1, "AQo": 1, "AJo": 1, "ATo": 1, "A9o": 1, "A8o": 0.85, "A7o": 0.65,
    "A6o": 0.40, "A5o": 0.60, "A4o": 0.45, "A3o": 0.30, "A2o": 0.20,
    "KQs": 1, "KJs": 1, "KTs": 1, "K9s": 1, "K8s": 1, "K7s": 0.95,
    "K6s": 0.80, "K5s": 0.65, "K4s": 0.45, "K3s": 0.35, "K2s": 0.25,
    "KQo": 1, "KJo": 1, "KTo": 0.95, "K9o": 0.55, "K8o": 0.10,
    "QJs": 1, "QTs": 1, "Q9s": 1, "Q8s": 0.95, "Q7s": 0.65, "Q6s": 0.45,
    "Q5s": 0.40, "Q4s": 0.25, "Q3s": 0.10, "Q2s": 0.05,
    "QJo": 1, "QTo": 0.85, "Q9o": 0.40, "Q8o": 0.05,
    "JTs": 1, "J9s": 1, "J8s": 0.95, "J7s": 0.55, "J6s": 0.10,
    "JTo": 0.85, "J9o": 0.35, "J8o": 0.05,
    "T9s": 1, "T8s": 1, "T7s": 0.85, "T6s": 0.20, "T9o": 0.55, "T8o": 0.05,
    "98s": 1, "97s": 0.85, "96s": 0.30, "98o": 0.05,
    "87s": 1, "86s": 0.75, "85s": 0.20,
    "76s": 1, "75s": 0.65, "74s": 0.05,
    "65s": 1, "64s": 0.65, "63s": 0.05,
    "54s": 1, "53s": 0.55, "52s": 0.05,
    "43s": 0.65, "42s": 0.05, "32s": 0.20,
}

_BTN_RAISE = {
    "AA": 1, "KK": 1, "QQ": 1, "JJ": 1, "TT": 1, "99": 1, "88": 1, "77": 1,
    "66": 1, "55": 1, "44": 1, "33": 1, "22": 1,
    "AKs": 1, "AQs": 1, "AJs": 1, "ATs": 1, "A9s": 1, "A8s": 1, "A7s": 1,
    "A6s": 1, "A5s": 1, "A4s": 1, "A3s": 1, "A2s": 1,
    "AKo": 1, "AQo": 1, "AJo": 1, "ATo": 1, "A9o": 1, "A8o": 0.90, "A7o": 0.65,
    "A6o": 0.30, "A5o": 0.55, "A4o": 0.35, "A3o": 0.20, "A2o": 0.10,
    "KQs": 1, "KJs": 1, "KTs": 1, "K9s": 1, "K8s": 1, "K7s": 1, "K6s": 1,
    "K5s": 0.90, "K4s": 0.65, "K3s": 0.45, "K2s": 0.35,
    "KQo": 1, "KJo": 1, "KTo": 1, "K9o": 0.55, "K8o": 0.10,
    "QJs": 1, "QTs": 1, "Q9s": 1, "Q8s": 1, "Q7s": 0.70, "Q6s": 0.55,
    "Q5s": 0.50, "Q4s": 0.30, "Q3s": 0.15, "Q2s": 0.05,
    "QJo": 1, "QTo": 0.90, "Q9o": 0.45, "Q8o": 0.05,
    "JTs": 1, "J9s": 1, "J8s": 1, "J7s": 0.70, "J6s": 0.15,
    "JTo": 0.95, "J9o": 0.55, "J8o": 0.05,
    "T9s": 1, "T8s": 1, "T7s": 0.90, "T6s": 0.25, "T9o": 0.65, "T8o": 0.10,
    "98s": 1, "97s": 0.95, "96s": 0.40, "98o": 0.15,
    "87s": 1, "86s": 0.85, "85s": 0.30,
    "76s": 1, "75s": 0.80, "74s": 0.10,
    "65s": 1, "64s": 0.75, "63s": 0.10,
    "54s": 1, "53s": 0.65, "52s": 0.10,
    "43s": 0.75, "42s": 0.10, "32s": 0.25,
}

# SB chart from the Wizard image: limp-heavy in 8-max because it assumes the
# 6 earlier seats already folded and only BB is left. For our heads-up override
# we DON'T use this chart at all (see _hu_btn_open below).
_SB_DIST = {
    # Top of range: 9% raise pure-strategy hands (premium)
    "AA": _rc(1, 0), "KK": _rc(1, 0), "QQ": _rc(1, 0), "JJ": _rc(1, 0),
    "AKs": _rc(1, 0), "AKo": _rc(1, 0),
    "AQs": _rc(0.85, 0.15), "AQo": _rc(0.60, 0.40),
    "AJs": _rc(0.60, 0.40),
    # Everything else mostly calls (the chart's 81.4% call band)
}


def _open_dist(pos, hand):
    """Return action distribution {'raise': f, 'call': f, 'fold': f}."""
    if pos == "SB":
        d = _SB_DIST.get(hand)
        if d:
            return d
        return {"raise": 0.0, "call": 0.81, "fold": 0.19}
    table = {"UTG": _LJ_RAISE, "UTG1": _LJ_RAISE,
             "LJ": _LJ_RAISE, "HJ": _HJ_RAISE,
             "CO": _CO_RAISE, "BTN": _BTN_RAISE}.get(pos)
    if table is None:
        return {"raise": 0.0, "call": 0.0, "fold": 1.0}
    rf = table.get(hand, 0.0)
    return {"raise": rf, "call": 0.0, "fold": 1.0 - rf}


# Hands NEVER opened HU from button — even GTO opens 75-85% wide here.
_HU_BTN_NEVER_OPEN = {
    "72o", "73o", "62o", "63o", "52o", "53o", "42o", "43o", "32o",
    "82o", "83o", "92o", "93o",
}


# ===========================================================================
# 3-bet defense ranges (vs an opener, before postflop)
# ===========================================================================

def _d(tb=0.0, c=0.0):
    """Defense distribution. Keys: '3bet', 'call', 'fold'."""
    return {"3bet": tb, "call": c, "fold": max(0.0, 1.0 - tb - c)}


# Opener tiers: how loose was their opening range?
_OPENER_TIER = {"UTG": "EARLY", "UTG1": "EARLY",
                "LJ": "MIDDLE", "HJ": "MIDDLE",
                "CO": "LATE", "BTN": "LATE", "SB": "LATE"}

# vs EARLY position open (tight opener -> we play tight back)
_VS_EARLY = {
    "AA": _d(1.0), "KK": _d(1.0),
    "QQ": _d(0.85, 0.15), "JJ": _d(0.30, 0.70),
    "TT": _d(0, 0.95), "99": _d(0, 0.85), "88": _d(0, 0.70),
    "77": _d(0, 0.55), "66": _d(0, 0.40), "55": _d(0, 0.30),
    "AKs": _d(0.80, 0.20), "AKo": _d(0.65, 0.35),
    "AQs": _d(0.35, 0.65), "AQo": _d(0, 0.65),
    "AJs": _d(0, 0.85), "ATs": _d(0, 0.50),
    "KQs": _d(0, 0.85), "KJs": _d(0, 0.50),
    "QJs": _d(0, 0.45), "JTs": _d(0, 0.45), "T9s": _d(0, 0.25),
    # Bluffs (suited aces with blockers)
    "A5s": _d(0.20), "A4s": _d(0.15),
}

# vs MIDDLE position open (LJ/HJ)
_VS_MIDDLE = {
    "AA": _d(1.0), "KK": _d(1.0),
    "QQ": _d(0.90, 0.10), "JJ": _d(0.50, 0.50),
    "TT": _d(0.20, 0.75), "99": _d(0, 0.95), "88": _d(0, 0.85),
    "77": _d(0, 0.70), "66": _d(0, 0.55), "55": _d(0, 0.40), "44": _d(0, 0.30),
    "AKs": _d(0.80, 0.20), "AKo": _d(0.70, 0.30),
    "AQs": _d(0.55, 0.40), "AQo": _d(0.20, 0.55),
    "AJs": _d(0.15, 0.75), "ATs": _d(0, 0.85),
    "AJo": _d(0, 0.55), "ATo": _d(0, 0.20),
    "KQs": _d(0.20, 0.70), "KJs": _d(0, 0.75),
    "KTs": _d(0, 0.45), "KQo": _d(0, 0.45),
    "QJs": _d(0, 0.60), "QTs": _d(0, 0.40),
    "JTs": _d(0, 0.55), "T9s": _d(0, 0.40),
    "98s": _d(0, 0.25), "87s": _d(0, 0.20),
    # Bluffs
    "A5s": _d(0.35), "A4s": _d(0.25), "A3s": _d(0.15),
    "K9s": _d(0.15), "Q9s": _d(0.10),
}

# vs LATE position open (CO/BTN/SB — wide range, we widen + add bluffs)
_VS_LATE = {
    "AA": _d(1.0), "KK": _d(1.0),
    "QQ": _d(0.95, 0.05), "JJ": _d(0.70, 0.30),
    "TT": _d(0.40, 0.55), "99": _d(0.20, 0.70), "88": _d(0.10, 0.75),
    "77": _d(0, 0.85), "66": _d(0, 0.75), "55": _d(0, 0.65), "44": _d(0, 0.55),
    "33": _d(0, 0.45), "22": _d(0, 0.40),
    "AKs": _d(0.80, 0.20), "AKo": _d(0.75, 0.25),
    "AQs": _d(0.65, 0.30), "AQo": _d(0.40, 0.45),
    "AJs": _d(0.30, 0.55), "AJo": _d(0.15, 0.55),
    "ATs": _d(0.10, 0.75), "ATo": _d(0, 0.55),
    "A9s": _d(0, 0.85), "A8s": _d(0, 0.65), "A7s": _d(0, 0.50),
    "KQs": _d(0.25, 0.60), "KQo": _d(0.15, 0.50),
    "KJs": _d(0.15, 0.60), "KJo": _d(0, 0.55),
    "KTs": _d(0, 0.70), "KTo": _d(0, 0.30),
    "K9s": _d(0, 0.50),
    "QJs": _d(0.10, 0.65), "QJo": _d(0, 0.40),
    "QTs": _d(0, 0.65), "Q9s": _d(0, 0.40),
    "JTs": _d(0.05, 0.65), "T9s": _d(0, 0.60),
    "98s": _d(0, 0.45), "87s": _d(0, 0.35),
    "76s": _d(0, 0.25), "65s": _d(0, 0.20),
    # Bluff 3-bets — suited aces & connectors
    "A5s": _d(0.65), "A4s": _d(0.50), "A3s": _d(0.40), "A2s": _d(0.25),
    "K9s": _d(0.30, 0.40),  # mixed bluff/call
    "Q9s": _d(0.25, 0.30),
}

# BB defense — closing the action, so call wider, 3-bet less for bluff
_BB_VS_EARLY = {
    "AA": _d(1.0), "KK": _d(1.0),
    "QQ": _d(0.80, 0.20), "JJ": _d(0.30, 0.70),
    "TT": _d(0, 1.0), "99": _d(0, 1.0), "88": _d(0, 1.0),
    "77": _d(0, 1.0), "66": _d(0, 1.0), "55": _d(0, 0.90),
    "44": _d(0, 0.70), "33": _d(0, 0.50), "22": _d(0, 0.40),
    "AKs": _d(0.70, 0.30), "AKo": _d(0.60, 0.40),
    "AQs": _d(0.30, 0.70), "AQo": _d(0, 0.95),
    "AJs": _d(0, 1.0), "AJo": _d(0, 0.85), "ATs": _d(0, 1.0),
    "A9s": _d(0, 0.95), "A8s": _d(0, 0.85), "A7s": _d(0, 0.70),
    "A5s": _d(0.20, 0.60), "A4s": _d(0, 0.55),
    "KQs": _d(0, 1.0), "KQo": _d(0, 0.85),
    "KJs": _d(0, 1.0), "KJo": _d(0, 0.70),
    "KTs": _d(0, 1.0), "K9s": _d(0, 0.85), "K8s": _d(0, 0.55),
    "QJs": _d(0, 1.0), "QTs": _d(0, 0.95), "Q9s": _d(0, 0.60),
    "JTs": _d(0, 1.0), "J9s": _d(0, 0.80), "T9s": _d(0, 0.95),
    "T8s": _d(0, 0.55), "98s": _d(0, 0.85), "87s": _d(0, 0.70),
    "76s": _d(0, 0.55), "65s": _d(0, 0.45), "54s": _d(0, 0.35),
}

_BB_VS_MIDDLE = {
    "AA": _d(1.0), "KK": _d(1.0),
    "QQ": _d(0.85, 0.15), "JJ": _d(0.40, 0.60),
    "TT": _d(0, 1.0), "99": _d(0, 1.0), "88": _d(0, 1.0),
    "77": _d(0, 1.0), "66": _d(0, 1.0), "55": _d(0, 1.0),
    "44": _d(0, 0.90), "33": _d(0, 0.75), "22": _d(0, 0.60),
    "AKs": _d(0.75, 0.25), "AKo": _d(0.65, 0.35),
    "AQs": _d(0.40, 0.60), "AQo": _d(0.10, 0.85),
    "AJs": _d(0, 1.0), "AJo": _d(0, 0.95), "ATs": _d(0, 1.0), "ATo": _d(0, 0.80),
    "A9s": _d(0, 1.0), "A8s": _d(0, 0.85), "A7s": _d(0, 0.75),
    "A6s": _d(0, 0.55), "A5s": _d(0.25, 0.65), "A4s": _d(0.15, 0.55),
    "KQs": _d(0, 1.0), "KQo": _d(0, 0.85),
    "KJs": _d(0, 1.0), "KJo": _d(0, 0.60),
    "KTs": _d(0, 1.0), "KTo": _d(0, 0.30),
    "K9s": _d(0, 0.85), "K8s": _d(0, 0.60), "K7s": _d(0, 0.40),
    "QJs": _d(0, 1.0), "QJo": _d(0, 0.50),
    "QTs": _d(0, 1.0), "Q9s": _d(0, 0.65), "Q8s": _d(0, 0.30),
    "JTs": _d(0, 1.0), "J9s": _d(0, 0.85), "J8s": _d(0, 0.40),
    "T9s": _d(0, 1.0), "T8s": _d(0, 0.70),
    "98s": _d(0, 1.0), "87s": _d(0, 0.85), "76s": _d(0, 0.70),
    "65s": _d(0, 0.55), "54s": _d(0, 0.40), "43s": _d(0, 0.20),
}

_BB_VS_LATE = {
    "AA": _d(1.0), "KK": _d(1.0),
    "QQ": _d(0.90, 0.10), "JJ": _d(0.55, 0.45),
    "TT": _d(0.25, 0.70),
    "99": _d(0, 1.0), "88": _d(0, 1.0), "77": _d(0, 1.0),
    "66": _d(0, 1.0), "55": _d(0, 1.0), "44": _d(0, 1.0),
    "33": _d(0, 0.95), "22": _d(0, 0.90),
    "AKs": _d(0.65, 0.35), "AKo": _d(0.55, 0.45),
    "AQs": _d(0.40, 0.60), "AQo": _d(0.25, 0.70),
    "AJs": _d(0.20, 0.80), "AJo": _d(0, 1.0), "ATs": _d(0, 1.0), "ATo": _d(0, 0.95),
    "A9s": _d(0, 1.0), "A9o": _d(0, 0.90),
    "A8s": _d(0, 1.0), "A8o": _d(0, 0.70),
    "A7s": _d(0, 1.0), "A7o": _d(0, 0.50),
    "A6s": _d(0, 1.0), "A5s": _d(0.40, 0.60),
    "A4s": _d(0.30, 0.70), "A3s": _d(0.20, 0.75), "A2s": _d(0, 0.85),
    "A6o": _d(0, 0.30), "A5o": _d(0, 0.50), "A4o": _d(0, 0.30),
    "KQs": _d(0.15, 0.85), "KQo": _d(0, 1.0),
    "KJs": _d(0, 1.0), "KJo": _d(0, 0.95),
    "KTs": _d(0, 1.0), "KTo": _d(0, 0.70),
    "K9s": _d(0.20, 0.75), "K8s": _d(0, 0.80), "K7s": _d(0, 0.60),
    "K9o": _d(0, 0.50),
    "QJs": _d(0, 1.0), "QJo": _d(0, 0.85),
    "QTs": _d(0, 1.0), "QTo": _d(0, 0.60),
    "Q9s": _d(0.15, 0.80), "Q8s": _d(0, 0.70),
    "JTs": _d(0, 1.0), "JTo": _d(0, 0.70),
    "J9s": _d(0.10, 0.85), "J8s": _d(0, 0.50),
    "T9s": _d(0, 1.0), "T8s": _d(0, 0.70),
    "98s": _d(0, 1.0), "87s": _d(0, 0.95),
    "76s": _d(0, 0.85), "65s": _d(0, 0.75),
    "54s": _d(0, 0.60), "43s": _d(0, 0.35),
    "97s": _d(0, 0.55), "86s": _d(0, 0.40),
}

_DEFENSE_TABLES = {
    ("EARLY", "BB"): _BB_VS_EARLY,
    ("MIDDLE", "BB"): _BB_VS_MIDDLE,
    ("LATE", "BB"): _BB_VS_LATE,
    ("EARLY", "IP"): _VS_EARLY,
    ("MIDDLE", "IP"): _VS_MIDDLE,
    ("LATE", "IP"): _VS_LATE,
    ("EARLY", "OOP"): _VS_EARLY,
    ("MIDDLE", "OOP"): _VS_MIDDLE,
    ("LATE", "OOP"): _VS_LATE,
}


def _defender_role(opener_pos, our_pos):
    if our_pos == "BB":
        return "BB"
    # Post-flop position. Order around the table from BTN backward:
    # BTN > CO > HJ > LJ > UTG; SB/BB are OOP postflop.
    order = ["UTG", "UTG1", "LJ", "HJ", "CO", "BTN"]
    if our_pos in ("SB", "BB"):
        return "BB" if our_pos == "BB" else "OOP"
    try:
        return "IP" if order.index(our_pos) > order.index(opener_pos) else "OOP"
    except ValueError:
        return "OOP"


def _defense_action(opener_pos, our_pos, hand):
    tier = _OPENER_TIER.get(opener_pos, "MIDDLE")
    role = _defender_role(opener_pos, our_pos)
    table = _DEFENSE_TABLES.get((tier, role), _VS_MIDDLE)
    return table.get(hand, _d(0, 0))


# 4-bet defense (we opened, they 3-bet)
_VS_3BET = {
    "AA": _d(1.0), "KK": _d(0.95, 0.05),
    "QQ": _d(0.50, 0.50), "JJ": _d(0, 0.85), "TT": _d(0, 0.65),
    "99": _d(0, 0.40), "88": _d(0, 0.25),
    "AKs": _d(0.85, 0.15), "AKo": _d(0.50, 0.50),
    "AQs": _d(0, 0.85), "AQo": _d(0, 0.30),
    "AJs": _d(0, 0.55), "ATs": _d(0, 0.30),
    "KQs": _d(0, 0.50), "KJs": _d(0, 0.20),
    "QJs": _d(0, 0.20), "JTs": _d(0, 0.20),
    # Small 4-bet bluffs with blockers
    "A5s": _d(0.15), "A4s": _d(0.10),
}


def _four_bet_defense(hand):
    return _VS_3BET.get(hand, _d(0, 0))


# ===========================================================================
# Short-stack push/fold tables
# ===========================================================================

# Hands to jam at each effective stack depth. JAM_X is a superset of JAM_(X+).
JAM_20BB = {
    "UTG": {"AA", "KK", "QQ", "JJ", "TT", "99", "88",
            "AKs", "AKo", "AQs", "AQo", "AJs"},
    "HJ":  {"AA", "KK", "QQ", "JJ", "TT", "99", "88", "77", "66",
            "AKs", "AKo", "AQs", "AQo", "AJs", "AJo", "ATs", "KQs"},
    "CO":  {"AA", "KK", "QQ", "JJ", "TT", "99", "88", "77", "66", "55",
            "AKs", "AKo", "AQs", "AQo", "AJs", "AJo", "ATs", "ATo",
            "A9s", "A8s", "A7s", "A5s", "KQs", "KQo", "KJs", "QJs"},
    "BTN": {"AA", "KK", "QQ", "JJ", "TT", "99", "88", "77", "66", "55", "44",
            "33", "22",
            "AKs", "AKo", "AQs", "AQo", "AJs", "AJo", "ATs", "ATo", "A9s",
            "A9o", "A8s", "A8o", "A7s", "A6s", "A5s", "A4s", "A3s", "A2s",
            "KQs", "KQo", "KJs", "KJo", "KTs", "KTo", "K9s", "K8s",
            "QJs", "QJo", "QTs", "Q9s", "JTs", "J9s", "T9s", "98s", "87s"},
    "SB":  {"AA", "KK", "QQ", "JJ", "TT", "99", "88", "77", "66", "55", "44",
            "33", "22",
            "AKs", "AKo", "AQs", "AQo", "AJs", "AJo", "ATs", "ATo", "A9s",
            "A9o", "A8s", "A8o", "A7s", "A7o", "A6s", "A5s", "A5o", "A4s",
            "A3s", "A2s",
            "KQs", "KQo", "KJs", "KJo", "KTs", "KTo", "K9s", "K9o", "K8s",
            "K7s",
            "QJs", "QJo", "QTs", "QTo", "Q9s", "Q8s",
            "JTs", "JTo", "J9s", "T9s", "T8s", "98s", "87s", "76s", "65s"},
}

JAM_12BB = {
    "UTG": JAM_20BB["UTG"] | {"77", "66", "AJo", "KQs"},
    "HJ":  JAM_20BB["HJ"]  | {"55", "44", "ATo", "KQo", "KJs", "QJs"},
    "CO":  JAM_20BB["CO"]  | {"44", "33", "22", "KJo", "KTs", "K9s", "QJo",
                              "QTs", "JTs", "T9s"},
    "BTN": JAM_20BB["BTN"] | {"KTo", "K9o", "K8s", "K7s", "Q8s", "Q7s",
                              "J8s", "T8s", "97s", "86s", "76s", "65s", "54s"},
    "SB":  JAM_20BB["SB"]  | {"K8o", "K7o", "Q8o", "Q7s", "J8s", "T7s", "97s",
                              "86s", "75s", "54s"},
}

JAM_8BB = {
    "UTG": JAM_12BB["UTG"] | {"55", "44", "ATo", "KJs", "QJs"},
    "HJ":  JAM_12BB["HJ"]  | {"33", "22", "A6o", "K9s", "JTs"},
    "CO":  JAM_12BB["CO"]  | {"K7s", "Q8s", "J8s", "T8s", "97s", "76s", "65s"},
    "BTN": JAM_12BB["BTN"] | {"K6s", "K5s", "Q6s", "J7s", "T7s", "96s", "85s",
                              "75s", "64s", "53s", "43s"},
    "SB":  JAM_12BB["SB"]  | {"K6s", "K5s", "K4s", "Q6s", "Q5s", "Q4s", "J7s",
                              "J6s", "T6s", "96s", "85s", "74s", "63s", "52s",
                              "32s", "43s"},
}


def _jam_hands(eff_bb, pos):
    p = pos if pos in JAM_20BB else _jam_pos_map(pos)
    if eff_bb <= 8:
        return JAM_8BB.get(p, set())
    if eff_bb <= 12:
        return JAM_12BB.get(p, set())
    if eff_bb <= 20:
        return JAM_20BB.get(p, set())
    return set()


def _jam_pos_map(pos):
    if pos in ("UTG", "UTG1"):
        return "UTG"
    if pos in ("LJ", "HJ", "MP"):
        return "HJ"
    return pos if pos in JAM_20BB else "BTN"


# Calling-an-all-in ranges by jammer position
_CALL_JAM = {
    "EARLY":  {"AA", "KK", "QQ", "JJ", "TT", "AKs", "AKo", "AQs"},
    "MIDDLE": {"AA", "KK", "QQ", "JJ", "TT", "99", "88",
               "AKs", "AKo", "AQs", "AQo", "AJs", "ATs", "KQs"},
    "LATE":   {"AA", "KK", "QQ", "JJ", "TT", "99", "88", "77",
               "AKs", "AKo", "AQs", "AQo", "AJs", "AJo", "ATs",
               "KQs", "KQo", "KJs", "QJs"},
    "BLIND":  {"AA", "KK", "QQ", "JJ", "TT", "99", "88", "77", "66", "55",
               "AKs", "AKo", "AQs", "AQo", "AJs", "AJo", "ATs", "ATo", "A9s",
               "KQs", "KQo", "KJs", "KJo", "KTs", "QJs", "QTs", "JTs"},
}


def _call_jam_range(jammer_pos):
    if jammer_pos in ("UTG", "UTG1"):
        return _CALL_JAM["EARLY"]
    if jammer_pos in ("LJ", "HJ"):
        return _CALL_JAM["MIDDLE"]
    if jammer_pos in ("CO", "BTN"):
        return _CALL_JAM["LATE"]
    if jammer_pos == "SB":
        return _CALL_JAM["BLIND"]
    return _CALL_JAM["MIDDLE"]


# ===========================================================================
# State inspection — engine schema aware
# ===========================================================================

def _detect_blinds(state):
    """Find SB and BB seats from action_log. Returns (sb_seat, bb_seat)."""
    sb = bb = None
    for a in state.get("action_log") or []:
        if not isinstance(a, dict):
            continue
        act = a.get("action")
        if act == "small_blind" and sb is None:
            sb = a.get("seat")
        elif act == "big_blind" and bb is None:
            bb = a.get("seat")
        if sb is not None and bb is not None:
            break
    return sb, bb


def _seat_positions(state):
    """Return {seat: position_name} for every seat. Position names use 6-max
    convention: UTG, HJ, CO, BTN, SB, BB. For larger tables we use UTG/UTG1/LJ
    early seats. Derived from blind positions in action_log (the engine does
    not broadcast the dealer)."""
    sb, bb = _detect_blinds(state)
    players = state.get("players") or []
    n = len(players)
    if n < 2 or sb is None or bb is None:
        return {}

    # Heads-up: dealer = SB
    if n == 2:
        return {sb: "SB", bb: "BB"}

    # Multi-way: positions counted backward from BB
    # 3 -> [BTN, SB, BB]
    # 4 -> [CO, BTN, SB, BB]
    # 5 -> [HJ, CO, BTN, SB, BB]
    # 6 -> [UTG, HJ, CO, BTN, SB, BB]
    # 7 -> [UTG, LJ, HJ, CO, BTN, SB, BB]    (LJ inserted)
    # 8 -> [UTG, UTG1, LJ, HJ, CO, BTN, SB, BB]
    # 9 -> [UTG, UTG1, LJ, HJ, CO, BTN, SB, BB] + extra UTG2 (not modelled)
    by_size = {
        2: ["SB", "BB"],
        3: ["BTN", "SB", "BB"],
        4: ["CO", "BTN", "SB", "BB"],
        5: ["HJ", "CO", "BTN", "SB", "BB"],
        6: ["UTG", "HJ", "CO", "BTN", "SB", "BB"],
        7: ["UTG", "LJ", "HJ", "CO", "BTN", "SB", "BB"],
        8: ["UTG", "UTG1", "LJ", "HJ", "CO", "BTN", "SB", "BB"],
        9: ["UTG", "UTG1", "LJ", "HJ", "CO", "BTN", "SB", "BB", "BTN"],
    }
    names = by_size.get(n, by_size[6])

    # Walk seats starting from sb backward. names is ordered "first to act
    # preflop -> ... -> BB". The seat that's BB is the *last* in `names`.
    # We need a mapping seat -> position. Start from bb and walk backward.
    out = {}
    seat = bb
    for name in reversed(names):
        out[seat] = name
        seat = (seat - 1) % n
    return out


def _hero_position(state):
    me = state.get("seat_to_act")
    return _seat_positions(state).get(me)


def _seats_in_hand(state):
    """Set of seats that haven't folded this hand."""
    out = set()
    for p in state.get("players") or []:
        if not isinstance(p, dict):
            continue
        if p.get("is_folded"):
            continue
        if p.get("state") in ("folded", "busted"):
            continue
        out.add(p.get("seat"))
    return out


def _n_live_opponents(state):
    me = state.get("seat_to_act")
    n = 0
    for p in state.get("players") or []:
        if not isinstance(p, dict):
            continue
        if p.get("seat") == me:
            continue
        if p.get("is_folded") or p.get("state") in ("folded", "busted"):
            continue
        n += 1
    return max(1, n)


def _effective_stack_bb(state):
    me = state.get("seat_to_act")
    my_stack = state.get("your_stack", 0) + state.get("your_bet_this_street", 0)
    opp_stacks = []
    for p in state.get("players") or []:
        if not isinstance(p, dict):
            continue
        if p.get("seat") == me:
            continue
        if p.get("is_folded") or p.get("state") in ("folded", "busted"):
            continue
        s = p.get("stack", 0) + p.get("bet_this_street", 0)
        if s > 0:
            opp_stacks.append(s)
    eff = min(my_stack, max(opp_stacks)) if opp_stacks else my_stack
    return eff / BIG_BLIND


def _reconstruct_streets(action_log):
    """Replay action_log and annotate each entry with the street index it
    happened on (0=preflop, 1=flop, 2=turn, 3=river).

    Mirrors v4.1's _reconstruct_hand logic. Returns a list of dicts with the
    original keys plus 'street_idx', 'pot_before', 'owed'."""
    committed = {}
    total = {}
    folded = set()
    allin = set()
    acted = set()
    current_bet = 0
    street_idx = 0
    out = []

    for entry in action_log or []:
        if not isinstance(entry, dict):
            continue
        seat = entry.get("seat")
        act = entry.get("action")
        amt = entry.get("amount") or 0
        if seat is None or act is None:
            continue

        committed.setdefault(seat, 0)
        total.setdefault(seat, 0)

        if act in ("small_blind", "big_blind"):
            committed[seat] += amt
            total[seat] += amt
            current_bet = max(current_bet, committed[seat])
            continue

        owed = max(0, current_bet - committed[seat])
        pot_before = sum(total.values())

        out.append({
            "seat": seat, "action": act, "amount": amt,
            "street_idx": street_idx, "owed": owed, "pot_before": pot_before,
        })

        aggressive = False
        if act == "fold":
            folded.add(seat)
        elif act == "check":
            acted.add(seat)
        elif act == "call":
            pay = max(0, current_bet - committed[seat])
            committed[seat] += pay
            total[seat] += pay
            acted.add(seat)
        elif act in ("raise", "all_in"):
            new_level = max(amt, committed[seat])
            delta = new_level - committed[seat]
            committed[seat] = new_level
            total[seat] += delta
            if new_level > current_bet:
                current_bet = new_level
                aggressive = True
            if act == "all_in":
                allin.add(seat)
            acted.add(seat)
            if aggressive:
                acted = {seat}

        active = [s for s in set(list(committed) + list(total))
                  if s not in folded and s not in allin]
        if active and all(committed.get(s, 0) == current_bet and s in acted
                          for s in active):
            street_idx = min(street_idx + 1, 3)
            committed = {s: 0 for s in committed}
            current_bet = 0
            acted = set()

    return out


def _preflop_history(state):
    """Inspect preflop actions in action_log.

    Returns:
        raises:               count of preflop raises
        first_raiser_seat:    seat of the first raiser
        last_raiser_seat:     seat of the most recent raiser
        last_raiser_was_us:   bool
        first_raiser_pos:     position name of first raiser
        last_raiser_pos:      position name of last raiser
    """
    info = {
        "raises": 0,
        "first_raiser_seat": None,
        "last_raiser_seat": None,
        "last_raiser_was_us": False,
        "first_raiser_pos": None,
        "last_raiser_pos": None,
    }
    me = state.get("seat_to_act")
    recon = _reconstruct_streets(state.get("action_log") or [])
    positions = _seat_positions(state)

    for rec in recon:
        if rec["street_idx"] != 0:
            continue
        if rec["action"] in ("raise", "all_in"):
            info["raises"] += 1
            if info["first_raiser_seat"] is None:
                info["first_raiser_seat"] = rec["seat"]
                info["first_raiser_pos"] = positions.get(rec["seat"])
            info["last_raiser_seat"] = rec["seat"]
            info["last_raiser_pos"] = positions.get(rec["seat"])
            info["last_raiser_was_us"] = (rec["seat"] == me)
    return info


def _we_are_pfr(state):
    """True if the most recent preflop raise was made by us."""
    return _preflop_history(state)["last_raiser_was_us"]


def _equity_shade(state):
    """Down-shade equity-vs-random based on how much preflop action there was."""
    raises = _preflop_history(state)["raises"]
    if raises >= 2:
        return 0.85   # 3-bet pot
    if raises >= 1:
        return 0.92   # single raised pot
    return 1.0        # limped / unopened


def _is_facing_jam(state):
    """True if the bet we're facing is an effective all-in for us."""
    owed = state.get("amount_owed", 0)
    stack = state.get("your_stack", 0)
    if stack > 0 and owed >= stack:
        return True
    for a in (state.get("action_log") or [])[-8:]:
        if isinstance(a, dict) and a.get("action") == "all_in":
            return True
    return False


def _is_facing_bet(state):
    return state.get("amount_owed", 0) > 0


# ===========================================================================
# Action helpers
# ===========================================================================

def _required_equity(state):
    owed = state.get("amount_owed", 0)
    pot = state.get("pot", 0)
    if owed <= 0:
        return 0.0
    return owed / (pot + owed)


def _sample_dist(dist, rng):
    """Sample from a distribution dict (already-summed-to-1)."""
    r = rng.random()
    cum = 0.0
    for k, v in dist.items():
        cum += v
        if r < cum:
            return k
    return max(dist.items(), key=lambda kv: kv[1])[0]


def _raise_to(state, pot_fraction):
    """Compute a legal raise total = current_bet + (pot * fraction)."""
    pot = state.get("pot", 0)
    min_to = state.get("min_raise_to", 0)
    my_bet = state.get("your_bet_this_street", 0)
    stack = state.get("your_stack", 0)
    cap = stack + my_bet
    target = state.get("current_bet", 0) + int(pot * pot_fraction)
    return min(max(target, min_to), cap)


def _bb_to_chips(state, size_bb):
    """Convert a BB size into a raise total. Uses the engine's BIG_BLIND."""
    return int(size_bb * BIG_BLIND)


def _legal_raise(state, target):
    """Snap target to a legal action. Returns all_in if we'd commit."""
    target = max(int(target), state.get("min_raise_to", target))
    stack = state.get("your_stack", 0)
    my_bet = state.get("your_bet_this_street", 0)
    cap = stack + my_bet
    if target >= cap:
        return {"action": "all_in"}
    return {"action": "raise", "amount": target}


def _check_or_fold(state):
    return {"action": "check"} if state.get("can_check") else {"action": "fold"}


# ===========================================================================
# Postflop decision logic
# ===========================================================================

# Tunables — twist these after sparring. All controlled here so you don't have
# to hunt through code to find the levers.
CBET_FREQ_HU_DRY = 0.85
CBET_FREQ_HU_WET = 0.55
CBET_FREQ_MULTIWAY = 0.30
VALUE_BET_EQ = 0.60
RAISE_FOR_VALUE_EQ = 0.72
MIN_BLUFF_EQ = 0.18
DRAW_CALL_MAX_POT_FRAC = 0.45


def _decide_postflop(state, rng):
    """Equity-driven postflop decision."""
    hole = state.get("your_cards") or []
    board = state.get("community_cards") or []
    pot = max(1, state.get("pot", 1))
    owed = state.get("amount_owed", 0)
    can_check = state.get("can_check", owed == 0)

    n_opp = _n_live_opponents(state)
    if n_opp <= 0:
        return _check_or_fold(state)

    eq = equity_vs_random(hole, board, n_opp, rng)
    if eq is None:
        # Equity unavailable -> conservative fallback
        if can_check:
            return {"action": "check"}
        return {"action": "call"} if _required_equity(state) <= 0.20 else {"action": "fold"}

    eq *= _equity_shade(state)
    cls = _strength_class(eq)
    texture = _texture(board)
    is_pfr = _we_are_pfr(state)
    facing_bet = owed > 0
    pot_odds = (owed / (pot + owed)) if facing_bet else 0.0

    if facing_bet:
        return _facing_bet_decision(state, eq, cls, pot_odds, texture, n_opp, rng)
    return _no_bet_decision(state, eq, cls, texture, n_opp, is_pfr, rng)


def _facing_bet_decision(state, eq, cls, pot_odds, texture, n_opp, rng):
    pot = max(1, state.get("pot", 1))
    owed = state.get("amount_owed", 0)
    facing_size_pct = owed / pot if pot > 0 else 1.0

    # Raise for value with monsters and very strong hands
    if eq >= RAISE_FOR_VALUE_EQ and n_opp == 1:
        return _legal_raise(state, _raise_to(state, 1.0))

    # Strong hand: call, occasionally raise on dry boards
    if cls == "strong":
        if texture["dry"] and n_opp == 1 and rng.random() < 0.35:
            return _legal_raise(state, _raise_to(state, 0.75))
        return {"action": "call"}

    # Medium / weak-made: pot-odds call/fold
    if cls in ("medium", "weak_made"):
        if eq > pot_odds + 0.05:
            return {"action": "call"}
        return {"action": "fold"}

    # Draw: pot-odds + occasional semi-bluff raise
    if cls == "draw":
        if facing_size_pct < DRAW_CALL_MAX_POT_FRAC and eq > pot_odds:
            return {"action": "call"}
        if texture["wetness"] > 0.5 and n_opp == 1 and rng.random() < 0.15:
            return _legal_raise(state, _raise_to(state, 1.0))
        return {"action": "fold"}

    # Air: fold, occasional bluff-raise (rare)
    if cls == "air":
        if facing_size_pct < 0.5 and n_opp == 1 and rng.random() < 0.04:
            return _legal_raise(state, _raise_to(state, 1.0))
        return {"action": "fold"}

    return {"action": "fold"}


def _no_bet_decision(state, eq, cls, texture, n_opp, is_pfr, rng):
    pot = max(1, state.get("pot", 1))
    street = state.get("street", "preflop")

    if cls == "monster":
        # Slow-play sometimes on dry boards
        if texture["dry"] and n_opp == 1 and rng.random() < 0.20:
            return {"action": "check"}
        size_frac = 0.85 if street == "river" else (0.75 if texture["wetness"] > 0.5 else 0.65)
        return _legal_raise(state, _raise_to(state, size_frac))

    if cls == "strong":
        size_frac = 0.65 if street == "river" else (0.65 if texture["wetness"] > 0.4 else 0.50)
        return _legal_raise(state, _raise_to(state, size_frac))

    if cls == "medium":
        if n_opp == 1 and eq > VALUE_BET_EQ:
            return _legal_raise(state, _raise_to(state, 0.45))
        return {"action": "check"}

    if cls == "weak_made":
        return {"action": "check"}

    if cls == "draw":
        if n_opp == 1:
            freq = 0.55 if texture["wetness"] > 0.4 else 0.35
            if rng.random() < freq:
                return _legal_raise(state, _raise_to(state, 0.55))
        return {"action": "check"}

    # air
    if is_pfr:
        cbet = (CBET_FREQ_MULTIWAY if n_opp >= 2 else
                (CBET_FREQ_HU_WET if texture["wetness"] > 0.4 else CBET_FREQ_HU_DRY))
        if eq >= MIN_BLUFF_EQ and rng.random() < cbet:
            frac = 0.33 if texture["dry"] else (0.5 if texture["wetness"] < 0.5 else 0.66)
            return _legal_raise(state, _raise_to(state, frac))
    return {"action": "check"}


# ===========================================================================
# Preflop decision logic
# ===========================================================================

SHORT_STACK_BB = 20.0


def _decide_preflop(state, rng):
    hole = state.get("your_cards") or []
    hand = _canonical(hole)
    pos = _hero_position(state)
    eff_bb = _effective_stack_bb(state)
    history = _preflop_history(state)
    facing_jam = _is_facing_jam(state)
    seats_in_hand = _seats_in_hand(state)

    if hand is None or pos is None:
        return _check_or_fold(state)

    # Short-stack mode: jam or fold
    if eff_bb <= SHORT_STACK_BB:
        return _short_stack(state, hand, pos, history, facing_jam, eff_bb, rng)

    # 100bb chart mode
    if facing_jam:
        jammer = history["last_raiser_pos"] or history["first_raiser_pos"] or "BTN"
        return {"action": "call"} if hand in _call_jam_range(jammer) else {"action": "fold"}

    if history["raises"] == 0:
        return _open_decision(state, hand, pos, seats_in_hand, rng)

    if history["raises"] == 1 and history["last_raiser_was_us"]:
        # Edge case — only happens if we opened and now face limp behind us?
        # (rare in 6max) fall through to defense
        return _check_or_fold(state)

    if history["raises"] >= 1 and history["last_raiser_was_us"]:
        return _vs_3bet(state, hand, rng)

    if history["raises"] >= 1:
        return _vs_open(state, hand, pos, history, rng)

    return _check_or_fold(state)


def _open_decision(state, hand, pos, seats_in_hand, rng):
    """First in. Use the RFI chart, with HU override for SB."""
    # Heads-up override: 8-max SB chart limps 81% which is wrong HU
    if len(seats_in_hand) == 2 and pos == "SB":
        return _hu_btn_open(state, hand, rng)

    dist = _open_dist(pos, hand)
    choice = _sample_dist(dist, rng)
    if choice == "raise":
        size_bb = OPEN_SIZE_BB.get(pos, 2.5)
        return _legal_raise(state, _bb_to_chips(state, size_bb))
    if choice == "call":
        # SB limping; size already paid (the SB post) -> just call BB
        return {"action": "call"}
    return _check_or_fold(state)


def _hu_btn_open(state, hand, rng):
    """HU button (=SB) open. Open ~85% — only the trashiest hands fold."""
    if hand in _HU_BTN_NEVER_OPEN:
        # Even these open occasionally in real solver outputs
        if rng.random() < 0.10:
            return _legal_raise(state, _bb_to_chips(state, 2.5))
        return _check_or_fold(state)
    return _legal_raise(state, _bb_to_chips(state, 2.5))


def _vs_open(state, hand, our_pos, history, rng):
    opener_pos = history["first_raiser_pos"] or "HJ"
    dist = _defense_action(opener_pos, our_pos, hand)
    choice = _sample_dist(dist, rng)

    if choice == "3bet":
        opener_size = state.get("current_bet", 0)
        # 3-bet sizing: IP ~3x, OOP ~4x the opener's raise
        in_pos = _defender_role(opener_pos, our_pos) == "IP"
        multiplier = 3.0 if in_pos else 4.0
        return _legal_raise(state, int(opener_size * multiplier))
    if choice == "call":
        return {"action": "call"}
    return _check_or_fold(state)


def _vs_3bet(state, hand, rng):
    dist = _four_bet_defense(hand)
    choice = _sample_dist(dist, rng)
    if choice == "3bet":
        three_bet_to = state.get("current_bet", 0)
        return _legal_raise(state, int(three_bet_to * 2.25))
    if choice == "call":
        return {"action": "call"}
    return {"action": "fold"}


def _short_stack(state, hand, pos, history, facing_jam, eff_bb, rng):
    if facing_jam:
        jammer = history["last_raiser_pos"] or history["first_raiser_pos"] or "BTN"
        return {"action": "call"} if hand in _call_jam_range(jammer) else {"action": "fold"}

    if history["raises"] == 0:
        # First in: open-jam if hand in range, otherwise fold (below 12bb)
        # or min-raise (12-20bb with marginal premiums)
        jam_set = _jam_hands(eff_bb, pos or "BTN")
        if hand in jam_set:
            return {"action": "all_in"}
        if eff_bb <= 12:
            return _check_or_fold(state)
        # 12-20bb: with strong hands not in jam set, just min-raise
        dist = _open_dist(pos, hand)
        if dist.get("raise", 0) > 0.5:
            return _legal_raise(state, _bb_to_chips(state, 2.2))
        return _check_or_fold(state)

    # Facing a raise short-stacked: tighter version of standard defense
    return _vs_open(state, hand, pos, history, rng)


# ===========================================================================
# Top-level decide
# ===========================================================================

def _sanitize(action, state):
    """Make sure the returned action is legal regardless of internal bugs."""
    if not isinstance(action, dict):
        action = {"action": "fold"}
    a = str(action.get("action", "fold")).lower().strip()
    can_check = state.get("can_check", False)
    if a == "check":
        return {"action": "check"} if can_check else {"action": "call"}
    if a == "call":
        return {"action": "check"} if can_check else {"action": "call"}
    if a == "all_in":
        return {"action": "all_in"}
    if a == "raise":
        try:
            amt = int(action.get("amount"))
        except (TypeError, ValueError):
            amt = state.get("min_raise_to", 0)
        min_to = state.get("min_raise_to", 0)
        cap = state.get("your_stack", 0) + state.get("your_bet_this_street", 0)
        amt = max(amt, min_to)
        return {"action": "all_in"} if amt >= cap else {"action": "raise", "amount": amt}
    return {"action": "fold"}


def _emergency(state):
    """Last-resort safe action if everything else fails."""
    try:
        if state.get("can_check"):
            return {"action": "check"}
        owed = state.get("amount_owed", 0)
        pot = state.get("pot", 1)
        if 0 < owed < pot * 0.15:
            return {"action": "call"}
    except Exception:
        pass
    return {"action": "fold"}


def decide(game_state: dict) -> dict:
    """Engine entry point."""
    # Warmup hook: prime eval7 so the first real call isn't paying import cost.
    if isinstance(game_state, dict) and game_state.get("type") == "warmup":
        if _HAVE_EVAL7 and _FULL_DECK:
            try:
                rng = random.Random(0)
                equity_vs_random(["As", "Kh"], ["2c", "7d", "9s"], 1, rng,
                                 time_budget=0.05, max_iters=80)
            except Exception:
                pass
        return {"action": "fold"}

    try:
        rng = _spot_rng(game_state)
        street = game_state.get("street", "preflop")
        if street == "preflop":
            action = _decide_preflop(game_state, rng)
        else:
            action = _decide_postflop(game_state, rng)
        return _sanitize(action, game_state)
    except Exception:
        return _emergency(game_state)
