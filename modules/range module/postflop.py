"""
Postflop decision module.

Real-money postflop poker comes down to four questions:
    1. How strong is our hand RIGHT NOW?
    2. How many opponents are we against, and what's their range?
    3. What does the board structure favor?
    4. Are we the preflop aggressor (initiative)?

We answer (1) with Monte Carlo equity, (2) by counting active opponents and
applying a wetness-aware range assumption, (3) with the board-texture
module, and (4) by scanning the action log.

The output is a single action with a sensible sizing for the spot.
"""

from .equity import equity_vs_random, hand_strength_class
from .board import analyze as analyze_board


# Tunables — these are the dials you'll want to twist after sparring.
CBET_FREQ_HU_DRY = 0.85       # heads-up c-bet on dry boards as PFR
CBET_FREQ_HU_WET = 0.55       # heads-up c-bet on wet boards as PFR
CBET_FREQ_MULTIWAY = 0.30     # multiway c-bet (much tighter)
VALUE_BET_EQUITY_THRESHOLD = 0.60
RAISE_FOR_VALUE_EQUITY_THRESHOLD = 0.72
BLUFF_RAISE_EQUITY_THRESHOLD = 0.30  # need decent equity to bluff-raise
DRAW_CALL_MAX_POT_FRAC = 0.45        # don't call huge bets with naked draws
MIN_BLUFF_EQUITY = 0.18

# Pre-river equity adjustment vs random — opponents who called/raised
# preflop are stronger than random, so we down-shade equity.
EQ_SHADE_VS_CALLER = 0.92
EQ_SHADE_VS_RAISER = 0.85


def decide_postflop(state, rng, n_iters=1500):
    """Return an action dict for a postflop spot.

    Plays the hand to value or pot odds, with bet sizing tied to board
    wetness and hand strength.
    """
    hole = state.get("your_cards", [])
    board = state.get("community_cards", [])
    pot = max(1, state.get("pot", 1))
    owed = state.get("amount_owed", 0)
    can_check = state.get("can_check", owed == 0)
    stack = state.get("your_stack", 0)
    current_bet = state.get("current_bet", 0)

    n_opp = _count_active_opponents(state)
    if n_opp <= 0:
        return _check_or_fold(state)

    texture = analyze_board(board)
    is_pfr = _are_we_the_preflop_raiser(state)
    facing_bet = owed > 0

    # Equity is vs random; we shade based on whether we faced action.
    eq_raw = equity_vs_random(hole, board, n_opponents=n_opp, n_iters=n_iters)
    shade = _equity_shade(state, facing_bet)
    eq = eq_raw * shade

    cls = hand_strength_class(eq)

    # Pot odds when facing a bet
    pot_odds = owed / (pot + owed) if facing_bet else 0.0

    # ---------------- Facing a bet ----------------
    if facing_bet:
        return _facing_bet_decision(
            state, eq, cls, pot_odds, texture, n_opp, rng,
        )

    # ---------------- Checked to / we lead -----------
    return _no_bet_decision(
        state, eq, cls, texture, n_opp, is_pfr, rng,
    )


# ---------------------------------------------------------------------------
# Decision branches
# ---------------------------------------------------------------------------
def _facing_bet_decision(state, eq, cls, pot_odds, texture, n_opp, rng):
    """We owe chips. Raise / call / fold."""
    pot = max(1, state.get("pot", 1))
    owed = state.get("amount_owed", 0)
    facing_size_pct = owed / pot if pot > 0 else 1.0

    # Raise-for-value
    if eq >= RAISE_FOR_VALUE_EQUITY_THRESHOLD and n_opp == 1:
        target = _raise_size(state, texture, fraction=1.0)  # ~pot-sized raise
        return _legal_raise(state, target)

    # Mid-strong hand: call most bets, raise occasionally for protection
    if cls == "strong":
        # Mix raise on dry boards (deny equity), call on wet (let them barrel)
        if texture["wetness"] < 0.35 and n_opp == 1 and rng.random() < 0.35:
            target = _raise_size(state, texture, fraction=0.75)
            return _legal_raise(state, target)
        return {"action": "call"}

    # Medium / weak made: pure call/fold based on pot odds
    if cls in ("medium", "weak_made"):
        if eq > pot_odds + 0.05:
            return {"action": "call"}
        return {"action": "fold"}

    # Draw: call if pot odds + implied odds reasonable
    if cls == "draw":
        if facing_size_pct < DRAW_CALL_MAX_POT_FRAC and eq > pot_odds:
            return {"action": "call"}
        # Semi-bluff raise small portion of the time on wet boards
        if texture["wetness"] > 0.5 and rng.random() < 0.15:
            target = _raise_size(state, texture, fraction=1.0)
            return _legal_raise(state, target)
        return {"action": "fold"}

    # Air — fold to almost everything; occasionally raise as a pure bluff
    # but only with blockers and small facing size
    if cls == "air":
        if facing_size_pct < 0.5 and rng.random() < 0.04:
            target = _raise_size(state, texture, fraction=1.0)
            return _legal_raise(state, target)
        return {"action": "fold"}

    return {"action": "fold"}


