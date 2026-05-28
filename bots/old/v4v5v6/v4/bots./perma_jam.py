"""
perma_jam — Fullhouse test opponent.

Archetype: the meme "perma all-in" / maniac. Jams (goes all-in) the large
majority of the time, regardless of cards. Target for the 'perma_all_in' fast
override and the 'maniac' behavior read: all-in rate extremely high.

Strategy (mostly deterministic — spot-seeded RNG for reproducibility):
  - ~85% of decisions: all_in
  - otherwise: call if cheap, else check/call
  We seed a local RNG from the game state so a given spot always resolves the
  same way (reproducible matches), instead of using global random.

Legal/safe: stdlib only, no file/network, always returns a valid action.
"""

import random
import zlib

BOT_NAME = "PermaJam"
BOT_AVATAR = "robot_1"


def _spot_rng(state):
    try:
        parts = [
            str(state.get("hand_id", "")),
            str(state.get("street", "")),
            str(state.get("seat_to_act", "")),
            "".join(state.get("your_cards", []) or []),
            str(len(state.get("action_log", []) or [])),
        ]
        seed = zlib.crc32("|".join(parts).encode("utf-8")) & 0xffffffff
    except Exception:
        seed = 0
    return random.Random(seed)


def decide(game_state: dict) -> dict:
    try:
        if not isinstance(game_state, dict):
            return {"action": "fold"}
        if game_state.get("type") == "warmup":
            return {"action": "fold"}

        rng = _spot_rng(game_state)
        can_check = game_state.get("can_check", False)

        # Jam the large majority of the time.
        if rng.random() < 0.85:
            return {"action": "all_in"}

        # Non-jam fallback: take a free check, otherwise just call along.
        if can_check:
            return {"action": "check"}
        return {"action": "call"}

    except Exception:
        return {"action": "fold"}
