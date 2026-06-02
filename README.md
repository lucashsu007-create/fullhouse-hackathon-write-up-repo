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

All quality decisions came from backtests against a fixed panel of opponent bots, scored in big-blinds-per-100-hands (bb/100) and in raw chip delta. The methodology below is what made small (~1 bb/100) effects measurable and kept the project from shipping changes that merely looked good in noise.

### 1. Paired A/B, shared seeds

Every comparison ran the candidate and the baseline **in the same job on identical seeded hands**. Because both bots see the same cards and the same opponents, hands where they would act identically contribute exactly zero to the difference, and the paired chip delta is the *exact* head-to-head result rather than a difference of two noisy averages.

```text
paired_diff = total_chips(candidate) - total_chips(baseline)   # same hands
```

- The per-arm 95% CI reported by the harness measures within-arm bounce and is **irrelevant for A/B decisions** — it is used only for absolute standing (eval-mode runs).
- "Does it fire / by how much" was always read from the count of nonzero per-match paired entries, never from a summary block (an early harness bug reported two genuinely-different bots as identical in the summary while the truth was in the per-match data).

### 2. Seed replication for confidence

One seed = one exact paired-diff sample. Confidence comes from running **K independent seed bases** and taking the mean and standard error of the per-seed paired diffs.

- **Seeds, not hands-per-match, tighten the verdict.** A floor estimate swung ~3.5 bb/100 between two 3-seed batches on an *unchanged* bot. 3 seeds is not enough to call a floor; 10 seeds resolved most effects, and 40 seeds were used to settle the one borderline field (`exploiter_mix`).
- SE shrinks as 1/√(seeds). Extra hands-per-match past ~2,500 barely move a paired CI, because beyond saturation the additional hands are mostly spots where the two bots play identically (contributing 0 to the diff). Budget went to seeds, not match length.

### 3. The floor rule (promotion gate)

```text
Promote a candidate only if its paired-diff CI lower bound is >= 0
(within a small tolerance) in EVERY field. Highest mean does not promote.
```

A +2 / −1.5 profile (strong mean, one negative field) was rejected. This asymmetry is deliberate: a top-N cut punishes a bad worst-case far more than it rewards a good average, and every rejected exploit family (V8/V9/V11/V13-lag, the full V16 foundation, `ev_call`) had exactly the strong-mean / negative-floor shape.

### 4. Determinism as a correctness check

The engine is **perfectly deterministic across identical bot instances**: an identical-bot paired diff is exactly 0 chips. This was used as an invariant test for every flag-gated change:

```text
flag OFF vs base, paired  ->  must be exactly 0 chips
```

A non-zero result with the flag off proved a real (if tiny) code divergence — used to catch RNG-stream contamination introduced by editing already-modified files. The fix was discipline: build each variant as a single clean edit from a proven-clean source, then re-verify the 0-chip invariant before measuring EV.

- **Single-hero batches are fake-tight.** A bot run alone against a field shows seed-to-seed stdev ~0.1 (the seeds do not decorrelate), producing artificially narrow CIs. Paired in-job runs show stdev ~0.9 (real variance). "X beats Y" was therefore read only off paired runs; differencing two separate single-hero batches smears the small edges and is unreliable.

### 5. Probe + regression methodology (for exploit/leak fixes)

A leak fix or exploit needs **two kinds of field** to be judged:

- a **probe field** containing the target archetype, so the change can demonstrate value, and
- **regression-control fields** where the baseline already wins, so the change can be shown not to bleed.

A change that beats its probe *and* holds the controls is a real fix the panel was blind to. A change inert on the panel is not necessarily worthless (the panel may lack the target), but a change that helps the probe and hurts a control is rejected. This is why purpose-built sparring opponents exist:

- `perma_allin` / `perma_raiser` — to validate the Tier-1 archetype detector (it gained ~+20–45 bb/100 vs these and was exactly 0 vs all normal fields).
- `board_aware_tag` — a board-aware opponent built to test whether board-aware ranges help in their target regime (they did not; flat-to-negative).
- `river_valuebettor` — a low-aggression, big-river-value-only opponent built to test `river_underbluff_fold` (it gained +3.1 bb/100 there and 0 elsewhere).

### 6. Two-regime test (for gated features)

For any feature gated on an opponent read, the pass bar is all four at once:

```text
1. Invariant : flag OFF == base, exactly 0 chips
2. Liveness  : flag actually fires (else "flat" is "inert", not "neutral")
3. Target    : gains in the regime it is built for
4. Floor     : does not bleed on any realistic field (CI low >= 0)
```

A feature that fires but is flat in its target regime is dropped; a feature that gains in-target but bleeds a control is dropped. Only features that were costless on normal opponents and positive in their target regime (the Tier-1 detector, `river_underbluff_fold`) were shipped.

### 7. One change per candidate

Never bundle two ideas in one arm — paired batches isolate cleanly only if each variant differs from the baseline by a single change. The V16 *foundation* bundled eight fixes and could not be judged as a unit until it was decomposed into single-component arms, which is how the board-range filter was identified as the sole negative-floor source while the rest were neutral or positive.

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
