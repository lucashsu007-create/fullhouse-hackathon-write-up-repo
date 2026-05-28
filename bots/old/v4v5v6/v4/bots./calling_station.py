"""
calling_station — Fullhouse test opponent.

Archetype: the classic loose-passive "station". Calls almost anything, almost
never folds, rarely raises. Exists so the classifier/exploit layers have a clean
'station' / 'calling_bot' target. Should be read as: call_vs_bet very high,
fold_vs_bet very low, calls across all price buckets.

Strategy (deterministic — no RNG):
  - free check available -> check
  - facing a bet -> call unless it's an enormous overbet relative to stack
  - never bluffs, almost never raises (only jams the pure nuts-ish very rarely)

Legal/safe: stdlib only, no file/network, always returns a valid action.
"""

BOT_NAME = "CallingStation"
BOT_AVATAR = "robot_1"


def decide(game_state: dict) -> dict:
    try:
        if not isinstance(game_state, dict):
            return {"action": "fold"}
        if game_state.get("type") == "warmup":
            return {"action": "fold"}

        can_check = game_state.get("can_check", False)
        owed = game_state.get("amount_owed", 0)
        stack = game_state.get("your_stack", 0)

        # Free to continue -> always take the free card.
        if can_check or owed <= 0:
            return {"action": "check"}

        # Facing a bet: a station calls almost everything. Only fold to a bet
        # so large it would commit essentially the whole stack on a clearly
        # hopeless price — and even then, call most of the time. We keep this
        # close to "never fold" so the fold_vs_bet rate stays very low.
        if owed >= stack:
            # All-in to call. Stations still call wide here, but not literally
            # always — fold only the very worst, cheapest-information spots.
            return {"action": "call"}

        # Anything short of all-in: call.
        return {"action": "call"}

    except Exception:
        return {"action": "fold"}
