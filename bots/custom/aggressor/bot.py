"""Bot A: The Aggressor — raises constantly, bets big."""
import random

BOT_NAME = "The Aggressor"

def decide(state):
    stack = state["your_stack"]
    pot   = state["pot"]
    min_r = state["min_raise_to"]

    if random.random() < 0.7:
        raise_to = min(min_r * random.randint(2, 4), stack + state["your_bet_this_street"])
        raise_to = max(raise_to, min_r)
        return {"action": "raise", "amount": raise_to}

    if state["can_check"]:
        return {"action": "check"}

    return {"action": "call"}
