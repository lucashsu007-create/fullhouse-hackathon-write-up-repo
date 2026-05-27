"""Head-to-head sanity check.

Run 200 hands of: useful_bot.decide  vs.  a simple baseline bot.
We're not running the real engine here (don't have it), just a coarse
preflop+flop simulation that returns aggregate equity-weighted outcomes.

Goal: verify the useful bot makes obviously-better decisions than a
naive "call everything cheap, raise pairs" baseline. If it loses to that,
something is broken.
"""

import random
import sys
import os
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from gto import decide as useful_decide
from gto import canonical, equity_vs_random
from gto.hand_notation import all_169_hands


def baseline_decide(state):
    """Dumb baseline: raise pairs, call broadways, fold rest. Some bots
    in the reference set play roughly this strategy."""
    hand = canonical(state["your_cards"])
    can_check = state.get("can_check", False)
    owed = state.get("amount_owed", 0)
    stack = state.get("your_stack", 0)
    pot = state.get("pot", 1)
    big_blind = state.get("big_blind", 1)
    street = state.get("street", "preflop")

    # Crude hand class
    is_pair = len(hand) == 2
    is_broadway = len(hand) == 3 and hand[0] in "AKQJT" and hand[1] in "AKQJT"
    is_suited = len(hand) == 3 and hand[2] == "s"

    if street == "preflop":
        if is_pair or (hand in ("AKs", "AKo", "AQs", "AQo", "AJs")):
            if owed > 0 and owed <= 5 * big_blind:
                # raise 3x
                return {"action": "raise", "amount": int(3 * big_blind)}
            if can_check:
                return {"action": "check"}
            if owed > 0:
                return {"action": "call"}
            return {"action": "raise", "amount": int(3 * big_blind)}
        if is_broadway or is_suited:
            if can_check:
                return {"action": "check"}
            if owed <= 3 * big_blind:
                return {"action": "call"}
            return {"action": "fold"}
        if can_check:
            return {"action": "check"}
        return {"action": "fold"}

    # postflop: call up to 1/3 pot, otherwise fold or check
    if can_check:
        return {"action": "check"}
    if owed < pot / 3:
        return {"action": "call"}
    return {"action": "fold"}


def play_hand(bot_a, bot_b, big_blind=1, starting_stack=100, rng=None):
    """Play a single heads-up hand. Very simplified — preflop + 1 round of
    postflop, then go to showdown. No multistreet betting realism, but
    enough to test if decision quality is better than baseline."""
    rng = rng or random
    # Deal
    full_deck = [r + s for r in "23456789TJQKA" for s in "shdc"]
    rng.shuffle(full_deck)
    hand_a = full_deck[:2]
    hand_b = full_deck[2:4]
    board = full_deck[4:9]

    sb = big_blind / 2
    pot = sb + big_blind
    stack_a = starting_stack - sb
    stack_b = starting_stack - big_blind

    # Bot A is SB (BTN heads-up), Bot B is BB
    # Preflop: A acts first
    bet_a, bet_b = sb, big_blind

    def make_state(my_hand, my_stack, opp_stack, my_bet, opp_bet,
                   pot, board, street, can_check, action_log, is_btn):
        return {
            "your_cards": my_hand,
            "community_cards": board,
            "street": street,
            "pot": pot,
            "your_stack": my_stack,
            "amount_owed": max(0, opp_bet - my_bet),
            "can_check": can_check,
            "current_bet": opp_bet,
            "min_raise_to": max(opp_bet * 2, big_blind * 2),
            "players": [
                {"is_me": True, "is_button": is_btn,
                 "stack": my_stack, "chips": my_stack,
                 "position": "SB" if is_btn else "BB"},
                {"is_button": not is_btn,
                 "stack": opp_stack, "chips": opp_stack,
                 "position": "BB" if is_btn else "SB"},
            ],
            "action_log": action_log,
            "big_blind": big_blind,
        }

    log = []
    # SB (bot_a) acts first preflop
    state_a = make_state(hand_a, stack_a, stack_b, bet_a, bet_b,
                         pot, [], "preflop", False, log, is_btn=True)
    act_a = bot_a(state_a)

    if act_a["action"] == "fold":
        return -bet_a  # A folds, loses SB
    if act_a["action"] == "all_in":
        amt = stack_a + bet_a
        pot += stack_a
        bet_a = amt
        stack_a = 0
    elif act_a["action"] == "raise":
        amt = act_a["amount"]
        delta = amt - bet_a
        delta = min(delta, stack_a)
        stack_a -= delta
        pot += delta
        bet_a += delta
    elif act_a["action"] == "call":
        delta = bet_b - bet_a
        delta = min(delta, stack_a)
        stack_a -= delta
        pot += delta
        bet_a += delta

    # BB acts
    state_b = make_state(hand_b, stack_b, stack_a, bet_b, bet_a,
                         pot, [], "preflop", bet_a == bet_b, log, is_btn=False)
    act_b = bot_b(state_b)

    if act_b["action"] == "fold":
        return bet_b  # A wins what B already paid in
    if act_b["action"] == "all_in":
        amt = stack_b + bet_b
        delta = amt - bet_b
        stack_b -= delta
        pot += delta
        bet_b = amt
    elif act_b["action"] == "raise":
        delta = min(act_b["amount"] - bet_b, stack_b)
        stack_b -= delta
        pot += delta
        bet_b += delta
        # A acts again
        state_a = make_state(hand_a, stack_a, stack_b, bet_a, bet_b,
                             pot, [], "preflop", False, log, is_btn=True)
        act_a = bot_a(state_a)
        if act_a["action"] == "fold":
            return -bet_a
        if act_a["action"] in ("call", "all_in"):
            delta = min(bet_b - bet_a, stack_a)
            stack_a -= delta
            pot += delta
            bet_a += delta
    elif act_b["action"] == "call":
        delta = bet_a - bet_b
        delta = min(delta, stack_b)
        stack_b -= delta
        pot += delta
        bet_b += delta

    # Skip to showdown — use treys/eval7 to settle
    # (we're verifying decisions not betting tree, so this is fine)
    from gto.equity import equity_vs_random
    # Compare hand_a vs hand_b on full board
    eq = equity_vs_random(hand_a, board, n_opponents=1, n_iters=1)  # not meaningful for 1 iter
    # Better: directly compare hands
    try:
        from treys import Card, Evaluator
        ev = Evaluator()
        ra = ev.evaluate([Card.new(c) for c in board], [Card.new(c) for c in hand_a])
        rb = ev.evaluate([Card.new(c) for c in board], [Card.new(c) for c in hand_b])
        if ra < rb:  # lower = better in treys
            return pot - bet_a
        if ra > rb:
            return -bet_a
        return (pot / 2) - bet_a
    except Exception:
        return 0


def main():
    rng = random.Random(7)
    n_hands = 200

    # useful_bot as A, baseline as B
    profit = 0
    t0 = time.time()
    for h in range(n_hands):
        # Alternate who's button to remove positional bias
        if h % 2 == 0:
            profit += play_hand(useful_decide, baseline_decide, rng=rng)
        else:
            profit -= play_hand(baseline_decide, useful_decide, rng=rng)
    elapsed = time.time() - t0
    bb_per_100 = (profit / n_hands) * 100
    print(f"useful_bot vs baseline: {profit:+.1f} chips over {n_hands} hands "
          f"({bb_per_100:+.1f} BB/100, {elapsed:.1f}s total, "
          f"{elapsed*1000/n_hands:.0f}ms/hand)")
    return profit


if __name__ == "__main__":
    p = main()
    print(f"PASS" if p > 0 else "FAIL — bot is losing to baseline!")
