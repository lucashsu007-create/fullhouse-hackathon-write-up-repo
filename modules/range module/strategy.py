"""
Strategy orchestrator.

Routes each game state to the right sub-module:
    short stack (≤20bb) + preflop  -> pushfold
    preflop unopened               -> RFI chart
    preflop facing 3-bet (we PFR)  -> 4-bet defense
    preflop facing raise           -> 3-bet defense
    postflop                       -> postflop module

The `decide()` function is the engine's entry point.
"""

import random

from .hand_notation import canonical
from .ranges import get_open_action, OPEN_SIZE_BB
from .position import chart_position, is_unopened
from .defense import defense_action, four_bet_defense, OPENER_TIER
from .pushfold import open_jam_hands, call_jam_range, effective_stack_bb
from .postflop import decide_postflop


# Stack-depth threshold below which we switch to jam-or-fold mode.
SHORT_STACK_BB = 20.0


def decide(game_state, rng=None, equity_iters=1500):
    """Main entry point. Returns one of:
        {"action": "fold"}
        {"action": "check"}
        {"action": "call"}
        {"action": "raise", "amount": <int>}
        {"action": "all_in"}
    """
    rng = rng or random
    try:
        street = game_state.get("street", "preflop")
        if street == "preflop":
            return _preflop(game_state, rng)
        return decide_postflop(game_state, rng, n_iters=equity_iters)
    except Exception:
        # Hard safety net: never crash the bot.
        if game_state.get("can_check"):
            return {"action": "check"}
        return {"action": "fold"}


# ---------------------------------------------------------------------------
# Pre-flop routing
# ---------------------------------------------------------------------------
def _preflop(state, rng):
    hand = canonical(state["your_cards"])
    pos = chart_position(state)
    eff_bb = effective_stack_bb(state)

    # Detect what kind of preflop spot we're in.
    history = _preflop_history(state)
    facing_jam = _is_facing_jam(state)
    n_preflop_raises = history["raises"]

    # --- Short-stack push/fold mode ---
    if eff_bb <= SHORT_STACK_BB:
        return _short_stack(state, hand, pos, history, facing_jam, eff_bb, rng)

    # --- 100bb chart mode ---
    if facing_jam:
        # Someone shoved on us — call range
        return _call_jam_decision(state, hand, history)

    if is_unopened(state) and n_preflop_raises == 0:
        return _open_decision(state, hand, pos, rng)

    if n_preflop_raises == 1 and history["last_raiser_was_us"]:
        # We opened and are now facing a 3-bet
        return _vs_3bet(state, hand, rng)

    if n_preflop_raises >= 1:
        # Someone opened (or 3-bet) before us
        return _defense_decision(state, hand, pos, history, rng)

    # Fall-through (shouldn't reach here)
    return _check_or_fold(state)


def _open_decision(state, hand, pos, rng):
    """First in. Use the RFI chart."""
    if pos is None:
        return _check_or_fold(state)

    # Heads-up override: the 8-max SB chart is heavily limp-based because
    # it assumes 6 opponents already folded. In HU there are no other
    # folders to worry about — SB is the button. Standard HU button opens
    # ~75–85% of hands.
    n_active = _count_seats_in_hand(state)
    if n_active == 2 and pos == "SB":
        return _hu_btn_open(state, hand, rng)

    dist = get_open_action(pos, hand)
    chosen = _sample(dist, rng)

    if chosen == "fold":
        return _check_or_fold(state)
    if chosen == "call":
        return {"action": "call"}
    if chosen == "raise":
        size_bb = OPEN_SIZE_BB.get(pos, 2.5)
        amount = _bb_to_chips(state, size_bb)
        return _legal_raise(state, amount)
    return _check_or_fold(state)


# Heads-up button (=SB) open range. Excludes only the absolute worst trash.
_HU_BTN_NEVER_OPEN = {
    "72o", "73o", "62o", "63o", "52o", "53o", "42o", "43o", "32o",
    "82o", "83o", "92o", "93o",
}


