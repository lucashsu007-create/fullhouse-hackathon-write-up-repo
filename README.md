# Fullhouse Poker Bot Technical Report

## Summary

This repository documents the development, testing, and final ship decision for a Fullhouse Hackathon poker bot. The final recommended submission is `v16_ship`, a low-risk extension of `v16_noranges` with two cliff-gated exploit protections:

1. Tier-1 archetype detector for extreme maniacs and perma-all-in opponents.
2. River underbluff fold logic against confirmed low-aggression opponents.

The final matrix test shows that `v16_ship` is identical to `v16_noranges` on realistic fields, while gaining additional EV in targeted tail regimes. The fallback hierarchy is:

| Role | Bot |
|---|---|
| Recommended ship | `v16_ship` |
| Minimal upgrade fallback | `v16_noranges` |
| Anti-station hot-swap | `v16_nr_evsize` |
| Ultra-safe fallback | `V7-M2 (fix)` |

## Final Validation Result

The decisive test was an 8-bot × 8-field × 10-seed paired matrix:

```text
100 matches × 2500 hands
All bots paired in-job
0 errors
```

`v16_ship` passed all realistic floor fields by matching `v16_noranges` exactly, while adding positive targeted edges:

| Target field | `v16_ship` gain |
|---|---:|
| Maniac all-in | +2.6 bb/100 |
| Maniac raiser | +2.8 bb/100 |
| River underbluff | +3.1 bb/100 |

The only borderline realistic field was `exploiter_mix`, where the candidate remained slightly positive but not statistically proven above zero at 10 seeds. This is treated as break-even, not a regression.

## Version Lineage

| Version | Main change | Status |
|---|---|---|
| V4.1 | SafeTAG baseline | Superseded |
| V6 | GTO-style preflop charts and defense tables | Superseded |
| V7 | Range-aware postflop equity | Superseded |
| V7-M2 | Marginal postflop cutoff tightened | Promoted |
| V7-M2 (fix) | Fixed dead 4-bet defense gate | Safe fallback |
| V8/V9 | Board and opponent exploit overlays | Rejected |
| V10 | Wider 3-bet defense and 4-bet bluffs | Rejected |
| V11 | Read-driven exploit engine | Rejected |
| V12 | C-bet balance and continuous policy | Rejected |
| V13 | Range construction refinements | Deferred |
| V14 | Leak-fix flags | Built, not shipped |
| V16 foundation | Correctness and preflop/postflop fixes | Partially accepted |
| `v16_noranges` | V16 without board-range filter | Validated base |
| `v16_ship` | `v16_noranges` plus zero-cost insurance features | Recommended ship |

## Key Technical Fixes

### Dead 4-Bet Gate

The largest fix was a preflop logic bug. The bot intended to call `_four_bet_branch` after opening and facing a 3-bet, but the gate checked whether the last raiser was hero. In a true 4-bet-defense spot, the last raiser is the opponent, so the branch was never reached.

Effect:

```text
Before fix: bot folded all hands, including AA, to 3-bets in many 100bb spots.
After fix: dedicated 4-bet defense table became active.
```

This explained why earlier 3-bet-defense variants appeared byte-identical.

### V16 Decomposition

The V16 foundation initially looked like a broad upgrade, but decomposition showed:

| Component | Result |
|---|---|
| Equity denominator fix | Inert in chip EV |
| Cold 4-bet defense | Positive |
| Board-aware range filter | Negative floor source |
| Position-split opens | Small drag or wash |
| Thin value raises | Wash |

The final validated base therefore removes the board-range filter, producing `v16_noranges`.

## Rejected Strategy Families

### V8/V9: Conditional Exploit Overlays

Rejected because narrow triggers created capped upside and open-ended downside. When the target archetype was absent or misclassified, the overlays replaced good baseline decisions with fragile exploit decisions.

### V10: Wider 3-Bet Defense

Rejected after the 4-bet gate fix. Wider defense created more weak postflop spots and did not improve the floor.

### V11: Live Read-Based Exploitation

Rejected because false positives were too expensive. Even confidence-gated reads fired against competent opponents and bled EV. The main failure mode was bluffing into players who folded often overall but check-raised the hands that continued.

### V12: Balance and Continuous Policy

Rejected because adding bluffs or smoothing the postflop policy did not help against the tested population. The field rewarded value-heavy discipline more than theoretical balance.

### V13/V14: Deferred Leak Fixes

Range narrowing, sizing decoupling, trapping, river value polarization, and multiway caution were built or tested, but not shipped. These require real hand-history calibration before promotion.

## Strategic Finding

The repeated result is:

```text
Input/correctness improvements are robust.
Read-driven policy overrides bleed through false positives.
Eyeballed range narrowing is dangerous without showdown calibration.
Balance is low value against call-heavy or over-aggressive fields.
```

The bot performs best as a disciplined, range-aware TAG that avoids speculative exploit branches unless they are cliff-gated and costless on normal opponents.

## Testing Methodology

Testing used paired A/B backtests with shared hand seeds. Candidate quality was judged by paired chip difference, not by raw per-arm confidence intervals.

Promotion rule:

```text
Ship the bot with the best worst-field floor, not the highest mean.
```

A candidate was promoted only if it avoided meaningful regression across the field panel. Positive average EV was not enough.

## Safety

The final candidates were checked for:

| Check | Result |
|---|---|
| Banned constructs | Clean |
| Runtime crashes | None observed |
| Invalid actions | None observed |
| Time budget | Within limit |
| Backtest errors | 0 in final matrix |

Final upload should still be verified with:

```bash
python3 harden_scan.py path/to/final_bot.py
python3 -m py_compile path/to/final_bot.py
```

## Final Ship Decision

Submit:

```text
v16_ship
```

Reason:

```text
v16_ship = v16_noranges
           + Tier-1 archetype detector
           + river-underbluff-fold
```

It is a strict practical superset of `v16_noranges`: identical on realistic fields, positive in target regimes, and safer than the full V16 foundation because the bleeding board-range filter is removed.

Fallback order:

```text
1. v16_ship
2. v16_noranges
3. v16_nr_evsize
4. V7-M2 (fix)
```