def _no_bet_decision(state, eq, cls, texture, n_opp, is_pfr, rng):
    """It's checked to us. Bet or check."""
    pot = max(1, state.get("pot", 1))

    # Monster / strong: bet for value
    if cls == "monster":
        # Slow-play occasionally on dry boards to induce action
        if texture["wetness"] < 0.25 and rng.random() < 0.20:
            return {"action": "check"}
        size = _value_bet_size(state, texture, big=True)
        return _legal_raise(state, size)

    if cls == "strong":
        size = _value_bet_size(state, texture, big=False)
        return _legal_raise(state, size)

    # Medium-strength hand: thin value HU, check multiway
    if cls == "medium":
        if n_opp == 1 and eq > VALUE_BET_EQUITY_THRESHOLD:
            size = _value_bet_size(state, texture, big=False, thin=True)
            return _legal_raise(state, size)
        return {"action": "check"}

    # Weak made hand with showdown value — check
    if cls == "weak_made":
        return {"action": "check"}

    # Draw — semi-bluff some fraction of the time
    if cls == "draw":
        if n_opp == 1 and rng.random() < (0.55 if texture["wetness"] > 0.4 else 0.35):
            size = _semibluff_size(state, texture)
            return _legal_raise(state, size)
        return {"action": "check"}

    # Air — c-bet as PFR sometimes, otherwise check
    if cls == "air":
        if is_pfr:
            cbet_freq = (CBET_FREQ_MULTIWAY if n_opp >= 2 else
                         (CBET_FREQ_HU_WET if texture["wetness"] > 0.4
                          else CBET_FREQ_HU_DRY))
            # Don't c-bet pure trash; need a tiny bit of equity
            if eq >= MIN_BLUFF_EQUITY and rng.random() < cbet_freq:
                size = _cbet_size(state, texture)
                return _legal_raise(state, size)
        return {"action": "check"}

    return {"action": "check"}


# ---------------------------------------------------------------------------
# Sizing
# ---------------------------------------------------------------------------
def _value_bet_size(state, texture, big=False, thin=False):
    pot = max(1, state.get("pot", 1))
    current_bet = state.get("current_bet", 0)
    street = state.get("street", "preflop")

    if street == "river":
        frac = 0.85 if big else (0.60 if not thin else 0.45)
    else:
        # Flop/turn: bigger on wet boards to charge draws
        base = 0.75 if big else 0.55
        if texture["wetness"] > 0.5:
            base += 0.15
        if thin:
            base = min(base, 0.45)
        frac = base

    return current_bet + int(frac * pot)


def _cbet_size(state, texture):
    pot = max(1, state.get("pot", 1))
    current_bet = state.get("current_bet", 0)
    # Small c-bet on dry boards, larger on wet
    if texture["wetness"] < 0.25:
        frac = 0.33
    elif texture["wetness"] < 0.5:
        frac = 0.5
    else:
        frac = 0.66
    return current_bet + int(frac * pot)


def _semibluff_size(state, texture):
    pot = max(1, state.get("pot", 1))
    current_bet = state.get("current_bet", 0)
    frac = 0.6 if texture["wetness"] > 0.4 else 0.5
    return current_bet + int(frac * pot)


def _raise_size(state, texture, fraction=1.0):
    """Size a raise to ~fraction * pot ON TOP of the current bet."""
    pot = max(1, state.get("pot", 1))
    current_bet = state.get("current_bet", 0)
    return current_bet + int(fraction * pot)


# ---------------------------------------------------------------------------
# State inspection
# ---------------------------------------------------------------------------
def _count_active_opponents(state):
    players = state.get("players") or []
    n = 0
    for p in players:
        if not isinstance(p, dict):
            continue
        if p.get("is_me") or p.get("is_self") or p.get("hero"):
            continue
        if p.get("folded") or p.get("status") == "folded":
            continue
        n += 1
    # Fallback if no metadata: assume 1 opponent.
    return n if n > 0 else 1


def _are_we_the_preflop_raiser(state):
    """Scan preflop action log for last raise, see if it was us."""
    log = state.get("action_log", [])
    last_raiser = None
    for entry in log:
        if not isinstance(entry, dict):
            continue
        street = entry.get("street", "preflop")
        if street != "preflop":
            continue
        action = (entry.get("action") or "").lower()
        if "raise" in action or "bet" in action or "all_in" in action or "allin" in action:
            last_raiser = entry.get("player") or entry.get("seat")
    if last_raiser is None:
        return False
    # Engine-dependent: "me", "hero", or our seat index.
    if isinstance(last_raiser, str):
        return last_raiser.lower() in ("me", "hero", "self")
    if isinstance(last_raiser, int):
        players = state.get("players") or []
        if 0 <= last_raiser < len(players):
            p = players[last_raiser]
            return isinstance(p, dict) and (p.get("is_me") or p.get("is_self") or p.get("hero"))
    return False


def _equity_shade(state, facing_bet):
    """Down-shade equity-vs-random when there's been preflop action.

    Players who paid to see the flop are tighter than random — our
    hand-vs-random number overstates our equity.
    """
    log = state.get("action_log", [])
    raises = 0
    for entry in log:
        if not isinstance(entry, dict):
            continue
        if entry.get("street", "preflop") != "preflop":
            continue
        action = (entry.get("action") or "").lower()
        if "raise" in action or "bet" in action:
            raises += 1
    if raises >= 2:
        return EQ_SHADE_VS_RAISER  # 3-bet pot or similar
    if raises >= 1:
        return EQ_SHADE_VS_CALLER
    return 1.0


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------
def _check_or_fold(state):
    if state.get("can_check"):
        return {"action": "check"}
    return {"action": "fold"}


def _legal_raise(state, target):
    """Snap target to legal bounds. Falls back to all_in if we'd commit."""
    target = max(target, state.get("min_raise_to", target))
    stack = state.get("your_stack", 0)
    current_bet = state.get("current_bet", 0)
    if target >= current_bet + stack:
        return {"action": "all_in"}
    return {"action": "raise", "amount": int(target)}
