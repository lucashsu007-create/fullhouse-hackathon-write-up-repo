#!/usr/bin/env python3
"""
D1 FIELD PROFILER + RANGE CALIBRATOR
====================================
Consumes the downloadable D1 hand-history JSON and produces:
  (1) a per-opponent exploitability profile (VPIP/PFR, fold-to-cbet,
      raise-vs-bet, aggression, 3bet%, WTSD), and
  (2) calibrated range strings for v13 (_RANGE_STRINGS), fit to the actual
      SHOWDOWN holdings grouped by the line the villain took.

WHY THIS EXISTS
  v13's range strings (CONTINUE, OPEN_WIDE, THREEBET_WIDE, ...) are eyeballed.
  The D1 histories contain real showdown holdings tied to action lines, which is
  ground truth. This script turns those showdowns into range strings you can
  paste straight into _RANGE_STRINGS, and tells you which opponents (and which
  levers) are worth a finals patch.

USAGE
  python3 d1_profiler.py sniff   histories/         # inspect unknown schema
  python3 d1_profiler.py profile histories/         # per-opponent stats
  python3 d1_profiler.py ranges  histories/ --hero v7-m2   # calibrate ranges

IMPORTANT — SCHEMA ADAPTATION
  The downloadable format is not documented in the repo. This script normalizes
  via the KEYMAP block below with multi-name fallbacks. Run `sniff` first; if a
  field isn't auto-found, add the real key name to the relevant list in KEYMAP.
  That single block is the only thing that should ever need editing.

IMPORTANT — SHOWDOWN BIAS (read before trusting widths)
  You only see hands that REACHED showdown. Hands that folded earlier, or won
  without showdown, are invisible. So a calibrated range is the SHOWDOWN-REVEALED
  subset of the true range: it is reliable for the made-hand / value part of a
  line, and it UNDER-counts the bluffs and give-ups. Treat the emitted string as
  a lower bound on width; the profiler also prints an action-frequency-implied
  width (from VPIP/PFR) as a cross-check so you can widen with judgment.
"""

import json, os, sys, glob, math
import io, contextlib
from collections import defaultdict, Counter

# ---------------------------------------------------------------------------
# SCHEMA ADAPTATION — edit ONLY this block if `sniff` shows different keys.
# Each logical field maps to a list of candidate JSON keys, tried in order.
# ---------------------------------------------------------------------------
KEYMAP = {
    "hands_list":   ["hands", "hand_histories", "history", "records", "log"],
    "hand_id":      ["hand_id", "id", "hand", "hand_number", "n"],
    "board":        ["board", "community_cards", "community", "cards"],
    "actions":      ["actions", "action_log", "acts", "events"],
    "showdown":     ["showdown", "showdowns", "shows", "revealed", "holdings"],
    # within an action record:
    "act_seat":     ["seat", "player", "bot", "bot_id", "name", "actor", "pos"],
    "act_street":   ["street", "round", "phase", "street_idx", "street_index"],
    "act_action":   ["action", "act", "type", "move", "decision"],
    "act_amount":   ["amount", "size", "amt", "chips", "to", "bet"],
    # within a showdown record / map:
    "sd_seat":      ["seat", "player", "bot", "bot_id", "name"],
    "sd_cards":     ["cards", "hole", "holding", "hand", "hole_cards"],
}
STREET_NAMES = {0: "preflop", 1: "flop", 2: "turn", 3: "river"}
STREET_IDX = {"preflop": 0, "flop": 1, "turn": 2, "river": 3,
              "pre": 0, "f": 1, "t": 2, "r": 3, "0": 0, "1": 1, "2": 2, "3": 3}

_RANKS = "23456789TJQKA"
_RANK_VAL = {r: i for i, r in enumerate(_RANKS, start=2)}
_SUITS = "shdc"


# ---------------------------------------------------------------------------
# generic schema-tolerant accessors
# ---------------------------------------------------------------------------
def _get(d, logical, default=None):
    if not isinstance(d, dict):
        return default
    for k in KEYMAP[logical]:
        if k in d:
            return d[k]
    return default


def _street_to_idx(v):
    if isinstance(v, int):
        return v if v in (0, 1, 2, 3) else 0
    if isinstance(v, str):
        return STREET_IDX.get(v.strip().lower(), 0)
    return 0


