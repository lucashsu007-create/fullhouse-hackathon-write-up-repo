# Fullhouse Poker Bot Technical Report

## Summary

This repository documents the development, testing, and final ship decision for a Fullhouse Hackathon poker bot.

The current finals submission is:

```text
v16_squeeze_C_metacap
```

This is a conservative finals patch built on the validated `v16` / `v16_noranges` family. It keeps the proven aggressive baseline, keeps board-aware range narrowing disabled, and adds only small, cliff-gated value/capped-line punishers that showed no regression in the final confirmation sweeps.

Final fallback hierarchy:

| Role | Bot |
|---|---|
| Recommended finals ship | `v16_squeeze_C_metacap` |
| Cleanest low-complexity fallback | `v16_posfix_nutvalue` |
| Original live baseline | `v16_ship` |
| Ultra-safe fallback | `V7-M2 (fix)` |

The final upload was verified mechanically:

```text
BOT_NAME = v16_squeeze_C_metacap
python3 -m py_compile: OK
import: OK
decide(game_state): present
```

Expected active flags:

```python
_V16_FLAGS = {
    "ev_call": False,
    "spr": False,
    "action_filter": False,
    "blocker_bluff": False,
    "river_underbluff_fold": True,
    "nutted_value_sizing": True,
    "premium_squeeze": True,
    "tiny_size_tweak": False,
}
```

## Qualifier 1 Result (LIVE)

Submitted `v16_ship` to the live Fullhouse 2026 qualifier. The bot qualified for the finals.

A later live ranking context from this chat placed `v16_ship` around **rank 28 / 64 total**, so the finals patch work should be interpreted as an attempt to move a qualified but not favorite-tier bot into the contender band, not as proof that the bot is already the strongest in the field.

## Qualifier II / Finals Hand-History Findings

A merged set of released match histories was analysed during the finals patch window.

Key aggregate result for Pantheon:

| Metric | Value |
|---|---:|
| Unique matches | 36 |
| Recorded hands | 21,273 |
| Total chip delta | +433,707 |
| Avg chip delta / match | +12,047 |
| First-place finishes | 14 / 36 |
| Top-2 finishes | 30 / 36 |
| Busts | 17 / 36 |

Interpretation:

```text
The bot is clearly +EV, but high variance.
It wins large pots often enough to justify aggression.
It also busts often enough that small punt filters were worth investigating.
```

### Big-pot diagnostics

Showdown analysis did **not** support broad anti-bust tightening.

Revealed showdown sample:

| Bucket | Result |
|---|---:|
| Revealed showdowns | 922 |
| Wins | 476 |
| Losses | 446 |
| All revealed pots, raw signed | +1,697,271 |
| Pots >= 5k | +1,667,036 |
| Pots >= 10k | +1,635,993 |
| Pots >= 20k | +1,563,440 |

Medium-strength hands in large pots were also profitable overall:

| Bucket | Wins | Losses | Raw signed |
|---|---:|---:|---:|
| Medium hands, pot >= 10k | 43 | 20 | +950,275 |
| Medium hands, pot >= 20k | 32 | 7 | +963,951 |

Conclusion:

```text
Do not globally avoid big pots.
Do not globally nerf medium-hand stackoffs.
The bot's edge comes from fast chip accumulation, not survival.
```

The real leak buckets were narrower:

- Top pair in huge pots.
- Fake two pair on paired boards.
- Low flushes on dangerous flush boards.
- Sets or trips on boards where straights completed.

However, later tests showed that even narrow fake-strength filters reduced EV, because the bad spots were rare and the filters interfered with normal profitable aggression.

## Version Lineage

| Version | Main change | Status |
|---|---|---|
| V4.1 | SafeTAG baseline | Superseded |
| V6 | GTO-style preflop charts and defense tables | Superseded |
| V7 | Range-aware postflop equity | Superseded |
| V7-M2 | Marginal postflop cutoff tightened | Promoted historically |
| V7-M2 (fix) | Fixed dead 4-bet defense gate | Safe fallback |
| V8/V9 | Board and opponent exploit overlays | Rejected |
| V10 | Wider 3-bet defense and 4-bet bluffs | Rejected |
| V11 | Read-driven exploit engine | Rejected |
| V12 | C-bet balance and continuous policy | Rejected |
| V13/V14 | Leak-fix flags and range refinements | Deferred or rejected |
| V16 foundation | Correctness and preflop/postflop fixes | Partially accepted |
| `v16_noranges` | V16 with board-aware range filter disabled | Validated base |
| `v16_ship` | `v16_noranges` plus low-cost insurance features | Qualified, later used as baseline |
| `v16_posfix_nutvalue` | `v16_ship` plus postflop position fix and nut-region value sizing | Best clean upgrade |
| `v16_squeeze_C_AA_KK_QQ_AKs` | Adds premium squeeze with AA/KK/QQ/AKs, no size tweak | Slight raw gain, mostly identical to posfix |
| `v16_squeeze_C_metacap` | Adds premium squeeze plus capped-line punishers | Current finals ship |

