"""Reference bot 2 — pot-odds caller. For testing only."""

def decide(state: dict) -> dict:
    owed   = state["amount_owed"]
    pot    = state["pot"]
    stack  = state["your_stack"]

    if state["can_check"]:
        return {"action": "check"}

    # Call if getting better than 3:1 pot odds
    if owed == 0 or pot / owed >= 3:
        return {"action": "call"}

    return {"action": "fold"}