def _canonical(cards):
    """Two cards -> 'AKs'/'AKo'/'77'. Mirrors v7_m2._canonical."""
    if not cards or len(cards) != 2:
        return None
    try:
        r1, s1 = cards[0][0].upper(), cards[0][1].lower()
        r2, s2 = cards[1][0].upper(), cards[1][1].lower()
    except (IndexError, AttributeError, TypeError):
        return None
    if r1 not in _RANK_VAL or r2 not in _RANK_VAL:
        return None
    if r1 == r2:
        return r1 + r2
    if _RANK_VAL[r1] < _RANK_VAL[r2]:
        r1, r2, s1, s2 = r2, r1, s2, s1
    return r1 + r2 + ("s" if s1 == s2 else "o")


# ---------------------------------------------------------------------------
# normalization — raw hand JSON -> canonical structure
#   {hand_id, board:[...], actions:[{seat,street,action,amount}], showdown:{seat:[cards]}}
# ---------------------------------------------------------------------------
def normalize_hand(raw):
    hid = _get(raw, "hand_id", "?")
    board = _get(raw, "board", []) or []
    acts_raw = _get(raw, "actions", []) or []
    actions = []
    for a in acts_raw:
        if not isinstance(a, dict):
            continue
        seat = _get(a, "act_seat")
        action = _get(a, "act_action")
        if seat is None or action is None:
            continue
        actions.append({
            "seat": str(seat),
            "street": _street_to_idx(_get(a, "act_street", 0)),
            "action": str(action).strip().lower(),
            "amount": _to_num(_get(a, "act_amount", 0)),
        })
    # showdown can be a list of records or a {seat: cards} map
    sd_raw = _get(raw, "showdown", None)
    showdown = {}
    if isinstance(sd_raw, dict):
        for seat, cards in sd_raw.items():
            cc = cards.get("cards") if isinstance(cards, dict) else cards
            showdown[str(seat)] = cc
    elif isinstance(sd_raw, list):
        for rec in sd_raw:
            seat = _get(rec, "sd_seat")
            cc = _get(rec, "sd_cards")
            if seat is not None and cc:
                showdown[str(seat)] = cc
    return {"hand_id": hid, "board": board, "actions": actions, "showdown": showdown}


def _to_num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def load_hands(path):
    """Accepts a dir of JSON files or a single JSON file; yields normalized hands.
    Tolerates either a top-level list of hands or a dict containing one."""
    files = []
    if os.path.isdir(path):
        files = sorted(glob.glob(os.path.join(path, "*.json")))
    else:
        files = [path]
    for f in files:
        try:
            data = json.load(open(f))
        except Exception as e:
            print(f"  [skip] {f}: {e}", file=sys.stderr)
            continue
        hands = data if isinstance(data, list) else _get(data, "hands_list", None)
        if hands is None and isinstance(data, dict):
            hands = [data]                       # file IS a single hand
        for raw in (hands or []):
            yield normalize_hand(raw)


# ---------------------------------------------------------------------------
# per-hand line reconstruction (who raised pre, who c-bet, who faced bets)
# ---------------------------------------------------------------------------
RAISE_ACTS = {"raise", "bet", "all_in", "allin", "all-in", "raises", "bets"}
CALL_ACTS = {"call", "calls", "check", "checks"}        # voluntary continue
FOLD_ACTS = {"fold", "folds"}
AGGR_ACTS = {"raise", "bet", "all_in", "allin", "all-in", "raises", "bets"}


