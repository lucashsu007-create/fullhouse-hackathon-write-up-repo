#!/usr/bin/env python3
"""
reads_probe.py — sanity-check the V4 classifier on LIVE match data.

Plays the hero (V4 bot) heads-up against each reference bot and prints how the
implementation/behavior read converges as observations accumulate. Independent
hands (fresh 10k stacks each hand) so nobody busts and we keep observing the
opponent's true style.

The classifier reads the OPPONENT's actions + the prices they faced, which the
V3 reconstruction recovers from the public action log — so this is valid even
when eval7 is unavailable and the hero's equity engine is on its fallback.

    python3 reads_probe.py --hero bots/safetag_eq/bot.py \
        --opps bots/aggressor/bot.py bots/mathematician/bot.py \
               bots/shark/bot.py bots/ref_bot_2/bot.py \
        --hands 300
"""

import argparse
import importlib.util
import os
import sys
from pathlib import Path


def _find_repo(start):
    cur = Path(start).resolve()
    for d in [cur, *cur.parents]:
        if (d / "engine" / "game.py").is_file():
            return d
    return None


_ROOT = _find_repo(os.getcwd()) or _find_repo(__file__)
if _ROOT:
    sys.path.insert(0, str(_ROOT))

from engine.game import PokerEngine, STARTING_STACK


_seq = 0


def load(path):
    global _seq
    _seq += 1
    name = f"_probe_{_seq}"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def play_independent_hand(hero_mod, opp_decide, hero_id, opp_id, dealer, seed):
    """One heads-up hand with fresh stacks. Hero is driven by hero_mod.decide so
    its PLAYER_STATS updates; opponent by opp_decide."""
    eng = PokerEngine(
        hand_id=f"probe_{seed}",
        bot_ids=[hero_id, opp_id],
        dealer_seat=dealer % 2,
        starting_stacks={hero_id: STARTING_STACK, opp_id: STARTING_STACK},
        seed=seed,
    )
    state = eng.start_hand()
    steps = 0
    while state.get("type") == "action_request":
        seat = state["seat_to_act"]
        bid = [hero_id, opp_id][seat]
        try:
            if bid == hero_id:
                action = hero_mod.decide(state)
            else:
                action = opp_decide(state)
        except Exception:
            action = {"action": "fold"}
        if not isinstance(action, dict) or "action" not in action:
            action = {"action": "fold"}
        state = eng.apply_action(seat, action)
        steps += 1
        if steps > 1000:
            break


def snapshot(hero_mod, opp_id):
    st = hero_mod.PLAYER_STATS.get(opp_id)
    if not st or st.get("actions", 0) == 0:
        return None
    impl = hero_mod.classify_implementation(st)
    behav = hero_mod.classify_behavior(st)
    n = hero_mod.get_relevant_sample_size(st)
    c = hero_mod.compute_confidence(impl, behav, n)
    lam = hero_mod.exploit_weight(n, c)
    top_i = max(impl, key=impl.get)
    top_b = max(behav, key=behav.get)
    return {
        "n": n, "top_impl": top_i, "p_impl": impl[top_i],
        "top_behav": top_b, "p_behav": behav[top_b],
        "conf": c, "lambda": lam,
        "allin_rate": st["allins"] / st["actions"] if st["actions"] else 0,
        "foldb": st["fold_vs_bet"] / st["faced_bet"] if st["faced_bet"] else 0,
        "callb": st["call_vs_bet"] / st["faced_bet"] if st["faced_bet"] else 0,
        "buckets": (
            (st["cheap_called"], st["cheap_faced"]),
            (st["medium_called"], st["medium_faced"]),
            (st["expensive_called"], st["expensive_faced"]),
        ),
    }


def probe(hero_path, opp_path, hands, checkpoints):
    hero_mod = load(hero_path)              # fresh -> empty PLAYER_STATS
    opp_mod = load(opp_path)
    opp_decide = opp_mod.decide
    hero_id = "HERO"
    opp_id = Path(opp_path).parent.name or Path(opp_path).stem

    rows = []
    cps = sorted(set(checkpoints + [hands]))
    cpi = 0
    for h in range(hands):
        play_independent_hand(hero_mod, opp_decide, hero_id, opp_id,
                              dealer=h, seed=1000 + h)
        if cpi < len(cps) and (h + 1) >= cps[cpi]:
            snap = snapshot(hero_mod, opp_id)
            if snap:
                rows.append((h + 1, snap))
            cpi += 1
    return opp_id, rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hero", required=True)
    ap.add_argument("--opps", nargs="+", required=True)
    ap.add_argument("--hands", type=int, default=300)
    ap.add_argument("--checkpoints", type=int, nargs="+",
                    default=[25, 50, 100, 200])
    args = ap.parse_args()

    print(f"\nLive reads of {args.hero} (V4 classifier) vs each reference bot")
    print(f"heads-up, independent hands, up to {args.hands} hands each\n")

    for opp in args.opps:
        opp_id, rows = probe(args.hero, opp, args.hands, args.checkpoints)
        print(f"=== vs {opp_id} ===")
        print(f"  {'hands':>5} {'impl':>16} {'p':>5} {'behav':>8} {'p':>5} "
              f"{'conf':>5} {'lam':>5}  {'allin':>5} {'foldB':>5} {'callB':>5}  buckets(c/m/e)")
        for n_hands, s in rows:
            b = s["buckets"]
            bstr = (f"{b[0][0]}/{b[0][1]} {b[1][0]}/{b[1][1]} {b[2][0]}/{b[2][1]}")
            print(f"  {n_hands:>5} {s['top_impl']:>16} {s['p_impl']:>5.2f} "
                  f"{s['top_behav']:>8} {s['p_behav']:>5.2f} {s['conf']:>5.2f} "
                  f"{s['lambda']:>5.2f}  {s['allin_rate']:>5.2f} {s['foldb']:>5.2f} "
                  f"{s['callb']:>5.2f}  {bstr}")
        print()


if __name__ == "__main__":
    main()
