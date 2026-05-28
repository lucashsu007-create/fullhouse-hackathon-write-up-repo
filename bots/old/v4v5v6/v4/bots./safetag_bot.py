"""
SafeTAG baseline — Fullhouse Hackathon.

Versions 0 + 1 of the AdaptiveMetaExploitBot build order:
  V0  legal skeleton  : never crash, always return a legal action, fold fallback
  V1  SafeTAG         : tight-aggressive preflop ranges, pot-odds calls,
                        value-heavy postflop, low bluff

Deliberately dependency-light: stdlib only. Hand strength is a fast heuristic,
not Monte Carlo — the equity engine (eval7) is a later version. This keeps V1
well inside the 2s budget and the validator's import rules, and gives the A/B
harness a clean, deterministic baseline to measure the exploit layer against.

Design stance (from the framework):
  Safe by default. Value-heavy against callers. Bluff rarely, on dry boards.
  Respect strong aggression. Never blow up.
"""

import random
import zlib

BOT_NAME = "SafeTAG"
BOT_AVATAR = "robot_1"

_RANKS = "23456789TJQKA"
_RANK_VAL = {r: i for i, r in enumerate(_RANKS, start=2)}  # 2..14


# ---------------------------------------------------------------------------
# Preflop hand classification
# ---------------------------------------------------------------------------

def _preflop_class(cards):
    """Return one of: premium, strong, playable, speculative, trash.
    Mirrors the SafeTAG preflop chart in the framework."""
    try:
        r1, s1 = cards[0][0], cards[0][1]
        r2, s2 = cards[1][0], cards[1][1]
    except (IndexError, TypeError):
        return "trash"

    v1, v2 = _RANK_VAL.get(r1, 0), _RANK_VAL.get(r2, 0)
    hi, lo = max(v1, v2), min(v1, v2)
    pair = (r1 == r2)
    suited = (s1 == s2)
    gap = hi - lo

    # Premium: AA KK QQ AK
    if pair and hi >= 12:                      # QQ+
        return "premium"
    if hi == 14 and lo == 13:                  # AK (s or o)
        return "premium"

    # Strong: JJ TT AQ AJs KQs
    if pair and hi >= 10:                      # JJ, TT
        return "strong"
    if hi == 14 and lo == 12:                  # AQ
        return "strong"
    if hi == 14 and lo == 11 and suited:       # AJs
        return "strong"
    if hi == 13 and lo == 12 and suited:       # KQs
        return "strong"

    # Playable: 99-77, ATs, KJs, QJs, KQo
    if pair and 7 <= hi <= 9:                  # 99 88 77
        return "playable"
    if hi == 14 and lo == 10 and suited:       # ATs
        return "playable"
    if hi == 13 and lo == 11 and suited:       # KJs
        return "playable"
    if hi == 12 and lo == 11 and suited:       # QJs
        return "playable"
    if hi == 13 and lo == 12:                  # KQo (suited handled above)
        return "playable"

    # Speculative: 66-22, suited connectors, suited aces
    if pair:                                   # 66..22
        return "speculative"
    if suited and hi == 14:                    # any suited ace
        return "speculative"
    if suited and gap == 1 and lo >= 5:        # suited connectors 65s+
        return "speculative"

    return "trash"


_RANK_ORDER = {"premium": 4, "strong": 3, "playable": 2,
               "speculative": 1, "trash": 0}


def _at_least(cls, floor):
    return _RANK_ORDER[cls] >= _RANK_ORDER[floor]


# ---------------------------------------------------------------------------
# Postflop made-hand / draw heuristic (no eval7 — fast and good enough for V1)
# ---------------------------------------------------------------------------

