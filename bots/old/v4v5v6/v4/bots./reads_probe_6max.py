#!/usr/bin/env python3
"""
reads_probe_6max.py — validate the V4 classifier on a SIX-player table.

The heads-up probe (reads_probe.py) has two structural blind spots: a single
tight hero never shows opponents varied bet sizes (so a pot-odds caller looks
like a station), and tight opponents barely act where the hero observes them
(so they're starved of samples). A 6-max table fixes both: five different bots
bet a range of sizes, and every opponent racks up actions fast.

Setup:
  - seat 0 = hero (v4_baseline.py); seats 1-5 = the five custom bots
  - independent hands with FRESH 10k stacks each hand (no busting) so all six
    stay observable across up to 1000 hands; dealer button rotates each hand
  - hero.decide() drives the hero so its PLAYER_STATS accumulate exactly as in
    a real match; opponents driven by their own decide()
  - at each checkpoint we read the hero's classification of EACH opponent

Reports per opponent at each checkpoint: top implementation + prob, top behavior
+ prob, confidence, lambda, action count, fold-vs-bet, call-vs-bet, all-in rate,
and cheap/medium/expensive bucket coverage (so you can see the price gradient
finally appear).

Does NOT modify the hero. Collection-only — no decision is influenced.

    python3 reads_probe_6max.py --hero v4_baseline.py \
        --opps bots/custom/calling_station/bot.py \
               bots/custom/nit_folder/bot.py \
               bots/custom/perma_jam/bot.py \
               bots/custom/simple_tag/bot.py \
               bots/custom/mc_pot_odds/bot.py \
        --hands 1000 --checkpoints 50 100 200 500 1000
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
    name = f"_probe6_{_seq}"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def play_hand(decide_map, bot_ids, dealer, seed):
    """One 6-handed hand, fresh 10k stacks (no busting -> everyone observable).
    decide_map: {bot_id: decide_callable}. Seating = order of bot_ids."""
    eng = PokerEngine(
        hand_id=f"p6_{seed}",
        bot_ids=list(bot_ids),
        dealer_seat=dealer % len(bot_ids),
        starting_stacks={b: STARTING_STACK for b in bot_ids},
        seed=seed,
    )
    state = eng.start_hand()
    steps = 0
    while state.get("type") == "action_request":
        seat = state["seat_to_act"]
        bid = bot_ids[seat]
        try:
            action = decide_map[bid](state)
        except Exception:
            action = {"action": "fold"}
        if not isinstance(action, dict) or "action" not in action:
            action = {"action": "fold"}
        state = eng.apply_action(seat, action)
        steps += 1
        if steps > 2000:
            break


def snapshot(hero_mod, opp_id):
    st = hero_mod.PLAYER_STATS.get(opp_id)
    if not st or st.get("actions", 0) == 0:
        return None
    impl = hero_mod.classify_implementation(st)
    behav = hero_mod.classify_behavior(st)
    n = hero_mod.get_relevant_sample_size(st)
    # compute_confidence may or may not take n depending on hero version; try both.
    try:
        c = hero_mod.compute_confidence(impl, behav, n)
    except TypeError:
        c = hero_mod.compute_confidence(impl, behav)
    lam = hero_mod.exploit_weight(n, c)
    fb = st["faced_bet"]
    return {
        "n": n,
        "top_impl": max(impl, key=impl.get), "p_impl": impl[max(impl, key=impl.get)],
        "top_behav": max(behav, key=behav.get), "p_behav": behav[max(behav, key=behav.get)],
        "conf": c, "lambda": lam,
        "foldb": st["fold_vs_bet"] / fb if fb else 0.0,
        "callb": st["call_vs_bet"] / fb if fb else 0.0,
        "allin_rate": st["allins"] / st["actions"] if st["actions"] else 0.0,
        "cheap": (st["cheap_called"], st["cheap_faced"]),
        "medium": (st["medium_called"], st["medium_faced"]),
        "expensive": (st["expensive_called"], st["expensive_faced"]),
    }


EXPECTED = {
    "calling_station": "calling_bot / station",
    "nit_folder": "folding_bot / nit",
    "perma_jam": "perma_all_in / maniac",
    "simple_tag": "simple_tag / tag",
    "mc_pot_odds": "monte_carlo / (station-like)",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hero", required=True)
    ap.add_argument("--opps", nargs="+", required=True)
    ap.add_argument("--hands", type=int, default=1000)
    ap.add_argument("--checkpoints", type=int, nargs="+",
                    default=[50, 100, 200, 500, 1000])
    ap.add_argument("--seed-base", type=int, default=5000)
    args = ap.parse_args()

    if len(args.opps) != 5:
        print(f"warning: expected 5 opponents, got {len(args.opps)} "
              f"(table will be {len(args.opps)+1}-handed)")

    hero_mod = load(args.hero)               # fresh -> empty PLAYER_STATS
    hero_id = "HERO"
    opp_ids, opp_mods = [], {}
    for p in args.opps:
        oid = Path(p).parent.name or Path(p).stem
        opp_ids.append(oid)
        opp_mods[oid] = load(p)

    bot_ids = [hero_id] + opp_ids
    decide_map = {hero_id: hero_mod.decide}
    for oid in opp_ids:
        decide_map[oid] = opp_mods[oid].decide

    cps = sorted(set(args.checkpoints + [args.hands]))
    out = []

    def emit(line=""):
        out.append(line)
        print(line)

    emit(f"\n6-max live reads of {args.hero} (V4 classifier)")
    emit(f"table: hero + {len(opp_ids)} custom bots, fresh stacks each hand, "
         f"up to {args.hands} hands")
    emit(f"checkpoints: {cps}\n")

    cpi = 0
    results_at = {oid: {} for oid in opp_ids}
    for h in range(args.hands):
        play_hand(decide_map, bot_ids, dealer=h, seed=args.seed_base + h)
        if cpi < len(cps) and (h + 1) >= cps[cpi]:
            cp = cps[cpi]
            emit(f"================ checkpoint: {cp} hands ================")
            emit(f"  {'opponent':16} {'top_impl':>14} {'p':>5} {'top_behav':>8} "
                 f"{'p':>5} {'conf':>5} {'lam':>5} {'n':>5} {'foldB':>6} "
                 f"{'callB':>6} {'allin':>6}  buckets c/m/e (called/faced)")
            for oid in opp_ids:
                s = snapshot(hero_mod, oid)
                if s is None:
                    emit(f"  {oid:16} {'(no actions yet)':>14}")
                    continue
                c, m, e = s["cheap"], s["medium"], s["expensive"]
                bucket_str = f"{c[0]}/{c[1]}  {m[0]}/{m[1]}  {e[0]}/{e[1]}"
                emit(f"  {oid:16} {s['top_impl']:>14} {s['p_impl']:>5.2f} "
                     f"{s['top_behav']:>8} {s['p_behav']:>5.2f} {s['conf']:>5.2f} "
                     f"{s['lambda']:>5.2f} {s['n']:>5} {s['foldb']:>6.2f} "
                     f"{s['callb']:>6.2f} {s['allin_rate']:>6.2f}  {bucket_str}")
                results_at[oid][cp] = (s["top_impl"], s["conf"], s["lambda"])
            emit()
            cpi += 1

    # Final accuracy summary vs expected archetypes.
    emit("================ SUMMARY: expected vs observed ================")
    emit(f"  {'opponent':16} {'expected':28} {'observed@final':16} {'conf':>5} {'lam':>5}  verdict")
    final = cps[-1]
    for oid in opp_ids:
        exp = EXPECTED.get(oid, "?")
        obs, conf, lam = results_at[oid].get(final, ("(none)", 0.0, 0.0))
        exp_impl = exp.split(" / ")[0].strip()
        ok = (obs == exp_impl) or (oid == "mc_pot_odds" and obs in ("monte_carlo", "calling_bot"))
        verdict = "OK" if ok else "MISCLASS"
        emit(f"  {oid:16} {exp:28} {obs:16} {conf:>5.2f} {lam:>5.2f}  {verdict}")
    emit("\nNote: hero is collection-only; no decision was influenced by these "
         "reads (no strategy change).")

    return out


if __name__ == "__main__":
    main()
