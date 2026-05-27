"""
Swiss-tournament pairing + standings + finalist selection.

Pure-Python helpers used by the demo server and any local tournament harness.
The production match queue (BullMQ workers) ships its own copy in the private
ops repo; this module is the canonical reference for participants who want to
run their own multi-bot bake-off locally.
"""


def swiss_pairing(standings, table_size=6):
    """
    Pair bots into tables. `standings` is a list of dicts with bot_id, bot_path,
    cumulative_delta. Returns a list of tables (each table is a list of bot dicts).

    Stragglers (fewer bots left than `table_size`) are folded into the last full
    table rather than playing a short-handed match.
    """
    sorted_bots = sorted(standings, key=lambda b: -b.get("cumulative_delta", 0))

    tables = []
    i = 0
    while i < len(sorted_bots):
        remaining = len(sorted_bots) - i
        if remaining < table_size and tables:
            tables[-1].extend(sorted_bots[i:])
            break
        tables.append(sorted_bots[i:i + table_size])
        i += table_size

    return tables


def compute_standings(all_results):
    """
    `all_results`: list of {bot_id, bot_path, chip_delta}.
    Returns a sorted list of {bot_id, bot_path, cumulative_delta,
    matches_played, best_match_delta}, ranked by:

      1. cumulative_delta (DESC)             — primary: total chips won
      2. matches_played   (ASC)              — fewer matches with same chips = stronger
      3. best_match_delta (DESC)             — best single-match performance
      4. bot_id           (ASC)              — deterministic alphabetic last resort

    The matches_played tiebreaker rewards bots that scored a high
    cumulative chip delta over fewer matches (less variance exposure)
    over bots that ground it out across more rounds. Standard practice
    in chess-style Swiss tournaments adapted to chip scoring.
    """
    totals = {}
    for r in all_results:
        bid = r["bot_id"]
        delta = r["chip_delta"]
        if bid not in totals:
            totals[bid] = {
                "bot_id":           bid,
                "bot_path":         r.get("bot_path", ""),
                "cumulative_delta": 0,
                "matches_played":   0,
                "best_match_delta": delta,
            }
        totals[bid]["cumulative_delta"] += delta
        totals[bid]["matches_played"]   += 1
        if delta > totals[bid]["best_match_delta"]:
            totals[bid]["best_match_delta"] = delta

    return sorted(
        totals.values(),
        key=lambda b: (
            -b["cumulative_delta"],
             b["matches_played"],
            -b["best_match_delta"],
             b["bot_id"],
        ),
    )


def select_finalists(standings, n=64):
    """Top-n bots by cumulative chip delta with documented tiebreakers
    (see compute_standings). Default n=64 matches the qualifier cut."""
    return standings[:n]