def _postflop_strength(hole, board):
    """Coarse bucket: strong / medium / weak / air, plus draw flags.
    Returns (bucket, has_flush_draw, has_oesd)."""
    try:
        cards = hole + board
        ranks = [c[0] for c in cards]
        suits = [c[1] for c in cards]
    except (TypeError, IndexError):
        return "air", False, False

    rank_counts = {}
    for r in ranks:
        rank_counts[r] = rank_counts.get(r, 0) + 1
    counts = sorted(rank_counts.values(), reverse=True)

    hole_vals = sorted((_RANK_VAL.get(c[0], 0) for c in hole), reverse=True)
    board_vals = set(_RANK_VAL.get(c[0], 0) for c in board)

    # Made hands by pair structure across hole+board
    if counts and counts[0] >= 4:
        return "strong", False, False                  # quads
    if len(counts) >= 2 and counts[0] >= 3 and counts[1] >= 2:
        return "strong", False, False                  # full house
    if counts and counts[0] >= 3:
        return "strong", False, False                  # trips/set
    if counts.count(2) >= 2:
        return "strong", False, False                  # two pair

    # Flush draw / made flush
    suit_counts = {}
    for s in suits:
        suit_counts[s] = suit_counts.get(s, 0) + 1
    max_suit = max(suit_counts.values()) if suit_counts else 0
    made_flush = max_suit >= 5
    flush_draw = max_suit == 4
    if made_flush:
        return "strong", False, False

    # Straight detection (made + open-ended draw)
    uniq = sorted(set(_RANK_VAL.get(r, 0) for r in ranks))
    if 14 in uniq:                                     # wheel ace
        uniq = sorted(set(uniq + [1]))
    made_straight = _has_run(uniq, 5)
    oesd = (not made_straight) and _has_run(uniq, 4)
    if made_straight:
        return "strong", False, False

    # One pair — distinguish top/over pair from weak pair
    if counts and counts[0] == 2:
        paired_rank = next(_RANK_VAL.get(r, 0)
                           for r, c in rank_counts.items() if c == 2)
        top_board = max(board_vals) if board_vals else 0
        if paired_rank > top_board or paired_rank in hole_vals and hole_vals[0] >= top_board:
            return "medium", flush_draw, oesd          # top pair / overpair-ish
        return "weak", flush_draw, oesd                # middle/bottom pair

    # No pair: overcards count as air-ish unless strong draw
    if flush_draw or oesd:
        return "weak", flush_draw, oesd
    return "air", flush_draw, oesd


def _has_run(sorted_vals, length):
    if len(sorted_vals) < length:
        return False
    run = 1
    for i in range(1, len(sorted_vals)):
        if sorted_vals[i] == sorted_vals[i - 1] + 1:
            run += 1
            if run >= length:
                return True
        else:
            run = 1
    return False


# ---------------------------------------------------------------------------
# Position
# ---------------------------------------------------------------------------

def _position(state):
    """0.0 = early, 1.0 = late. Best-effort from public info."""
    try:
        seat = state["seat_to_act"]
        n = len(state["players"]) or 1
        return seat / max(n - 1, 1)
    except Exception:
        return 0.5


def _facing_raise(state):
    """Preflop: is there action beyond the blinds in front of us?"""
    try:
        for a in state.get("action_log", []):
            if a.get("action") in ("raise", "all_in"):
                return True
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# Bet sizing helpers (always clamped legal)
# ---------------------------------------------------------------------------

def _raise_to(state, pot_fraction):
    """Total-bet target = current_bet + pot_fraction*pot, clamped to [min_raise_to, all-in]."""
    pot = state.get("pot", 0)
    min_to = state.get("min_raise_to", 0)
    my_bet = state.get("your_bet_this_street", 0)
    stack = state.get("your_stack", 0)
    cap = stack + my_bet                       # all-in total
    target = state.get("current_bet", 0) + int(pot * pot_fraction)
    target = max(target, min_to)
    target = min(target, cap)
    return target


def _pot_odds_ok(state, max_required_equity):
    """True if calling price is cheap enough (required equity below threshold)."""
    owed = state.get("amount_owed", 0)
    pot = state.get("pot", 0)
    if owed <= 0:
        return True
    req = owed / (pot + owed)
    return req <= max_required_equity


# ---------------------------------------------------------------------------
# Core SafeTAG policy
# ---------------------------------------------------------------------------

def _spot_seed(state):
    """Stable 32-bit seed for THIS decision point (see safetag_eq_bot.py).
    Must match the eq-bot's scheme so identical spots roll identically across
    both bots, keeping cross-version A/B comparisons RNG-clean."""
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


