"""
balanced_tag — Fullhouse test opponent.

Archetype: a realistic tight-aggressive regular. Tight preflop, but UNLIKE the
bare-bones simple_tag it continues postflop at reasonable frequencies — with
pairs, draws, overcards, and some bluff-catchers — rather than folding
everything that isn't a strong made hand. The point of this bot is to be a TAG
that does NOT overfold to cheap/medium bets, so a classifier that still reads it
as a high-confidence folding_bot would be making a genuine error (not just
reading real overfolding, as it would with simple_tag).

Strategy (deterministic — spot-seeded RNG for reproducible mixed frequencies):
  preflop:
    strong (TT+, AK, AQ)   -> raise
    medium (pairs, broadway, suited A, suited connectors) -> raise/call cheap
    junk                   -> check if free, else fold
  postflop (continues at reasonable, price-aware frequencies):
    strong made hand (two pair+ / overpair / top pair good kicker) -> bet/raise
    medium made (any pair, incl. middle/bottom)                    -> call decent
                                                                       prices,
                                                                       bet small
                                                                       sometimes
    draw (flush draw / open-ender)                                 -> call good
                                                                       prices,
                                                                       semibluff
                                                                       sometimes
    overcards / bluff-catcher                                      -> call cheap
                                                                       bets some
                                                                       of the
                                                                       time
    air                                                            -> mostly
                                                                       give up

Legal/safe: stdlib only, no file/network, always returns a valid action.
"""

import random
import zlib

BOT_NAME = "BalancedTAG"
BOT_AVATAR = "robot_1"

_RANKS = "23456789TJQKA"
_VAL = {r: i for i, r in enumerate(_RANKS, start=2)}


def _spot_rng(state):
    try:
        parts = [
            str(state.get("hand_id", "")),
            str(state.get("street", "")),
            str(state.get("seat_to_act", "")),
            "".join(state.get("your_cards", []) or []),
            "".join(state.get("community_cards", []) or []),
            str(len(state.get("action_log", []) or [])),
        ]
        seed = zlib.crc32("|".join(parts).encode("utf-8")) & 0xffffffff
    except Exception:
        seed = 0
    return random.Random(seed)


def _preflop_tier(cards):
    try:
        r1, s1 = cards[0][0], cards[0][1]
        r2, s2 = cards[1][0], cards[1][1]
    except (IndexError, TypeError):
        return "junk"
    v1, v2 = _VAL.get(r1, 0), _VAL.get(r2, 0)
    hi, lo = max(v1, v2), min(v1, v2)
    pair = r1 == r2
    suited = s1 == s2
    gap = hi - lo
    if pair and hi >= 10:
        return "strong"
    if hi == 14 and lo >= 12:
        return "strong"
    if pair:
        return "medium"
    if hi >= 12 and lo >= 9:
        return "medium"
    if hi == 14 and suited:
        return "medium"
    if suited and gap == 1 and lo >= 4:        # suited connectors
        return "medium"
    return "junk"


def _postflop_read(hole, board):
    """Return (made, draw) where made in {strong, medium, overcards, air} and
    draw is a bool (flush draw or open-ended straight draw present)."""
    try:
        cards = hole + board
        ranks = [c[0] for c in cards]
        suits = [c[1] for c in cards]
        hole_vals = sorted((_VAL.get(c[0], 0) for c in hole), reverse=True)
        board_vals = [_VAL.get(c[0], 0) for c in board]
    except (TypeError, IndexError):
        return "air", False

    counts = {}
    for r in ranks:
        counts[r] = counts.get(r, 0) + 1
    cvals = sorted(counts.values(), reverse=True)

    # draw detection
    suit_counts = {}
    for s in suits:
        suit_counts[s] = suit_counts.get(s, 0) + 1
    flush_draw = max(suit_counts.values(), default=0) == 4
    uniq = sorted(set(_VAL.get(r, 0) for r in ranks))
    if 14 in uniq:
        uniq = sorted(set(uniq + [1]))
    oesd = _has_run(uniq, 4) and not _has_run(uniq, 5)
    draw = flush_draw or oesd

    # made-hand strength
    if (cvals and cvals[0] >= 3) or cvals.count(2) >= 2 or _has_run(uniq, 5) \
            or max(suit_counts.values(), default=0) >= 5:
        return "strong", draw
    if cvals and cvals[0] == 2:
        paired = next((_VAL.get(r, 0) for r, c in counts.items() if c == 2), 0)
        top_board = max(board_vals) if board_vals else 0
        if hole_vals and hole_vals[0] == hole_vals[1] and hole_vals[0] > top_board:
            return "strong", draw                     # overpair
        if paired >= top_board and hole_vals and hole_vals[0] >= 12:
            return "strong", draw                     # top pair good kicker
        return "medium", draw                          # any other pair
    # no pair: are we holding overcards to the board?
    top_board = max(board_vals) if board_vals else 0
    if hole_vals and hole_vals[0] > top_board:
        return "overcards", draw
    return "air", draw


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


