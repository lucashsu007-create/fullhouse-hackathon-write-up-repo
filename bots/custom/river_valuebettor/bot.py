"""
river_valuebettor — Fullhouse test opponent (UNDERBLUFFED-RIVER archetype).

Purpose: create the exact spot `river_underbluff_fold` targets, which the
existing panel never produces — a LOW-aggression villain that fires a BIG
river bet / jam only with a strong made hand (its river aggression is
value-heavy / underbluffed), and otherwise checks, calls, or folds.

Profile by street:
  - preflop : tight-passive. Raise only premiums; call a modest range; fold trash.
  - flop/turn: low aggression — mostly check/call with anything decent, fold air
               to bets. Almost never raises (keeps raise_rate low so the hero's
               `_rr <= 0.18` gate recognises it as an underbluffer).
  - river   : the defining behaviour. With a STRONG made hand (two pair+ or an
              overpair-ish top pair on a safe board) it bets BIG (>= ~0.7 pot) or
              jams. With anything weaker it checks (and folds to a bet). It does
              NOT bluff the river. So every big river bet it makes is value.

Against this opponent a hero that credits river-betting ranges with bluffs will
overcall and bleed; `river_underbluff_fold` (fold more to big river bets/jams
from low-aggression villains) should save chips. A board-blind/MDF hero should
lose to it on the river; the underbluff-fold exploit should beat it there.

Determinism: spot-seeded RNG. Safe: stdlib + optional eval7, always legal action.
"""
import random

try:
    import eval7
except Exception:                       # pragma: no cover
    eval7 = None

BOT_NAME = "RiverValueBettor"
BOT_AVATAR = "robot_6"

_VAL = {"2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8, "9": 9,
        "T": 10, "J": 11, "Q": 12, "K": 13, "A": 14}


def _seed(state):
    key = "|".join((
        str(state.get("hand_id", "")),
        str(state.get("street", "")),
        str(state.get("seat_to_act", "")),
        "".join(state.get("your_cards", []) or []),
        "".join(state.get("community_cards", []) or []),
    ))
    return random.Random(key)


def _made_tier(hole, board):
    """0 = air/weak, 1 = decent (pair), 2 = strong (two pair+ / set / better)."""
    if eval7 and len(hole) == 2 and len(board) >= 3:
        try:
            h = [eval7.Card(c) for c in hole]
            b = [eval7.Card(c) for c in board]
            ht = eval7.handtype(eval7.evaluate(h + b))
            if ht in ("Straight Flush", "Quads", "Full House", "Flush",
                      "Straight", "Trips", "Two Pair"):
                return 2
            if ht == "Pair":
                # promote a strong top pair (paired with a high hole card) toward value
                hole_ranks = sorted((_VAL.get(c[0], 0) for c in hole), reverse=True)
                board_ranks = [c[0] for c in board]
                paired_hi = any(c[0] in board_ranks and _VAL.get(c[0], 0) >= 12
                                for c in hole)
                return 2 if (paired_hi and hole_ranks[0] >= 13) else 1
            return 0
        except Exception:
            pass
    # fallback: pocket pair or paired board => decent
    hr = [c[0] for c in (hole or [])]
    br = [c[0] for c in (board or [])]
    if len(hr) == 2 and hr[0] == hr[1]:
        return 1
    if any(r in br for r in hr):
        return 1
    return 0


def _raise_to(state, frac):
    pot = max(1, int(state.get("pot", 0)))
    cur = int(state.get("current_bet", 0))
    min_to = int(state.get("min_raise_to", cur))
    stack = int(state.get("your_stack", 0))
    mine = int(state.get("your_bet_this_street", 0))
    target = max(cur + max(1, int(frac * pot)), min_to)
    cap = mine + stack
    return cap if target >= cap else target


def decide(game_state: dict) -> dict:
    try:
        if not isinstance(game_state, dict) or game_state.get("type") == "warmup":
            return {"action": "fold"}
        rng = _seed(game_state)
        hole = game_state.get("your_cards", []) or []
        board = game_state.get("community_cards", []) or []
        owed = int(game_state.get("amount_owed", 0))
        stack = int(game_state.get("your_stack", 0))
        can_check = game_state.get("can_check", False)
        street = game_state.get("street", "preflop")
        n_board = len(board)
        preflop = (n_board < 3) or (street == "preflop")

        # ---------------- PREFLOP: tight-passive ----------------
        if preflop:
            vals = sorted((_VAL.get(c[0], 0) for c in hole), reverse=True)
            if len(vals) < 2:
                return {"action": "check"} if owed <= 0 else {"action": "call"}
            hi, lo = vals[0], vals[1]
            pair = (hi == lo)
            suited = len(hole) == 2 and hole[0][1] == hole[1][1]
            premium = (pair and hi >= 12) or (hi == 14 and lo >= 13)  # QQ+/AK
            playable = pair or (hi >= 12 and lo >= 10) or (suited and hi >= 12)
            if owed <= 0 and can_check:
                if premium:
                    return {"action": "raise", "amount": _raise_to(game_state, 0.9)}
                return {"action": "check"}
            if owed > 0:
                if premium:
                    return {"action": "raise", "amount": _raise_to(game_state, 0.9)}
                if playable and owed < stack * 0.4:
                    return {"action": "call"}
                return {"action": "fold"}
            return {"action": "check"}

        tier = _made_tier(hole, board)
        river = (n_board == 5) or (street == "river")

        # ---------------- RIVER: underbluffed value-betting ----------------
        if river:
            if can_check or owed <= 0:
                # First to act / checked to: bet BIG only with a strong hand.
                if tier >= 2:
                    # value bet large; sometimes jam
                    if rng.random() < 0.35:
                        return {"action": "all_in", "amount": stack} if stack > 0 \
                            else {"action": "raise", "amount": _raise_to(game_state, 0.9)}
                    return {"action": "raise", "amount": _raise_to(game_state, 0.8)}
                # weak/medium: never bluff the river -> check
                return {"action": "check"}
            # Facing a river bet: call with strong, fold the rest (no bluff-raises).
            if tier >= 2:
                return {"action": "call"}
            if tier == 1 and owed <= 0.4 * max(1, game_state.get("pot", 0)):
                return {"action": "call"}
            return {"action": "fold"}

        # ---------------- FLOP / TURN: low aggression ----------------
        if can_check or owed <= 0:
            # rarely lead; mostly check (keeps raise_rate low)
            if tier >= 2 and rng.random() < 0.30:
                return {"action": "raise", "amount": _raise_to(game_state, 0.5)}
            return {"action": "check"}
        # facing a bet: call with anything decent, fold air, almost never raise
        if tier >= 1:
            if owed <= 0.7 * max(1, game_state.get("pot", 0)):
                return {"action": "call"}
            return {"action": "call"} if tier >= 2 else {"action": "fold"}
        return {"action": "fold"}

    except Exception:
        return {"action": "fold"}