def _safetag(state):
    can_check = state.get("can_check", False)
    owed = state.get("amount_owed", 0)
    street = state.get("street", "preflop")
    hole = state.get("your_cards", []) or []
    board = state.get("community_cards", []) or []
    pos = _position(state)
    rng = _spot_rng(state)

    # ---- PREFLOP ----
    if street == "preflop":
        cls = _preflop_class(hole)
        facing = _facing_raise(state)

        if not facing:
            # Unopened pot.
            open_floor = "playable" if pos > 0.5 else "strong"
            if _at_least(cls, open_floor):
                return {"action": "raise", "amount": _raise_to(state, 1.0)}  # ~pot-sized open
            if can_check:
                return {"action": "check"}
            # facing only blinds, cheap completes with speculative hands
            if _at_least(cls, "speculative") and _pot_odds_ok(state, 0.18):
                return {"action": "call"}
            return {"action": "fold"}

        # Facing a raise.
        if cls == "premium":
            return {"action": "raise", "amount": _raise_to(state, 1.0)}  # 3-bet
        if cls == "strong":
            return {"action": "call"}
        if cls == "playable" and (pos > 0.5 or _pot_odds_ok(state, 0.12)):
            return {"action": "call"}
        if can_check:
            return {"action": "check"}
        return {"action": "fold"}

    # ---- POSTFLOP ----
    bucket, fd, oesd = _postflop_strength(hole, board)
    drawy = fd or oesd

    if bucket == "strong":
        # Value bet / raise for value.
        if can_check:
            return {"action": "raise", "amount": _raise_to(state, 0.66)}
        # facing a bet — raise sometimes, otherwise call to keep them in
        if rng.random() < 0.55:
            return {"action": "raise", "amount": _raise_to(state, 0.9)}
        return {"action": "call"}

    if bucket == "medium":
        # Showdown value: bet thin / check-call reasonable sizes.
        if can_check:
            if pos > 0.55 and rng.random() < 0.6:
                return {"action": "raise", "amount": _raise_to(state, 0.5)}
            return {"action": "check"}
        if _pot_odds_ok(state, 0.33):
            return {"action": "call"}
        return {"action": "fold"}

    if drawy:
        # Draw: call if priced in; small semi-bluff occasionally when checked to.
        if can_check:
            if rng.random() < 0.25:
                return {"action": "raise", "amount": _raise_to(state, 0.5)}
            return {"action": "check"}
        if _pot_odds_ok(state, 0.30):
            return {"action": "call"}
        return {"action": "fold"}

    # Air.
    if can_check:
        # Rare c-bet bluff on dry-ish late-position spots only.
        dry = len(board) >= 3 and _board_is_dry(board)
        if pos > 0.6 and dry and rng.random() < 0.2:
            return {"action": "raise", "amount": _raise_to(state, 0.5)}
        return {"action": "check"}
    return {"action": "fold"}


def _board_is_dry(board):
    """Rainbow-ish, unconnected, no pair — cheap proxy."""
    try:
        suits = [c[1] for c in board]
        vals = sorted(_RANK_VAL.get(c[0], 0) for c in board)
    except Exception:
        return False
    max_suit = max((suits.count(s) for s in set(suits)), default=0)
    if max_suit >= 3:
        return False                          # flushy
    if len(set(c[0] for c in board)) < len(board):
        return False                          # paired
    spread = vals[-1] - vals[0] if vals else 0
    return spread >= 5                        # spaced out


# ---------------------------------------------------------------------------
# Safety wrapper — legalise, never crash
# ---------------------------------------------------------------------------

def _sanitize(action, state):
    """Force the proposed action into something the engine will accept."""
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
        amt = action.get("amount")
        try:
            amt = int(amt)
        except (TypeError, ValueError):
            amt = state.get("min_raise_to", 0)
        min_to = state.get("min_raise_to", 0)
        cap = state.get("your_stack", 0) + state.get("your_bet_this_street", 0)
        amt = max(amt, min_to)
        if amt >= cap:                        # would be all-in anyway
            return {"action": "all_in"}
        return {"action": "raise", "amount": amt}
    # default
    return {"action": "fold"}


def _emergency(state):
    """Last-resort fallback from the framework's decision pipeline."""
    try:
        if state.get("can_check", False):
            return {"action": "check"}
        owed = state.get("amount_owed", 0)
        pot = state.get("pot", 1)
        if owed > 0 and owed < pot * 0.15:
            return {"action": "call"}
    except Exception:
        pass
    return {"action": "fold"}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def decide(game_state: dict) -> dict:
    # Warm-up ping from the match runner: just return quickly.
    if isinstance(game_state, dict) and game_state.get("type") == "warmup":
        return {"action": "fold"}
    try:
        proposed = _safetag(game_state)
        return _sanitize(proposed, game_state)
    except Exception:
        return _emergency(game_state)