def _price(game_state):
    """required equity proxy = owed / (pot + owed)."""
    owed = game_state.get("amount_owed", 0)
    pot = game_state.get("pot", 0)
    if owed <= 0:
        return 0.0
    return owed / (pot + owed)


def decide(game_state: dict) -> dict:
    try:
        if not isinstance(game_state, dict):
            return {"action": "fold"}
        if game_state.get("type") == "warmup":
            return {"action": "fold"}

        street = game_state.get("street", "preflop")
        can_check = game_state.get("can_check", False)
        hole = game_state.get("your_cards", []) or []
        board = game_state.get("community_cards", []) or []
        stack = game_state.get("your_stack", 0)
        my_bet = game_state.get("your_bet_this_street", 0)
        min_to = game_state.get("min_raise_to", 0)
        pot = game_state.get("pot", 0)
        rng = _spot_rng(game_state)

        if street == "preflop":
            tier = _preflop_tier(hole)
            if tier == "strong":
                target = min(min_to * 3, stack + my_bet)
                return {"action": "raise", "amount": max(target, min_to)}
            if tier == "medium":
                if can_check:
                    # sometimes raise medium for balance, else see a flop
                    if rng.random() < 0.35:
                        target = min(min_to * 25 // 10, stack + my_bet)
                        return {"action": "raise", "amount": max(target, min_to)}
                    return {"action": "check"}
                return {"action": "call"} if _price(game_state) <= 0.30 else {"action": "fold"}
            return {"action": "check"} if can_check else {"action": "fold"}

        # Postflop — continue at reasonable, price-aware frequencies.
        made, draw = _postflop_read(hole, board)
        price = _price(game_state)

        if made == "strong":
            if can_check:
                bet = min(int(pot * 0.66) + min_to, stack + my_bet)
                return {"action": "raise", "amount": max(bet, min_to)}
            # value-raise sometimes, call otherwise
            if rng.random() < 0.4:
                rz = min(int(pot * 0.9) + min_to, stack + my_bet)
                return {"action": "raise", "amount": max(rz, min_to)}
            return {"action": "call"}

        if made == "medium":
            if can_check:
                if rng.random() < 0.4:
                    bet = min(int(pot * 0.5) + min_to, stack + my_bet)
                    return {"action": "raise", "amount": max(bet, min_to)}
                return {"action": "check"}
            # call reasonable prices with a made pair (don't overfold cheap bets)
            return {"action": "call"} if price <= 0.40 else {"action": "fold"}

        if draw:
            if can_check:
                if rng.random() < 0.45:      # semibluff
                    bet = min(int(pot * 0.55) + min_to, stack + my_bet)
                    return {"action": "raise", "amount": max(bet, min_to)}
                return {"action": "check"}
            # draws call good prices
            return {"action": "call"} if price <= 0.34 else {"action": "fold"}

        if made == "overcards":
            if can_check:
                return {"action": "check"}
            # bluff-catch / float cheap bets some of the time
            if price <= 0.22 and rng.random() < 0.5:
                return {"action": "call"}
            return {"action": "fold"}

        # air
        if can_check:
            # occasional small stab in position-agnostic fashion
            if rng.random() < 0.15:
                bet = min(int(pot * 0.5) + min_to, stack + my_bet)
                return {"action": "raise", "amount": max(bet, min_to)}
            return {"action": "check"}
        return {"action": "fold"}

    except Exception:
        return {"action": "fold"}
