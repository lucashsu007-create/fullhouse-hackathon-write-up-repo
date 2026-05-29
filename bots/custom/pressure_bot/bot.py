"""
PRESSURE — a strong, adaptive, polarized bluffer.
=================================================
Purpose: a sparring opponent that *exploits* a value-only / face-up bot like
v7_m2, rather than ignoring it. It is NOT a maniac (the `aggressor` ref bot
spews and loses). The design is the precise nemesis of an unbalanced
value-bettor:

  * Tight-aggressive preflop  -> its bets carry credibility (real value range).
  * Polarized postflop        -> bets big with value AND air, checks the middle.
  * Attacks weakness          -> when the opponent CHECKS (caps their range),
                                 it barrels to fold out medium hands.
  * Disciplined vs strength    -> FOLDS marginal hands to bets; it will not pay
                                 off a value-only bettor. This is what makes it
                                 "strong" rather than just loose.
  * Lightly adaptive          -> tracks how often opponents fold to its
                                 aggression and ramps bluffing against
                                 over-folders; respects bets from passive
                                 players (treats a sudden bet from a quiet
                                 opponent as value).

Same interface as the reference bots: decide(game_state) -> {"action": ...}.
Sizing: `amount` is the TOTAL bet, per the engine. Everything is wrapped so a
malformed state or a missing eval7 degrades to a safe check/fold, never a crash.

Run it:  python3 sandbox/match.py bots/v7_m2/bot.py bots/pressure/bot.py --hands 400
or drop it into a 6-bot table alongside v7_m2 to see if the imbalance leaks.
"""

import os, random

BOT_NAME = "pressure"

try:
    import eval7
    _HAVE_EVAL7 = True
    _DECK = [eval7.Card(r + s) for r in "23456789TJQKA" for s in "shdc"]
except Exception:                                   # pragma: no cover
    eval7 = None
    _HAVE_EVAL7 = False
    _DECK = []

_RANKS = "23456789TJQKA"
_RVAL = {r: i for i, r in enumerate(_RANKS, start=2)}

# per-opponent adaptation state (persists across hands within the process)
#   id -> {"agg_acts","tot_acts","faced_our_aggr","folded_our_aggr"}
_OPP = {}