def analyze_hand(hand):
    """Returns per-seat events for stat accumulation:
       seat -> dict(vpip, pfr, threebet, faced_cbet, folded_cbet, faced_bet,
                    raised_vs_bet, aggr_acts, total_acts, line_tag)
    line_tag is the pre+flop line used to bucket the showdown holding."""
    acts = hand["actions"]
    seats = []
    for a in acts:
        if a["seat"] not in seats:
            seats.append(a["seat"])
    ev = {s: dict(vpip=0, pfr=0, threebet=0, faced_cbet=0, folded_cbet=0,
                  faced_bet=0, raised_vs_bet=0, called_postflop=0,
                  aggr_acts=0, total_acts=0, line_tag=None,
                  opened=False, threebet_pre=False) for s in seats}

    # preflop pass: opener / 3bettor / voluntary
    pre = [a for a in acts if a["street"] == 0]
    n_pre_raises = 0
    opener = None
    for a in pre:
        s = a["seat"]
        ev[s]["total_acts"] += 1
        if a["action"] in AGGR_ACTS:
            ev[s]["aggr_acts"] += 1
        if a["action"] not in ("check", "checks", "fold", "folds"):
            ev[s]["vpip"] = 1                                   # voluntary chips
        if a["action"] in RAISE_ACTS:
            n_pre_raises += 1
            if n_pre_raises == 1:
                ev[s]["pfr"] = 1; ev[s]["opened"] = True; opener = s
            elif n_pre_raises == 2:
                ev[s]["threebet"] = 1; ev[s]["threebet_pre"] = True

    # per-street facing-a-bet detection (a "bet" is the first aggressive action
    # on a postflop street that others must answer)
    for st in (1, 2, 3):
        street_acts = [a for a in acts if a["street"] == st]
        bet_made = False
        bettor = None
        for a in street_acts:
            s = a["seat"]
            ev[s]["total_acts"] += 1
            if a["action"] in AGGR_ACTS:
                ev[s]["aggr_acts"] += 1
            if bet_made and s != bettor:
                ev[s]["faced_bet"] += 1
                if a["action"] in RAISE_ACTS:
                    ev[s]["raised_vs_bet"] += 1
                # c-bet = bettor on the flop was the preflop opener
                if st == 1 and bettor == opener:
                    ev[s]["faced_cbet"] += 1
                    if a["action"] in FOLD_ACTS:
                        ev[s]["folded_cbet"] += 1
            if a["action"] in ("call", "calls") and bet_made and s != bettor:
                ev[s]["called_postflop"] += 1
            if a["action"] in AGGR_ACTS and not bet_made:
                bet_made = True; bettor = s

    # line tag for showdown bucketing (pre + whether they peeled flop)
    for s in seats:
        peeled = ev[s]["called_postflop"]
        if ev[s]["threebet_pre"]:
            ev[s]["line_tag"] = "THREEBET"
        elif ev[s]["opened"]:
            ev[s]["line_tag"] = "OPEN"
        elif ev[s]["vpip"]:
            ev[s]["line_tag"] = ("CONTINUE_TIGHT" if peeled >= 2 else
                                 "CONTINUE" if peeled >= 1 else "WIDE")
        else:
            ev[s]["line_tag"] = None
    return ev


# ---------------------------------------------------------------------------
# PROFILE — aggregate exploitability stats per opponent
# ---------------------------------------------------------------------------
def profile(path):
    agg = defaultdict(lambda: Counter())
    nhands = 0
    for hand in load_hands(path):
        nhands += 1
        ev = analyze_hand(hand)
        for s, e in ev.items():
            a = agg[s]
            a["hands"] += 1
            for k in ("vpip", "pfr", "threebet", "faced_cbet", "folded_cbet",
                      "faced_bet", "raised_vs_bet", "aggr_acts", "total_acts"):
                a[k] += e[k]
    return nhands, agg


def _rate(num, den):
    return (num / den) if den else None


def compute_profile_rows(agg):
    """Shared rate + exploit-flag computation behind both the CLI ``profile``
    table and the dashboard, so the two never drift. Takes the aggregate dict
    from ``profile()`` and returns a list of row dicts sorted by hands desc:
        {opponent, hands, vpip, pfr, threebet, fold_to_cbet, raise_vs_bet,
         aggr, flags}
    where rate fields are floats in 0..1 or None (insufficient denominator)."""
    rows = []
    for s, a in agg.items():
        vpip = _rate(a["vpip"], a["hands"])
        pfr = _rate(a["pfr"], a["hands"])
        tb = _rate(a["threebet"], a["hands"])
        fcb = _rate(a["folded_cbet"], a["faced_cbet"])
        rvb = _rate(a["raised_vs_bet"], a["faced_bet"])
        aggr = _rate(a["aggr_acts"], a["total_acts"])
        # exploit flags: who is worth which lever
        flags = []
        if fcb is not None and fcb > 0.55 and (rvb or 0) < 0.12:
            flags.append("CBET-bluffable")          # folds to cbets, rarely raises
        if rvb is not None and rvb > 0.15:
            flags.append("CHECK-RAISER(gate-off)")  # punishes bets
        if aggr is not None and aggr > 0.35:
            flags.append("OVER-AGGRESSIVE(call-down)")
        if vpip is not None and vpip < 0.18:
            flags.append("NIT")
        if vpip is not None and fcb is not None and vpip > 0.40 and fcb < 0.30:
            flags.append("STATION(no-bluff)")
        rows.append({
            "opponent": s, "hands": a["hands"], "vpip": vpip, "pfr": pfr,
            "threebet": tb, "fold_to_cbet": fcb, "raise_vs_bet": rvb,
            "aggr": aggr, "flags": flags,
        })
    rows.sort(key=lambda r: -r["hands"])
    return rows