## Key Technical Fixes and Findings

### 1. Keep board-aware range narrowing disabled

The code still contains the range-filter machinery, but the decisive line remains:

```python
mode = "none"   # disable board-aware narrowing
```

This is intentional. Board-aware range filtering was repeatedly a negative-floor source. It looked theoretically correct but hurt against the tested fields, which were not balanced enough to justify the narrowing.

Final decision:

```text
Keep board filters disabled.
Do not re-enable action_filter.
Do not re-enable board-aware range narrowing.
```

### 2. Postflop position fix

Original `_position(state)` used raw seat index, which can create seat-order skew postflop. The accepted fix maps acting order more sensibly in postflop spots.

This was one of the few correctness-style changes that survived testing.

### 3. Nut-region value sizing

The accepted value accelerator is deliberately narrow:

```text
full house+
nut flush
safe set at low SPR
```

It does **not** add broad value acceleration with overpairs, top pair, generic straights, or fragile two pair.

This was the core of `v16_posfix_nutvalue`, which beat `v16_ship` cleanly in repeated batches.

### 4. Premium squeeze C

The tested squeeze variants were:

| Variant | Range |
|---|---|
| A | AA, KK |
| B | AA, KK, QQ |
| C | AA, KK, QQ, AKs |
| D | AA, KK, QQ, AKs, AKo |
| E | Position-aware squeeze |

The best raw scorer was variant C, but the realized difference from `v16_posfix_nutvalue` was tiny because the trigger rarely fired.

Final decision:

```text
Keep squeeze C because it is theoretically sound and directionally positive.
Do not include AKo in squeeze.
Do not include tiny open-size or 3-bet-size tweaks.
```

### 5. Metacap punishers

`v16_squeeze_C_metacap` added narrow capped-line punishers:

```text
1. Limp punish with AA/KK/QQ/AKs
2. Min-raise punish with AA/KK/QQ/AKs
3. Tiny flop/turn donk punish with value only
4. River block-bet punish with nut flush / full house+
5. Raise sanitizer guard
```

These branches are rare. The observed edge was small, but they did not increase bust rate or lose the important control fields.

Final decision:

```text
Keep metacap.
Do not stack additional broad postflop logic on top.
```

## Rejected Patch-Window Experiments

### Broad fake-strength filters

Rejected. Filters for top pair, fake two pair, low flush, overpair-on-paired-board, and river-completion discipline looked correct from horror hands, but reduced EV in A/B.

Reason:

```text
The bad hands were too rare.
The filters touched too many normal profitable spots.
```

### Broad value acceleration

Rejected. `v21_value_accel` and later pressure variants increased activity but reduced EV versus `v16_ship` and `v16_posfix_nutvalue`.

Failure mode:

```text
More aggression with non-nut hands created bigger pots without enough fold equity.
```

### Pressure package

Rejected. Flop c-bet pressure, delayed turn stabs, and wider pressure logic performed worse than the baseline family.

### Meta-counter variants that did not fire

These were tested on top of `C_metacap`:

| Variant | Intended change | Result |
|---|---|---|
| `tightdonk` | Remove clean overpair from tiny-donk punish | Identical in realized results |
| `blockstraight` | Add safe straight to river block-bet punish | Identical in realized results |
| `tightminraise` | Restrict min-raise punish to AA/KK/QQ | Identical in realized results |

A 30-job, 750-match sweep showed all four versions exactly equal in realized chip results:

```text
C_metacap = blockstraight = tightdonk = tightminraise
```

Therefore the cleanest file remains `v16_squeeze_C_metacap`.

## Final Validation Results

### Confirmation sweep, seeds 33 to 37

The final confirmation batch compared:

```text
v16_ship
v16_posfix_nutvalue
v16_squeeze_C_AA_KK_QQ_AKs
v16_squeeze_C_metacap
```

Settings:

```text
25 jobs
625 matches per bot
5 presets
0 errors
```