def _hu_btn_open(state, hand, rng):
    """HU button open — raise wide, fold only the worst trash."""
    if hand in _HU_BTN_NEVER_OPEN:
        # Even these fold less than 100% in true GTO, but they're close.
        if rng.random() < 0.1:
            size = _bb_to_chips(state, 2.5)
            return _legal_raise(state, size)
        return _check_or_fold(state)
    size = _bb_to_chips(state, 2.5)
    return _legal_raise(state, size)


def _count_seats_in_hand(state):
    """How many seats are still active (not folded)?"""
    players = state.get("players") or []
    n = 0
    for p in players:
        if not isinstance(p, dict):
            n += 1
            continue
        if p.get("folded") or p.get("status") == "folded":
            continue
        n += 1
    return n if n > 0 else len(players)


def _defense_decision(state, hand, our_pos, history, rng):
    """Facing a raise (or raise + caller). Use defense ranges."""
    opener_pos = history["first_raiser_pos"] or "HJ"
    dist = defense_action(opener_pos, our_pos or "BB", hand)
    chosen = _sample(dist, rng)

    if chosen == "fold":
        return _check_or_fold(state)
    if chosen == "call":
        return {"action": "call"}
    if chosen == "3bet":
        # Size: ~3x opener IP, ~4x OOP (rough heuristic)
        opener_size = _last_raise_to(state)
        in_position = _will_be_in_position(opener_pos, our_pos)
        multiplier = 3.0 if in_position else 4.0
        target = int(opener_size * multiplier)
        return _legal_raise(state, target)
    return _check_or_fold(state)


def _vs_3bet(state, hand, rng):
    """We opened and someone 3-bet. Use 4-bet defense range."""
    dist = four_bet_defense(hand)
    chosen = _sample(dist, rng)

    if chosen == "fold":
        return {"action": "fold"}
    if chosen == "call":
        return {"action": "call"}
    if chosen == "3bet":
        # "3bet" in this table actually means 4-bet (we're the one
        # raising again). Size ~2.25x their 3-bet.
        three_bet_to = _last_raise_to(state)
        target = int(three_bet_to * 2.25)
        return _legal_raise(state, target)
    return {"action": "fold"}


# ---------------------------------------------------------------------------
# Short-stack mode
# ---------------------------------------------------------------------------
def _short_stack(state, hand, pos, history, facing_jam, eff_bb, rng):
    if facing_jam:
        return _call_jam_decision(state, hand, history)

    # If unopened, consider open-jamming.
    if history["raises"] == 0:
        jam_pos = pos or "BTN"
        if hand in open_jam_hands(eff_bb, jam_pos):
            return {"action": "all_in"}
        # Below 12bb we should be jamming-or-folding entirely
        if eff_bb <= 12:
            return _check_or_fold(state)
        # 12–20bb: with strong-but-not-jam hands, mini-raise
        dist = get_open_action(pos or "BTN", hand)
        if dist.get("raise", 0) > 0.5:
            size = _bb_to_chips(state, 2.2)
            return _legal_raise(state, size)
        return _check_or_fold(state)

    # Facing a raise but not a jam: tighter version of the standard defense
    return _defense_decision(state, hand, pos, history, rng)


def _call_jam_decision(state, hand, history):
    """Someone went all-in. Decide call vs fold using call ranges."""
    jammer_pos = history["last_raiser_pos"] or history["first_raiser_pos"] or "BTN"
    call_range = call_jam_range(jammer_pos)
    if hand in call_range:
        return {"action": "call"}
    return {"action": "fold"}


