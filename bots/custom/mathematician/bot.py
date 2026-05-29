"""Bot B: The Mathematician — calls only when pot odds justify it."""

BOT_NAME = "The Mathematician"

def decide(state):
    owed  = state["amount_owed"]
    pot   = state["pot"]
    stack = state["your_stack"]

    if state["can_check"]:
        return {"action": "check"}

    if owed == 0:
        return {"action": "check"}

    # Call if getting 3:1 or better
    pot_odds = pot / owed if owed > 0 else 999
    if pot_odds >= 3.0:
        return {"action": "call"}

    return {"action": "fold"}
