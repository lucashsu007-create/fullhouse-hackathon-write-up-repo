"""
Position detection.

The Fullhouse tournament is 6-max, but the GTO charts you've encoded are
8-max. We need to:

  1. Figure out which seat the bot is in.
  2. Figure out what "position name" that maps to in the GTO chart.

Position detection inputs the engine's `game_state` dict. Because the engine
schema for `players` and `action_log` isn't pinned down in the README, we
make the function robust to a few likely shapes — adapt the field names if
your local engine uses different ones.
"""

# Standard 8-max chart positions, in action order pre-flop.
EIGHTMAX_ORDER = ["UTG", "UTG1", "LJ", "HJ", "CO", "BTN", "SB", "BB"]

# Standard 6-max action order.
SIXMAX_ORDER = ["UTG", "HJ", "CO", "BTN", "SB", "BB"]

# 6-max -> 8-max chart mapping.
# Rationale: in 6-max each non-blind seat is one "step closer" to the button
# than the analogous 8-max position. The closest looseness match is:
#   6-max UTG (4 off the button) ~= 8-max LJ (4 off the button)
#   6-max HJ  (3 off the button) ~= 8-max HJ
#   6-max CO  (2 off the button) ~= 8-max CO
#   6-max BTN (1 off the button) ~= 8-max BTN
#   6-max SB                    -> SB
#   6-max BB                    -> BB
SIXMAX_TO_CHART = {
    "UTG": "LJ",
    "HJ": "HJ",
    "CO": "CO",
    "BTN": "BTN",
    "SB": "SB",
    "BB": "BB",
}


def detect_position(game_state):
    """Best-effort position string from a game_state dict.

    Returns a position name from SIXMAX_ORDER if the table is 6-handed, or
    from EIGHTMAX_ORDER if it's 8-handed. Falls back to None if we can't
    figure it out.

    Position is determined by SEAT (not by who's still in the hand), since
    positions are fixed at the start of the hand.
    """
    players = game_state.get("players")
    if not players:
        return None

    # First check if the player's seat is labeled directly. Engines often
    # expose a 'position' field on each player — use that if present.
    for p in players:
        if isinstance(p, dict) and (p.get("is_me") or p.get("is_self") or p.get("hero")):
            label = p.get("position")
            if label and isinstance(label, str) and label.upper() in (
                "UTG", "UTG1", "UTG2", "LJ", "HJ", "MP", "CO", "BTN", "SB", "BB"
            ):
                # Normalize MP to HJ for 6-max.
                lbl = label.upper()
                return "HJ" if lbl == "MP" else lbl

    n = len(players)  # Use full table size, not just active players.

    btn_idx = _find_button_index(players)
    me_idx = _find_my_index(players)
    if btn_idx is None or me_idx is None:
        return None

    # Pre-flop action order: SB, BB post first, then UTG acts first. Seat
    # indices in *action order* (UTG first ... BB last):
    #   action_order[0] = (btn + 3) % n  (UTG)
    #   ...
    #   action_order[n-3] = (btn) % n    (BTN)
    #   action_order[n-2] = (btn + 1) % n (SB)
    #   action_order[n-1] = (btn + 2) % n (BB)
    if n == 2:
        # Heads-up: SB is on the button.
        if me_idx == btn_idx:
            return "SB"
        return "BB"

    action_order = []
    for i in range(n - 2):
        action_order.append((btn_idx + 3 + i) % n)
    action_order.append((btn_idx + 1) % n)  # SB
    action_order.append((btn_idx + 2) % n)  # BB

    if n == 6:
        labels = SIXMAX_ORDER
    elif n == 8:
        labels = EIGHTMAX_ORDER
    else:
        labels = _generic_labels(n)

    for ord_idx, seat in enumerate(action_order):
        if seat == me_idx:
            return labels[ord_idx] if ord_idx < len(labels) else None
    return None


def chart_position(game_state):
    """Map detected seat label to a chart key in `RANGES_BY_POSITION`.

    Use this when querying ranges from `gto.ranges`.
    """
    pos = detect_position(game_state)
    if pos is None:
        return None
    players = game_state.get("players") or []
    if len(players) <= 6:
        return SIXMAX_TO_CHART.get(pos, pos)
    return pos


def is_unopened(game_state):
    """True if no player has raised yet pre-flop (we're facing only blinds)."""
    if game_state.get("street", "preflop") != "preflop":
        return False
    log = game_state.get("action_log", [])
    for entry in log:
        action = (entry.get("action") or "").lower() if isinstance(entry, dict) else str(entry).lower()
        # Anything except blind posts / folds / checks would indicate a raise.
        # Be conservative — bet/raise/all_in count as "opened."
        if any(tok in action for tok in ("raise", "bet", "all_in", "allin")):
            return False
    return True


# ---------------------------------------------------------------------------
# Internal helpers — these guess at the engine schema. Adapt if needed.
# ---------------------------------------------------------------------------
def _is_active(player):
    if isinstance(player, dict):
        if player.get("folded"):
            return False
        if player.get("status") == "folded":
            return False
        if player.get("in_hand") is False:
            return False
    return True


def _find_button_index(players):
    for i, p in enumerate(players):
        if not isinstance(p, dict):
            continue
        if p.get("is_button") or p.get("dealer") or p.get("position") == "BTN":
            return i
    return None


def _find_my_index(players):
    for i, p in enumerate(players):
        if not isinstance(p, dict):
            continue
        if p.get("is_me") or p.get("is_self") or p.get("hero"):
            return i
    return None


def _generic_labels(n):
    """Build a positional label list for non-standard table sizes."""
    # Always end with SB, BB. Fill UTG/HJ/CO/BTN-equivalents before them.
    base = ["UTG"] + [f"UTG{i}" for i in range(1, n - 5)] + ["LJ", "HJ", "CO", "BTN"]
    base = base[-(n - 2):]  # trim
    return base + ["SB", "BB"]