Aggregate result:

| Bot | bb/100 | Mean Δ | Win rate | Bust rate | First-place |
|---|---:|---:|---:|---:|---:|
| `v16_squeeze_C_metacap` | **36.36** | **29,085** | **77.28%** | **22.40%** | **72.00%** |
| `v16_squeeze_C_AA_KK_QQ_AKs` | 36.29 | 29,034 | 77.28% | 22.40% | 71.84% |
| `v16_posfix_nutvalue` | 36.08 | 28,864 | 76.96% | 22.56% | 71.52% |
| `v16_ship` | 34.46 | 27,566 | 75.36% | 24.16% | 68.96% |

Paired diff versus `v16_squeeze_C_metacap`:

```text
v16_ship:          -1.90 bb/100, t ≈ -2.77
v16_posfix:        -0.28 bb/100, t ≈ -1.88
plain squeeze C:   -0.06 bb/100, t ≈ -1.11
```

Interpretation:

```text
C_metacap clearly beats v16_ship.
C_metacap is directionally ahead of posfix_nutvalue.
C_metacap is only slightly ahead of plain squeeze C.
The edge is small, but the fallback controls are clean.
```

Preset-level result:

```text
ref_competent: C_metacap best
wall_balanced: C_metacap best
broad_mix2: C_metacap best
nit_field: tied
barrel_field: tied / best
```

This clears the final ship rule:

```text
Ship C_metacap if it beats v16_ship,
does not lose wall_balanced,
does not lose barrel_field,
and remains at least tied with posfix_nutvalue.
```

## Strategic Finding

The repeated result is:

```text
Correctness-style improvements are robust.
Broad exploit overlays bleed.
Board-aware range narrowing is dangerous without reliable calibration.
The bot wins by fast value-heavy chip accumulation, not by survival.
Small capped-line punishers are acceptable only if they are rare and do not alter normal aggression.
```

The final bot remains close to the proven `v16` core. The accepted changes are intentionally small:

```text
postflop position fix
nut-region value sizing
premium squeeze C
rare capped-line punishers
raise sanitizer guard
```

## Testing Methodology

All quality decisions came from paired A/B testing against fixed opponent panels, scored in bb/100 and raw chip delta.

### 1. Paired A/B, shared seeds

Every comparison ran candidate and baseline in the same job on identical seeded hands.

```text
paired_diff = total_chips(candidate) - total_chips(baseline)
```

This matters because most variants play identically on most hands. Shared seeds make the nonzero spots visible.

### 2. Seed replication

Confidence came from independent seed bases, not from only increasing hands per match. Small effects around 1 bb/100 need many seed bases to stabilize.

### 3. Floor rule

A candidate is only promoted if it does not create a negative floor in important control fields.

```text
Highest mean alone is not enough.
A strong mean with a bad wall_balanced or barrel_field result is rejected.
```

### 4. Determinism check

Identical bot instances should produce exactly 0 paired chip difference. This was used to verify that variants were either truly inert or firing only in rare spots.

### 5. One change per candidate

The late patch window focused on narrow ablations. Bundled ideas were avoided unless they had already passed as independent components.

## Safety

The final candidate was checked for:

| Check | Result |
|---|---|
| Syntax compile | OK |
| Import | OK |
| `decide(game_state)` present | OK |
| Invalid action smoke tests | Passed |
| Time budget in sweeps | 0 slow actions |
| Backtest errors in final confirmation | 0 |
| Board-aware range filters | Disabled |
| `ev_call` | Disabled |
| Broad c-bet / turn-stab package | Not included |
| Broad defensive folds | Not included |

Final upload should still be verified locally with:

```bash
python3 -m py_compile bot.py
python3 harden_scan.py bot.py
```

## Final Ship Decision

Submit:

```text
v16_squeeze_C_metacap
```

Reason:

```text
v16_squeeze_C_metacap
= validated v16 core
+ postflop position fix
+ nut-region value sizing
+ premium squeeze C
+ rare capped-line punishers
+ sanitizer guard
```

It is the best current finals candidate because it:

```text
beats v16_ship clearly,
is directionally ahead of v16_posfix_nutvalue,
does not lose wall_balanced,
does not lose barrel_field,
has 0 errors in final confirmation sweeps,
and keeps all previously rejected broad overlays disabled.
```

Fallback order:

```text
1. v16_squeeze_C_metacap
2. v16_posfix_nutvalue
3. v16_ship
4. V7-M2 (fix)
```
