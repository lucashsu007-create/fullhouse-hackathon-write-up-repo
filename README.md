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

## Qualifier 1 Result (LIVE) — qualified for finals, 5th of top 64

Submitted `v16_ship` (shown as `v16ship2`) to the live Fullhouse 2026 Qualifier. **Result: 5th place of the top 64 that advance to the finals.**

| Metric | Value |
|---|---:|
| Rank | 5 / top 64 |
| Avg Δ / match | +16,462 |
| Total Δ | +246,935 |
| Win % | 60% |
| Matches | 15 |

The cut for the top 64 was approximately +5,000 avg/match, so the bot finished roughly **3× above the qualification line** — a comfortable, not marginal, qualification.

**The live variance profile matched the design intent.** Per-match chip deltas were strongly bimodal: the bot either tabled a stack (multiple +50,000 matches) or lost a single capped buy-in (−10,000), rarely landing in between. Across the logged matches the per-match standard deviation was ~24,000 with a median of ~+14,000 and a hard −10,000 downside floor. This right-skewed, downside-bounded distribution — big wins, capped losses — is exactly the profile a cumulative-chip, top-N cut rewards, and is why a 60% win rate produced a top-5 finish.

**Earlier-version matches confirm the hot-swap was safe.** The qualifier allowed re-uploading mid-event; matches played under the earlier `V7-M2 (fix)` build were also solidly positive (mean ~+9,700/match), so switching to `v16_ship` during the qualifier did not cost ground.

### Field analysis from the released hand histories (8,070 hands, 725 showdowns)

When per-hand replays were released, profiling the real opponent field confirmed the strategic assumptions the bot was built on:

- **The field is tight-passive and over-folds, not balanced/board-aware.** Facing a raise, the field folded **67% of the time across ~9,900 spots**; many opponents folded 78–96% to raises. Fold-to-bet rates were similarly high across most bots.
- **Solver-named bots did not play like solvers.** Opponents named `gto`, `EquityBot`, `TheQuantBot`, etc. exhibited loose-passive or over-folding behavior, not balanced defense. The names were aspirational; the behavior was exploitable.
- **Implication for the architecture's known blind spot:** the field showed no evidence of the board-aware/balanced archetype that the board-blind range model is weak against. This is direct, real-field support for shipping `v16_noranges`/`v16_ship` (board filter removed) rather than a board-aware variant — the board-aware range filter bled against exactly the tight-passive population this field turned out to be.
- **The field over-folds to raises**, which is the regime the `river_underbluff_fold` flag and the bot's value-heavy aggression already target. The theoretical edge left on the table (raising even more thinly to harvest the high fold-to-raise rate) was deliberately **not** shipped: at only ~400 hands per match the per-opponent sample is too thin to gate such an exploit reliably, and an untested thin-value expansion risks the sticky minority (the ~33% who call/re-raise) — the same thin-value wash and read-driven false-positive failure modes documented below.

## Final Validation Result

The decisive pre-submission test was an 8-bot × 8-field × 10-seed paired matrix:

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

The only borderline realistic field was `exploiter_mix`, where the candidate remained slightly positive but not statistically proven above zero. A follow-up 40-seed run on `exploiter_mix` resolved it to **+0.18 bb/100, CI [−0.10, +0.45]** — a statistical dead heat with `V7-M2 (fix)`, i.e. break-even, not a regression. All other fields were clear positive passes.

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
| `v16_ship` | `v16_noranges` plus zero-cost insurance features | Recommended ship (qualified 5th) |

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

The final validated base therefore removes the board-range filter, producing `v16_noranges`. The field analysis above (a tight-passive, over-folding population) independently confirms this was the correct call.

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

Range narrowing, sizing decoupling, trapping, river value polarization, and multiway caution were built or tested, but not shipped. These require real hand-history calibration before promotion. Note: `multiway_caution` in particular showed an early negative read on station-heavy fields (tightening value against opponents who pay off) and was deliberately excluded from `v16_ship`.

### Board-awareness and per-opponent exploit gating (patch-window experiments)

Two further ideas were investigated against the released field data and rejected for shipping:

- **In-bot board-aware range adaptation** was non-viable: the postflop fold signal needed to classify an opponent as board-aware does not accumulate at usable rates within a 400-hand match. A purpose-built board-aware sparring opponent also showed the board filter only flips ~10% of decisions and those flips roughly cancel.
- **Per-opponent "raise thinner vs confirmed over-folders" gating** was rejected on the 400-hand sample constraint: the gate would cross threshold only late in a match, soft behavioral thresholds at ~15–40 observations per opponent invite false positives, and the bleed risk concentrates in the sticky minority that calls/re-raises — the V11 failure mode. The field-wide over-folding is real, but neither a per-opponent nor a static thin-value expansion clears the bar without showdown-calibrated validation.

## Strategic Finding

The repeated result is:

```text
Input/correctness improvements are robust.
Read-driven policy overrides bleed through false positives.
Eyeballed range narrowing is dangerous without showdown calibration.
Balance is low value against call-heavy or over-aggressive fields.
```

The bot performs best as a disciplined, range-aware TAG that avoids speculative exploit branches unless they are cliff-gated and costless on normal opponents. The live qualifier result and the released hand histories corroborate this: the field was tight-passive and over-folding, and the disciplined value-heavy bot finished comfortably above the cut.

## Testing Methodology

Testing used paired A/B backtests with shared hand seeds. Candidate quality was judged by paired chip difference, not by raw per-arm confidence intervals.

Promotion rule:

```text
Ship the bot with the best worst-field floor, not the highest mean.
```

A candidate was promoted only if it avoided meaningful regression across the field panel. Positive average EV was not enough.

Two power-related lessons were reinforced empirically:

- **Seeds, not hands-per-match, tighten the verdict.** A floor estimate swung ~3.5 bb/100 between two 3-seed batches on an unchanged bot; 10–40 seeds were required to resolve ~1 bb/100 effects.
- **Single-hero batches are fake-tight.** Reading "X beats Y" off two separate single-hero runs is unreliable (seed-to-seed stdev ~0.1, an artifact); only paired in-job runs (stdev ~0.9, real variance) are trustworthy for comparisons. The engine is otherwise perfectly deterministic across identical bots (identical-bot paired diff = exactly 0).

## Safety

The final candidates were checked for:

| Check | Result |
|---|---|
| Banned constructs | Clean |
| Runtime crashes | None observed (0 exceptions over 6,000 fuzz states) |
| Invalid actions | None observed |
| Time budget | Within limit (0 timeouts in the live qualifier) |
| Backtest errors | 0 in final matrix |

Final upload should still be verified with:

```bash
python3 harden_scan.py path/to/final_bot.py
python3 -m py_compile path/to/final_bot.py    # on a real Python 3.10 interpreter
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

It is a strict practical superset of `v16_noranges`: identical on realistic fields, positive in target regimes, and safer than the full V16 foundation because the bleeding board-range filter is removed. **This is the build that qualified 5th in Qualifier 1.**

Fallback order:

```text
1. v16_ship
2. v16_noranges
3. v16_nr_evsize
4. V7-M2 (fix)
```