def print_profile(nhands, agg):
    print(f"\nProfiled {nhands} hands, {len(agg)} opponents.\n")
    hdr = (f"{'opponent':<18}{'hands':>6}{'VPIP':>7}{'PFR':>7}{'3bet':>7}"
           f"{'fold→cbet':>11}{'raise/bet':>11}{'aggr%':>7}  exploit-flag")
    print(hdr); print("-" * len(hdr))

    def fmt(x):
        return "  -  " if x is None else f"{100*x:>5.1f}%"
    for r in compute_profile_rows(agg):
        print(f"{r['opponent']:<18}{r['hands']:>6}{fmt(r['vpip']):>7}"
              f"{fmt(r['pfr']):>7}{fmt(r['threebet']):>7}"
              f"{fmt(r['fold_to_cbet']):>11}{fmt(r['raise_vs_bet']):>11}"
              f"{fmt(r['aggr']):>7}  {', '.join(r['flags'])}")
    print("\nLever guidance:")
    print("  Many CBET-bluffable & few CHECK-RAISER  -> the v12 c-bet is worth patching.")
    print("  Many OVER-AGGRESSIVE                     -> widen aggressor ranges (v13_lag).")
    print("  Mostly competent (no flags)              -> keep v7_m2; don't patch.")


# ---------------------------------------------------------------------------
# RANGES — calibrate _RANGE_STRINGS from showdown holdings, grouped by line
# ---------------------------------------------------------------------------
def calibrate_ranges(path, hero=None, min_combos=8):
    """Collect showdown holdings bucketed by the showing seat's line tag, across
    ALL opponents (the field's population range per line). Returns
    bucket -> Counter(hand_class) and the implied range string."""
    buckets = defaultdict(Counter)
    for hand in load_hands(path):
        ev = analyze_hand(hand)
        for seat, cards in hand["showdown"].items():
            if seat not in ev:
                continue
            if hero is not None and seat == hero:
                continue                              # field only, not us
            tag = ev[seat]["line_tag"]
            cls = _canonical(cards)
            if tag and cls:
                buckets[tag][cls] += 1
    out = {}
    for tag, ctr in buckets.items():
        total = sum(ctr.values())
        out[tag] = {
            "n": total,
            "classes": ctr,
            "range_str": _classes_to_range_str(ctr) if total >= min_combos else None,
        }
    return out


def _classes_to_range_str(ctr):
    """Compact a Counter of hand classes into a readable range string. Sorts
    pairs high->low, then suited, then offsuit, each by descending rank. Keeps
    every class observed at least once (showdown = revealed truth)."""
    pairs, suited, offsuit = [], [], []
    for cls in ctr:
        if len(cls) == 2:
            pairs.append(cls)
        elif cls.endswith("s"):
            suited.append(cls)
        else:
            offsuit.append(cls)
    pk = lambda c: _RANK_VAL[c[0]]
    sk = lambda c: (_RANK_VAL[c[0]], _RANK_VAL[c[1]])
    pairs.sort(key=pk, reverse=True)
    suited.sort(key=sk, reverse=True)
    offsuit.sort(key=sk, reverse=True)
    return ", ".join(pairs + suited + offsuit)


_RANGE_BUCKET_ORDER = ["WIDE", "CONTINUE", "CONTINUE_TIGHT", "OPEN", "THREEBET"]
_RANGE_BUCKET_NAMES = {
    "WIDE": "WIDE (limp/call, no peel)",
    "CONTINUE": "CONTINUE (called 1 postflop bet)",
    "CONTINUE_TIGHT": "CONTINUE_TIGHT (called 2 postflop bets)",
    "OPEN": "OPEN (raised first in)",
    "THREEBET": "THREEBET (re-raised preflop)",
}