# ---------------------------------------------------------------------------
# History parsing
# ---------------------------------------------------------------------------
def _preflop_history(state):
    """Inspect action_log for preflop actions.

    Returns a dict:
        raises: count of raises this street
        first_raiser_pos: position name of the first raiser (or None)
        last_raiser_pos:  position name of the most recent raiser (or None)
        last_raiser_was_us: True iff the most recent raiser is us
    """
    info = {
        "raises": 0,
        "first_raiser_pos": None,
        "last_raiser_pos": None,
        "last_raiser_was_us": False,
    }
    log = state.get("action_log", [])
    players = state.get("players") or []

    for entry in log:
        if not isinstance(entry, dict):
            continue
        if entry.get("street", "preflop") != "preflop":
            continue
        action = (entry.get("action") or "").lower()
        if "raise" in action or "all_in" in action or "allin" in action or "bet" in action:
            info["raises"] += 1
            player_ref = entry.get("player") or entry.get("seat")
            pos = _resolve_position(player_ref, players)
            if info["first_raiser_pos"] is None:
                info["first_raiser_pos"] = pos
            info["last_raiser_pos"] = pos
            info["last_raiser_was_us"] = _player_is_us(player_ref, players)
    return info


def _resolve_position(player_ref, players):
    """Best-effort: turn a player ref into a position string."""
    if isinstance(player_ref, dict):
        return player_ref.get("position")
    if isinstance(player_ref, str):
        # Engine might pass "UTG" directly, or a seat name.
        if player_ref.upper() in ("UTG", "UTG1", "LJ", "HJ", "MP", "CO", "BTN", "SB", "BB"):
            return "HJ" if player_ref.upper() == "MP" else player_ref.upper()
    if isinstance(player_ref, int) and 0 <= player_ref < len(players):
        p = players[player_ref]
        if isinstance(p, dict):
            return p.get("position")
    return None


def _player_is_us(player_ref, players):
    if isinstance(player_ref, str):
        return player_ref.lower() in ("me", "hero", "self")
    if isinstance(player_ref, int) and 0 <= player_ref < len(players):
        p = players[player_ref]
        return isinstance(p, dict) and (p.get("is_me") or p.get("is_self") or p.get("hero"))
    return False


def _is_facing_jam(state):
    """True if the bet we're facing is an all-in."""
    owed = state.get("amount_owed", 0)
    stack = state.get("your_stack", 0)
    # If calling would put us all-in, treat it as a jam decision
    if stack > 0 and owed >= stack:
        return True
    # Also check log for an explicit all_in action this street
    log = state.get("action_log", [])
    for entry in log[-6:]:  # only recent
        if isinstance(entry, dict):
            action = (entry.get("action") or "").lower()
            if "all_in" in action or "allin" in action:
                return True
    return False


def _last_raise_to(state):
    """The current bet amount (which is also the most recent raise target)."""
    return state.get("current_bet", 0) or state.get("min_raise_to", 0) or 1


def _will_be_in_position(opener_pos, our_pos):
    """True if our_pos acts after opener_pos post-flop."""
    order = ["UTG", "UTG1", "LJ", "HJ", "CO", "BTN", "SB", "BB"]
    try:
        # SB and BB act first post-flop (worst position)
        if our_pos in ("SB", "BB"):
            return False
        if opener_pos in ("SB", "BB"):
            return True
        return order.index(our_pos) > order.index(opener_pos)
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Sampling / sizing primitives
# ---------------------------------------------------------------------------
def _sample(dist, rng):
    """Sample from {name: freq} distribution."""
    r = rng.random()
    cum = 0.0
    for name, freq in dist.items():
        cum += freq
        if r < cum:
            return name
    return max(dist.items(), key=lambda kv: kv[1])[0]


def _bb_to_chips(state, size_bb):
    bb_size = state.get("big_blind") or state.get("current_bet") or 100
    return int(size_bb * bb_size)


def _legal_raise(state, target):
    target = max(int(target), state.get("min_raise_to", target))
    stack = state.get("your_stack", 0)
    current_bet = state.get("current_bet", 0)
    if target >= current_bet + stack:
        return {"action": "all_in"}
    return {"action": "raise", "amount": int(target)}


def _check_or_fold(state):
    return {"action": "check"} if state.get("can_check") else {"action": "fold"}
