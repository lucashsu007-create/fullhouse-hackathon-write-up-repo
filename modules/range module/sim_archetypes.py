"""Run the useful bot against multiple opponent archetypes."""

import random
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from tests.sim_vs_baseline import play_hand, baseline_decide
from gto import decide as useful_decide
from gto import canonical


def aggressor_decide(state):
    """Raise constantly (like the 'aggressor' reference bot)."""
    can_check = state.get("can_check", False)
    owed = state.get("amount_owed", 0)
    stack = state.get("your_stack", 0)
    pot = state.get("pot", 1)
    big_blind = state.get("big_blind", 1)
    current_bet = state.get("current_bet", 0)

    if owed >= stack:
        # Facing all-in — only call with very strong hands
        hand = canonical(state["your_cards"])
        if hand in ("AA", "KK", "QQ", "AKs", "AKo"):
            return {"action": "call"}
        return {"action": "fold"}

    # Always raise pot
    target = current_bet + max(int(pot), big_blind * 2)
    if target >= current_bet + stack:
        return {"action": "all_in"}
    return {"action": "raise", "amount": target}


def tight_decide(state):
    """Plays only premium hands (like a nit)."""
    hand = canonical(state["your_cards"])
    can_check = state.get("can_check", False)
    owed = state.get("amount_owed", 0)
    pot = state.get("pot", 1)
    big_blind = state.get("big_blind", 1)
    street = state.get("street", "preflop")

    premium = {"AA", "KK", "QQ", "JJ", "TT", "AKs", "AKo", "AQs", "AQo"}

    if street == "preflop":
        if hand in premium:
            if owed > 0:
                return {"action": "raise", "amount": int(3 * big_blind)}
            return {"action": "raise", "amount": int(3 * big_blind)}
        if can_check:
            return {"action": "check"}
        return {"action": "fold"}

    # postflop: only call/bet with strong made hand assumption
    if hand in {"AA", "KK", "QQ"}:
        if can_check:
            return {"action": "raise", "amount": int(0.7 * pot) + state.get("current_bet", 0)}
        return {"action": "call"}
    if can_check:
        return {"action": "check"}
    return {"action": "fold"}


def mathematician_decide(state):
    """Calling station — calls at any pot odds better than 3:1."""
    can_check = state.get("can_check", False)
    owed = state.get("amount_owed", 0)
    pot = state.get("pot", 1)

    if can_check:
        return {"action": "check"}
    if owed > 0 and pot / owed >= 3:
        return {"action": "call"}
    return {"action": "fold"}


def run_match(opponent_name, opponent_fn, n_hands=200, seed=42):
    rng = random.Random(seed)
    profit = 0
    for h in range(n_hands):
        if h % 2 == 0:
            profit += play_hand(useful_decide, opponent_fn, rng=rng)
        else:
            profit -= play_hand(opponent_fn, useful_decide, rng=rng)
    bb_per_100 = (profit / n_hands) * 100
    verdict = "WIN" if profit > 0 else "LOSE"
    print(f"vs {opponent_name:14s}: {profit:+7.1f} chips ({bb_per_100:+6.1f} BB/100) [{verdict}]")
    return profit


print(f"{'='*60}")
print(f"useful_bot vs reference archetypes  ({200} hands each)")
print(f"{'='*60}")
total = 0
for name, fn in [
    ("baseline",     baseline_decide),
    ("aggressor",    aggressor_decide),
    ("tight",        tight_decide),
    ("mathematician", mathematician_decide),
]:
    total += run_match(name, fn)
print(f"{'-'*60}")
print(f"{'TOTAL':14s}: {total:+7.1f} chips")