# ---------------------------------------------------------------------------
# small safe helpers
# ---------------------------------------------------------------------------
def _num(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return float(d)


def _cards(raw):
    out = []
    for c in (raw or []):
        try:
            if isinstance(c, str) and len(c) >= 2 and c[0].upper() in _RVAL:
                out.append(c[0].upper() + c[1].lower())
        except Exception:
            pass
    return out


def _spot_rng(state):
    """Deterministic per-spot RNG so mixed frequencies are reproducible."""
    key = (tuple(_cards(state.get("your_cards"))),
           tuple(_cards(state.get("community_cards"))),
           int(_num(state.get("pot"))), str(state.get("street")))
    return random.Random(hash(key) & 0xFFFFFFFF)


# ---------------------------------------------------------------------------
# hand strength + draws
# ---------------------------------------------------------------------------
def _equity(hole, board, n_opp, rng, iters=400):
    """Monte-Carlo equity vs n random opponents. eval7 when available; a crude
    high-card heuristic as fallback so the bot still functions (and never
    crashes) without eval7."""
    if _HAVE_EVAL7 and len(hole) == 2:
        try:
            hcards = [eval7.Card(c) for c in hole]
            bcards = [eval7.Card(c) for c in board]
            dead = set(str(c) for c in hcards + bcards)
            live = [c for c in _DECK if str(c) not in dead]
            need = 5 - len(bcards)
            wins = ties = 0
            n_opp = max(1, int(n_opp))
            for _ in range(iters):
                rng.shuffle(live)
                idx = 0
                opp_holes = []
                for _o in range(n_opp):
                    opp_holes.append(live[idx:idx + 2]); idx += 2
                fill = live[idx:idx + need]
                full = bcards + fill
                my = eval7.evaluate(hcards + full)
                best_opp = max(eval7.evaluate(oh + full) for oh in opp_holes)
                if my > best_opp:
                    wins += 1
                elif my == best_opp:
                    ties += 1
            return (wins + ties / 2.0) / iters
        except Exception:
            pass
    # ---- fallback heuristic (no eval7): rank-based, very rough ----
    if len(hole) != 2:
        return 0.4
    hi = max(_RVAL.get(hole[0][0], 7), _RVAL.get(hole[1][0], 7))
    lo = min(_RVAL.get(hole[0][0], 7), _RVAL.get(hole[1][0], 7))
    pair = hole[0][0] == hole[1][0]
    suited = hole[0][1] == hole[1][1]
    base = 0.35 + 0.02 * (hi - 7) + 0.01 * (lo - 7)
    if pair:
        base = 0.50 + 0.035 * (hi - 7)
    if suited:
        base += 0.03
    return max(0.05, min(0.95, base))


def _draws(hole, board):
    """Return (flush_draw, straight_draw) booleans for semi-bluff detection."""
    cards = hole + board
    if len(cards) < 4:
        return False, False
    suits = {}
    for c in cards:
        suits[c[1]] = suits.get(c[1], 0) + 1
    flush_draw = any(v == 4 for v in suits.values())
    vals = sorted(set(_RVAL.get(c[0], 0) for c in cards))
    if 14 in vals:
        vals = sorted(set(vals + [1]))            # wheel
    straight_draw = False
    for i in range(len(vals)):
        window = [v for v in vals if vals[i] <= v < vals[i] + 5]
        if len(window) >= 4:
            straight_draw = True
            break
    return flush_draw, straight_draw


# ---------------------------------------------------------------------------
# table / opponent context
# ---------------------------------------------------------------------------
def _opp_ids(state):
    me = state.get("seat_to_act")
    out = []
    for p in (state.get("players") or []):
        if not isinstance(p, dict):
            continue
        if p.get("in_hand", True) and p.get("seat") != me:
            out.append(str(p.get("bot_id", p.get("seat"))))
    return out


def _n_in_pot(state):
    n = 0
    for p in (state.get("players") or []):
        if isinstance(p, dict) and p.get("in_hand", True):
            n += 1
    return max(2, n)


def _overfold_mult(state):
    """Adaptation: if live opponents fold a lot to our aggression, bluff more."""
    ids = _opp_ids(state)
    faced = folded = 0
    for i in ids:
        o = _OPP.get(i)
        if o:
            faced += o["faced_our_aggr"]; folded += o["folded_our_aggr"]
    if faced < 8:
        return 1.0
    rate = folded / faced
    # 0.30 fold -> 0.85x ; 0.50 -> 1.0x ; 0.75 -> 1.35x
    return max(0.7, min(1.5, 0.55 + 0.9 * rate))


def _villain_passive_and_betting(state):
    """If a quiet (low-aggression) opponent is the one betting into us, treat
    their bet as value -> we fold marginal hands. Mirrors v7_m2's rock logic,
    used here on defense."""
    if _num(state.get("amount_owed")) <= 0:
        return False
    ids = _opp_ids(state)
    for i in ids:
        o = _OPP.get(i)
        if o and o["tot_acts"] >= 15 and (o["agg_acts"] / o["tot_acts"]) < 0.12:
            return True
    return False


def _update_opp(state):
    """Light tracking from the action log: per-opponent aggression frequency and
    fold-to-our-aggression. Wrapped so any schema oddity just disables it."""
    try:
        log = state.get("action_log") or []
        me = state.get("seat_to_act")
        my_id = None
        for p in (state.get("players") or []):
            if isinstance(p, dict) and p.get("seat") == me:
                my_id = str(p.get("bot_id", p.get("seat")))
        prev_aggr_by = None
        for a in log:
            if not isinstance(a, dict):
                continue
            seat = a.get("seat", a.get("player"))
            sid = str(seat)
            act = str(a.get("action", "")).lower()
            if sid != str(my_id):
                o = _OPP.setdefault(sid, {"agg_acts": 0, "tot_acts": 0,
                                          "faced_our_aggr": 0, "folded_our_aggr": 0})
                o["tot_acts"] += 1
                if act in ("raise", "bet", "all_in", "allin"):
                    o["agg_acts"] += 1
                # did this opponent face OUR aggression and fold?
                if prev_aggr_by == str(my_id):
                    o["faced_our_aggr"] += 1
                    if act in ("fold", "folds"):
                        o["folded_our_aggr"] += 1
            if act in ("raise", "bet", "all_in", "allin"):
                prev_aggr_by = sid
            elif act in ("fold", "folds", "call", "calls", "check", "checks"):
                prev_aggr_by = None
    except Exception:
        pass


# ---------------------------------------------------------------------------
# sizing
# ---------------------------------------------------------------------------
def _raise_to(state, frac):
    """Total-bet target for a bet/raise of `frac` * pot (after calling).
    Clamps to [min_raise_to, all-in]; returns ('all_in', None) if it commits."""
    pot = max(1.0, _num(state.get("pot"), 1))
    owed = max(0.0, _num(state.get("amount_owed"), 0))
    cur = _num(state.get("current_bet"), 0)
    stack = max(0.0, _num(state.get("your_stack"), 0))
    minr = _num(state.get("min_raise_to"), 0)
    if owed <= 0:
        target = frac * pot
    else:
        target = cur + frac * (pot + owed)
    my_in = max(0.0, cur - owed)
    target = max(target, minr, cur + 1)
    additional = target - my_in
    if additional >= stack:
        return ("all_in", None)
    return ("raise", int(round(target)))


def _aggr_action(state, frac):
    act, amt = _raise_to(state, frac)
    if act == "all_in":
        return {"action": "all_in"}
    return {"action": "raise", "amount": amt}


# ---------------------------------------------------------------------------
# preflop
# ---------------------------------------------------------------------------
def _canon(hole):
    if len(hole) != 2:
        return None
    r1, s1 = hole[0][0], hole[0][1]
    r2, s2 = hole[1][0], hole[1][1]
    if _RVAL[r1] < _RVAL[r2]:
        r1, r2, s1, s2 = r2, r1, s2, s1
    if r1 == r2:
        return r1 + r2
    return r1 + r2 + ("s" if s1 == s2 else "o")


def _preflop(state, hole, rng):
    owed = _num(state.get("amount_owed"))
    cls = _canon(hole)
    if cls is None:
        return {"action": "check"} if state.get("can_check") else {"action": "fold"}
    hi = _RVAL[cls[0]]; lo = _RVAL[cls[1]] if len(cls) >= 2 else hi
    pair = len(cls) == 2
    suited = cls.endswith("s")
    # strength score for an aggressive-but-credible opener
    score = (hi + lo) / 28.0
    if pair:
        score = 0.55 + 0.04 * (hi - 7)
    if suited:
        score += 0.06
    if hi == 14:                                    # ace-x gets a bump (3bet bluffs)
        score += 0.05
    n = _n_in_pot(state)

    if owed <= 0:                                   # first in / limped to us -> raise wide
        if score > 0.42 or pair:
            return _aggr_action(state, 1.1)         # ~3x open
        if state.get("can_check"):
            return {"action": "check"}
        return {"action": "fold"}
    # facing a raise: polarized 3-bet or fold (aggressive), occasional call
    if score > 0.72 or (pair and hi >= 11):
        return _aggr_action(state, 1.0)             # value 3bet
    if (suited and hi == 14) and rng.random() < 0.5:
        return _aggr_action(state, 1.0)             # bluff 3bet (Axs)
    pot = max(1.0, _num(state.get("pot")))
    if score > 0.5 and owed < 0.35 * pot and n <= 3:
        return {"action": "call"}
    return {"action": "fold"}


# ---------------------------------------------------------------------------
# postflop
# ---------------------------------------------------------------------------
def _postflop(state, hole, board, rng):
    owed = _num(state.get("amount_owed"))
    can_check = bool(state.get("can_check"))
    n_opp = _n_in_pot(state) - 1
    eq = _equity(hole, board, n_opp, rng)
    fd, sd = _draws(hole, board)
    has_draw = fd or sd
    hu = n_opp <= 1
    of = _overfold_mult(state)
    pot = max(1.0, _num(state.get("pot")))

    # ---------------- we have the betting lead / it's checked to us ----------
    if owed <= 0:
        if eq >= 0.80:
            return _aggr_action(state, 1.1 if rng.random() < 0.3 else 0.8)   # value/overbet
        if eq >= 0.62:
            return _aggr_action(state, 0.75)                                 # value
        if has_draw:
            if rng.random() < (0.85 if hu else 0.55):
                return _aggr_action(state, 0.7)                              # semi-bluff
            return {"action": "check"}
        if eq >= 0.45:
            # medium: deny equity sometimes, pot-control otherwise
            if hu and rng.random() < 0.45 * of:
                return _aggr_action(state, 0.5)
            return {"action": "check"}
        # AIR: pure bluff. Frequency scales with heads-up + opponent overfolding.
        base = (0.62 if hu else 0.22)
        if rng.random() < base * of:
            return _aggr_action(state, 0.7)
        return {"action": "check"}

    # ---------------- facing a bet -------------------------------------------
    call_cost = owed
    pot_odds = call_cost / (pot + call_cost) if (pot + call_cost) > 0 else 1.0
    respect = _villain_passive_and_betting(state)    # quiet villain betting = value

    if eq >= 0.82:
        return _aggr_action(state, 0.9)              # value raise
    if eq >= 0.60 and not respect:
        return {"action": "call"}                    # strong-ish bluff-catch
    if has_draw and eq + (0.30 if fd else 0.22) >= pot_odds:
        # semi-bluff raise sometimes, else call with odds
        if hu and rng.random() < 0.40 * of and not respect:
            return _aggr_action(state, 0.9)
        return {"action": "call"}
    if eq >= 0.55 and eq >= pot_odds and not respect:
        return {"action": "call"}                    # marginal call only vs non-passive
    # DISCIPLINE: do not pay off. Fold marginal/air to bets, esp. vs value.
    # Occasional bluff-raise in position vs a small bet from a weak-looking line.
    if (not respect) and hu and eq < 0.35 and owed < 0.4 * pot \
            and rng.random() < 0.12 * of:
        return _aggr_action(state, 1.0)              # float-raise bluff
    return {"action": "fold"}


# ---------------------------------------------------------------------------
# entry
# ---------------------------------------------------------------------------
def decide(game_state):
    if isinstance(game_state, dict) and game_state.get("type") == "warmup":
        if _HAVE_EVAL7:
            try:
                _equity(["As", "Kh"], ["2c", "7d", "9s"], 1, random.Random(0), iters=50)
            except Exception:
                pass
        return {"action": "fold"}
    try:
        _update_opp(game_state)
        hole = _cards(game_state.get("your_cards"))
        board = _cards(game_state.get("community_cards"))
        rng = _spot_rng(game_state)
        street = str(game_state.get("street", "")).lower()
        if street.startswith("pre") or len(board) < 3:
            out = _preflop(game_state, hole, rng)
        else:
            out = _postflop(game_state, hole, board, rng)
        return _sanitize(out, game_state)
    except Exception:
        return _emergency(game_state)


def _sanitize(out, state):
    if not isinstance(out, dict) or out.get("action") not in (
            "fold", "check", "call", "raise", "all_in"):
        return _emergency(state)
    if out["action"] == "check" and _num(state.get("amount_owed")) > 0:
        return {"action": "call"}                    # can't check facing a bet
    if out["action"] == "raise":
        if not isinstance(out.get("amount"), (int, float)):
            return {"action": "call"} if _num(state.get("amount_owed")) > 0 \
                else {"action": "check"}
        out["amount"] = int(out["amount"])
    return out


def _emergency(state):
    try:
        if state.get("can_check", False) or _num(state.get("amount_owed")) <= 0:
            return {"action": "check"}
    except Exception:
        pass
    return {"action": "fold"}