def order_range_buckets(buckets):
    """Flatten the ``calibrate_ranges`` output into a display-ordered list of
    dicts so the CLI and the dashboard render the same buckets in the same order:
        {tag, display_name, n, n_classes, range_str}
    (``range_str`` is None when there were too few showdowns to trust)."""
    out = []
    for tag in _RANGE_BUCKET_ORDER:
        if tag not in buckets:
            continue
        b = buckets[tag]
        out.append({
            "tag": tag,
            "display_name": _RANGE_BUCKET_NAMES.get(tag, tag),
            "n": b["n"],
            "n_classes": len(b["classes"]),
            "range_str": b["range_str"],
        })
    return out


def print_ranges(buckets):
    print(f"\nCalibrated ranges from showdowns ({len(buckets)} line-buckets):\n")
    for b in order_range_buckets(buckets):
        print(f"# {b['display_name']}  (n={b['n']} showdowns)")
        if b["range_str"] is None:
            print(f'    # too few showdowns to trust ({b["n"]}); keep eyeballed string\n')
            continue
        print(f'    "{b["tag"]}":  # {b["n_classes"]} distinct classes observed')
        print(f'        "{b["range_str"]}",\n')
    print("CAVEAT: showdown-revealed = the made-hand/value part of each line.")
    print("It under-counts bluffs and give-ups, so widths are a LOWER BOUND.")
    print("Widen the bluff-heavy buckets (OPEN/THREEBET) with judgment, using the")
    print("PFR/aggr rates from `profile` as a guide to how much air to add back.")


# ---------------------------------------------------------------------------
# SNIFF — inspect an unknown schema and report what maps / what's missing
# ---------------------------------------------------------------------------
def sniff(path):
    files = (sorted(glob.glob(os.path.join(path, "*.json"))) if os.path.isdir(path)
             else [path])
    if not files:
        print("no JSON files found at", path); return
    f = files[0]
    print(f"Sniffing {f}\n")
    data = json.load(open(f))
    top = data if isinstance(data, list) else data
    if isinstance(data, dict):
        print("top-level keys:", list(data.keys()))
        hands = _get(data, "hands_list", None)
        print("hands_list found via:", _found_key(data, "hands_list"))
    else:
        print("top-level is a LIST of", len(data), "items")
        hands = data
    if not hands:
        print("\n!! could not locate the hands list. Add its key to "
              "KEYMAP['hands_list'].")
        if isinstance(data, dict):
            return
        hands = data
    h0 = hands[0]
    print("\nfirst hand keys:", list(h0.keys()) if isinstance(h0, dict) else type(h0))
    for logical in ("hand_id", "board", "actions", "showdown"):
        print(f"  {logical:<10} <- {_found_key(h0, logical)}")
    acts = _get(h0, "actions", []) or []
    if acts:
        print("\nfirst action keys:", list(acts[0].keys()))
        for logical in ("act_seat", "act_street", "act_action", "act_amount"):
            print(f"  {logical:<12} <- {_found_key(acts[0], logical)}")
    sd = _get(h0, "showdown", None)
    print("\nshowdown sample:", json.dumps(sd)[:200] if sd else "(none in hand 0)")
    print("\nIf any field shows '<MISSING>', add the real key to KEYMAP and re-run.")


def _found_key(d, logical):
    if not isinstance(d, dict):
        return "<not-a-dict>"
    for k in KEYMAP[logical]:
        if k in d:
            return repr(k)
    return "<MISSING>"


def sniff_report(path):
    """Return the same schema-inspection text that ``sniff`` prints, as a string,
    so a UI can display it without capturing stdout itself."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        sniff(path)
    return buf.getvalue()


# ---------------------------------------------------------------------------
def main(argv):
    if len(argv) < 3:
        print(__doc__); return 1
    cmd, path = argv[1], argv[2]
    hero = None
    if "--hero" in argv:
        hero = argv[argv.index("--hero") + 1]
    if cmd == "sniff":
        sniff(path)
    elif cmd == "profile":
        n, agg = profile(path); print_profile(n, agg)
    elif cmd == "ranges":
        print_ranges(calibrate_ranges(path, hero=hero))
    else:
        print("unknown command:", cmd); print(__doc__); return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
