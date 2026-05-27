"""End-to-end tests for the useful bot."""

import random
import sys
import os
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from gto import (
    decide, equity_vs_random, hand_strength_class, backend_name,
    analyze_board, defense_action, four_bet_defense,
    open_jam_hands, call_jam_range,
)


# ---------------------------------------------------------------------------
# Equity calculator
# ---------------------------------------------------------------------------
def test_equity_backend():
    name = backend_name()
    print(f"OK equity_backend → {name}")
    assert name in ("eval7", "treys", "none")


def test_equity_basic():
    # AA vs random preflop ~ 85%
    eq = equity_vs_random(["As", "Ah"], [], n_opponents=1, n_iters=2000)
    assert 0.80 < eq < 0.90, f"AA preflop equity: {eq}"
    # 72o vs random preflop ~ 35%
    eq = equity_vs_random(["7s", "2h"], [], n_opponents=1, n_iters=2000)
    assert 0.30 < eq < 0.42, f"72o preflop equity: {eq}"
    # AA vs random with a board where we have set
    eq = equity_vs_random(["As", "Ah"], ["Ad", "7c", "2s"], n_iters=2000)
    assert eq > 0.95, f"AA with set: {eq}"
    print("OK equity_basic")


def test_equity_speed():
    """Equity calc must fit comfortably in 2s budget."""
    t0 = time.time()
    for _ in range(5):
        equity_vs_random(["As", "Kh"], ["7d", "Tc", "2s"], n_opponents=2, n_iters=1500)
    elapsed = (time.time() - t0) / 5
    assert elapsed < 0.5, f"Equity too slow: {elapsed*1000:.0f}ms"
    print(f"OK equity_speed → {elapsed*1000:.0f}ms per call")


# ---------------------------------------------------------------------------
# Board texture
# ---------------------------------------------------------------------------
def test_board_texture():
    # Dry rainbow board
    t = analyze_board(["Ks", "7h", "2d"])
    assert t["rainbow"]
    assert not t["flush_draw"]
    assert not t["paired"]
    assert t["wetness"] < 0.2
    print(f"OK dry board wetness={t['wetness']:.2f}")

    # Wet monotone connected
    t = analyze_board(["Ts", "9s", "8s"])
    assert t["monotone"]
    assert t["straight_draw"]
    assert t["wetness"] > 0.7
    print(f"OK wet monotone wetness={t['wetness']:.2f}")

    # Paired
    t = analyze_board(["7h", "7c", "2d"])
    assert t["paired"]
    print("OK paired")

    # Flush draw on turn
    t = analyze_board(["As", "7s", "2h", "9s"])
    assert t["flush_draw"]
    print("OK turn fd")


# ---------------------------------------------------------------------------
# Defense ranges
# ---------------------------------------------------------------------------
def test_defense_basic():
    # vs UTG open in BB — AA always 3-bets
    d = defense_action("UTG", "BB", "AA")
    assert d["3bet"] >= 0.95, d
    # vs UTG open in BB — 72o always folds
    d = defense_action("UTG", "BB", "72o")
    assert d["fold"] >= 0.99, d
    # vs BTN open in BB — A5s is a 3-bet bluff
    d = defense_action("BTN", "BB", "A5s")
    assert d["3bet"] > 0.3, f"A5s vs BTN should bluff 3bet: {d}"
    # vs CO open in IP (we're BTN) — JJ mostly 3bets
    d = defense_action("CO", "BTN", "JJ")
    assert d["3bet"] > 0.5, d
    print("OK defense_basic")


def test_4bet_defense():
    # Facing 3bet: AA always 4bets, AKs mostly 4bets, KK mostly 4bets
    assert four_bet_defense("AA")["3bet"] >= 0.99
    assert four_bet_defense("KK")["3bet"] >= 0.9
    # QQ mixed
    d = four_bet_defense("QQ")
    assert 0.3 < d["3bet"] < 0.7
    # AQs mostly calls or folds
    d = four_bet_defense("AQs")
    assert d["3bet"] < 0.1
    # 72o just folds
    assert four_bet_defense("72o")["fold"] >= 0.99
    print("OK 4bet_defense")


