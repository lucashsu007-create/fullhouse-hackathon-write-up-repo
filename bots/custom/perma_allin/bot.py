"""perma_allin — degenerate test bot: shoves all-in every decision (random range)."""
BOT_NAME = "PermaAllin"; BOT_AVATAR = "robot_1"
def decide(game_state: dict) -> dict:
    try:
        if not isinstance(game_state, dict) or game_state.get("type")=="warmup":
            return {"action":"fold"}
        stack=int(game_state.get("your_stack",0)); owed=int(game_state.get("amount_owed",0))
        if stack<=0: return {"action":"call"} if owed>0 else {"action":"check"}
        return {"action":"all_in","amount":stack}
    except Exception:
        return {"action":"call"}
