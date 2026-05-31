"""perma_raiser — degenerate test bot: raises/bets every decision (random range, not all-in)."""
BOT_NAME = "PermaRaiser"; BOT_AVATAR = "robot_2"
def decide(game_state: dict) -> dict:
    try:
        if not isinstance(game_state, dict) or game_state.get("type")=="warmup":
            return {"action":"fold"}
        cur=int(game_state.get("current_bet",0)); pot=max(1,int(game_state.get("pot",0)))
        minto=int(game_state.get("min_raise_to",cur)); stack=int(game_state.get("your_stack",0))
        mine=int(game_state.get("your_bet_this_street",0))
        target=max(minto, cur+max(1,int(0.75*pot)))
        cap=mine+stack
        if target>=cap:  # can't make a legal raise -> just get it in / call
            return {"action":"all_in","amount":stack} if stack>0 else {"action":"check"}
        return {"action":"raise","amount":target}
    except Exception:
        return {"action":"call"}