# ---------------------------------------------------------------------------
# Push/fold
# ---------------------------------------------------------------------------
def test_pushfold_ranges():
    # 20bb UTG: tight jam range
    r = open_jam_hands(20, "UTG")
    assert "AA" in r and "AKs" in r and "AKo" in r
    assert "72o" not in r and "JTs" not in r

    # 8bb BTN: very wide
    r = open_jam_hands(8, "BTN")
    assert "AA" in r and "76s" in r and "T9s" in r
    assert len(r) > 50

    # Calling jam from UTG: tight
    r = call_jam_range("UTG")
    assert "AA" in r and "KK" in r
    assert "T9s" not in r
    print("OK pushfold_ranges")


# ---------------------------------------------------------------------------
# Full decide() integration
# ---------------------------------------------------------------------------
def _state(hole, board=None, btn_idx=2, n=6, me_idx=5,
           folded_indices=None, pot=1.5, owed=1, current_bet=1,
           stack=100, big_blind=1, can_check=False, street=None,
           action_log=None):
    """Build a synthetic game_state."""
    folded_indices = folded_indices or []
    players = []
    for i in range(n):
        p = {"stack": stack, "chips": stack}
        if i == me_idx:
            p["is_me"] = True
        if i == btn_idx:
            p["is_button"] = True
        if i in folded_indices:
            p["folded"] = True
        players.append(p)
    if street is None:
        street = "flop" if board and len(board) == 3 else (
                 "turn" if board and len(board) == 4 else (
                 "river" if board and len(board) == 5 else "preflop"))
    return {
        "your_cards": hole,
        "community_cards": board or [],
        "street": street,
        "pot": pot,
        "your_stack": stack,
        "amount_owed": owed,
        "can_check": can_check,
        "current_bet": current_bet,
        "min_raise_to": current_bet * 2,
        "players": players,
        "action_log": action_log or [],
        "big_blind": big_blind,
    }


def test_decide_preflop_open():
    # UTG with AA — must open
    state = _state(["As", "Ah"], btn_idx=2, me_idx=5, n=6)
    counts = {"raise": 0, "all_in": 0, "fold": 0, "call": 0, "check": 0}
    for s in range(50):
        a = decide(state, rng=random.Random(s))
        counts[a["action"]] = counts.get(a["action"], 0) + 1
    assert counts["raise"] + counts["all_in"] == 50, counts
    print(f"OK UTG AA opens: {counts}")


def test_decide_preflop_defense_3bet():
    # UTG opens 2.5bb, we're BTN with AA — should 3bet
    state = _state(
        ["As", "Ah"],
        btn_idx=2, me_idx=2, n=6,  # me is btn
        folded_indices=[5],  # UTG is seat 5 — wait need to recompute
        pot=4, owed=2.5, current_bet=2.5,
        action_log=[
            {"action": "raise", "player": 5, "street": "preflop"},  # UTG raised
        ],
    )
    # Need to mark seat 5 (UTG) as the raiser; not folded.
    state["players"][5]["folded"] = False
    # Seat 0 (HJ), seat 1 (CO), all between UTG and BTN folded.
    state["players"][0]["folded"] = True
    state["players"][1]["folded"] = True

    a = decide(state, rng=random.Random(0))
    assert a["action"] in ("raise", "all_in"), f"BTN AA vs UTG open should 3bet: {a}"
    print(f"OK BTN AA vs UTG open → {a}")


def test_decide_short_stack_jam():
    # 12bb stack with AA on BTN — must jam
    state = _state(["As", "Ah"], btn_idx=2, me_idx=2, n=6,
                   stack=12, big_blind=1, pot=1.5, owed=1)
    state["players"][5]["folded"] = True
    state["players"][0]["folded"] = True
    state["players"][1]["folded"] = True
    a = decide(state, rng=random.Random(0))
    assert a["action"] == "all_in", f"Short stack AA on BTN: {a}"
    print(f"OK short stack AA jam → {a}")


def test_decide_postflop_value_bet():
    # AA on K72r with a checked-to spot — should bet for value
    state = _state(
        ["As", "Ah"], board=["Ks", "7h", "2d"],
        btn_idx=2, me_idx=2, n=6,
        pot=6, owed=0, current_bet=0, can_check=True, stack=97,
        action_log=[
            {"action": "raise", "player": 2, "street": "preflop"},
            {"action": "call",  "player": 4, "street": "preflop"},  # BB calls
        ],
    )
    state["players"][5]["folded"] = True
    state["players"][0]["folded"] = True
    state["players"][1]["folded"] = True
    state["players"][3]["folded"] = True
    counts = {"raise": 0, "all_in": 0, "check": 0, "fold": 0, "call": 0}
    for s in range(20):
        a = decide(state, rng=random.Random(s))
        counts[a["action"]] = counts.get(a["action"], 0) + 1
    # Should bet most of the time (some slow-play mix)
    assert counts["raise"] + counts["all_in"] >= 14, f"AA should bet flop: {counts}"
    print(f"OK postflop AA value bet: {counts}")


def test_decide_postflop_fold_to_bet_with_trash():
    # 7-high no draws facing a pot-size bet — must fold
    state = _state(
        ["7s", "2h"], board=["As", "Kh", "Qd"],
        btn_idx=2, me_idx=4, n=6,  # we're BB
        pot=10, owed=10, current_bet=10, can_check=False, stack=90,
        action_log=[
            {"action": "raise", "player": 2, "street": "preflop"},
            {"action": "call",  "player": 4, "street": "preflop"},
            {"action": "bet",   "player": 2, "street": "flop"},
        ],
    )
    state["players"][5]["folded"] = True
    state["players"][0]["folded"] = True
    state["players"][1]["folded"] = True
    state["players"][3]["folded"] = True
    a = decide(state, rng=random.Random(0))
    assert a["action"] == "fold", f"Should fold trash to big bet: {a}"
    print(f"OK fold trash to big bet → {a}")


def test_decide_speed():
    """All decisions must return in well under 2 seconds."""
    state = _state(
        ["Js", "Th"], board=["As", "Kh", "9d", "2s"],
        btn_idx=2, me_idx=4, n=6,
        pot=20, owed=10, current_bet=10, stack=80,
    )
    t0 = time.time()
    a = decide(state)
    elapsed = time.time() - t0
    assert elapsed < 1.0, f"Decision too slow: {elapsed*1000:.0f}ms"
    print(f"OK decide_speed → {elapsed*1000:.0f}ms, action={a}")


# ---------------------------------------------------------------------------
# Robustness — never crash
# ---------------------------------------------------------------------------
def test_never_crashes_on_weird_input():
    # Missing fields, empty lists, weird types
    weird_states = [
        {"your_cards": ["As", "Kh"]},  # almost nothing
        {"your_cards": ["As", "Kh"], "community_cards": [], "street": "preflop"},
        {"your_cards": ["7d", "2c"], "community_cards": ["As", "Kh", "Qs"],
         "street": "flop", "pot": 0, "your_stack": 0, "amount_owed": 0},
    ]
    for s in weird_states:
        a = decide(s)
        assert "action" in a
    print("OK never_crashes")


if __name__ == "__main__":
    test_equity_backend()
    test_equity_basic()
    test_equity_speed()
    test_board_texture()
    test_defense_basic()
    test_4bet_defense()
    test_pushfold_ranges()
    test_decide_preflop_open()
    test_decide_preflop_defense_3bet()
    test_decide_short_stack_jam()
    test_decide_postflop_value_bet()
    test_decide_postflop_fold_to_bet_with_trash()
    test_decide_speed()
    test_never_crashes_on_weird_input()
    print("\nAll tests passed.")
