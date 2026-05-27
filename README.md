## Current Bot Versions and Results

### V4.1: Frozen SafeTAG Baseline

`v4.1` is the frozen control baseline.

It uses:

- 2-bucket positional preflop logic,
- pot-sized opens,
- 4-bucket postflop equity tree,
- `eval7` Monte Carlo equity versus uniform-random opponent hands,
- RAM-only opponent statistics collection,
- implementation and behavior classifier outputs.

Important: the V3 statistics system and V4 classifier are populated, but they are not used for actions in V4.1.

```text
V4.1 action = SafeTAG(state, equity_vs_random)
```

This makes V4.1 the clean baseline for paired A/B testing.

---

### V6: Preflop Chart Extension

`v6` is a preflop-only extension of V4.1.

The postflop logic, equity engine, and classifier behavior remain byte-identical to V4.1.

Main changes:

- replaced the 2-bucket preflop rule with per-position GTO Wizard charts,
- added position-specific charts for UTG, HJ, CO, BTN, and SB,
- added per-seat open sizes from 2.1bb to 3.5bb,
- added 9-table opener-tier × defender-role 3-bet defense,
- added dedicated 4-bet table,
- added position-keyed jam ranges below 20bb effective,
- added heads-up SB override.

```text
V6 = V4.1 + stronger preflop policy
```

#### V6 A/B Results Versus V4.1

| Field | Result vs V4.1 | t-stat | Interpretation |
|---|---:|---:|---|
| Mixed | +16.4 bb/100 | 2.32 | Statistically positive, p ≈ 0.02 |
| Station-heavy | +28.1 bb/100 | 4.19 | Strong positive |
| Tight-heavy | Positive | Not significant | Directionally positive |
| Aggro-heavy | Positive | Not significant | Directionally positive |

Conclusion:

```text
V6 materially improves the baseline, mainly through stronger preflop discipline and sizing.
```

---

### V7: Range-Aware Postflop Equity Extension

`v7` is a postflop-equity extension of V6.

The key change is replacing:

```text
equity_vs_random
```

with:

```text
equity_vs_range
```

Instead of assuming opponents hold uniform-random hands postflop, V7 assigns each opponent a range bucket from their observed action log.

Opponent range buckets:

```text
WIDE
OPEN
THREEBET
NUTTED
UNKNOWN
```

A passive-rocket override upgrades an opponent to `NUTTED` when a `PLAYER_STATS`-confirmed passive villain raises in the current hand.

Multiway equity uses per-iteration combo sampling with deck-conflict rejection. Heads-up spots use the same path to preserve spot-RNG determinism.

Postflop thresholds are unchanged.

```text
V7 = V6 + range-aware postflop equity
```

#### V7 A/B Results Versus V6

| Field | Result vs V6 | t-stat | Interpretation |
|---|---:|---:|---|
| Trap-heavy | +20.3 bb/100 | 4.18 | Strong positive, p < 0.001 |
| Mixed | +10.7 bb/100 | 1.49 | Borderline positive |
| Mixed standard deviation | 15% reduction |  | Lower variance |

Estimated cumulative improvement:

```text
V4.1 -> V7 ≈ +25 bb/100 on mixed fields
```

Conclusion:

```text
V7 improves exploit resistance against trap-heavy opponents while also reducing mixed-field variance.
```

---

## Version Summary

| Version | Main Change | Scope | Status |
|---|---|---|---|
| V4.1 | SafeTAG baseline with unused classifier reads | Baseline | Frozen control |
| V6 | GTO Wizard preflop charts, better opens, 3-bet defense, 4-bets, jam ranges | Preflop | Strong improvement |
| V7 | Range-aware postflop equity instead of random-hand equity | Postflop equity | Current best candidate |

---

## Current Project Status

```text
V4.1: frozen baseline
V6: validated preflop upgrade
V7: validated range-aware postflop upgrade
Current best version: V7
```

Main empirical takeaways:

```text
V6 improves preflop EV versus V4.1.
V7 improves postflop robustness versus V6.
Trap-heavy performance improves significantly.
Mixed-field performance improves directionally.
Mixed-field variance decreases by roughly 15%.
```

The current development conclusion is:

```text
V7 is the leading candidate for final validation.
```

The next step is to run larger paired A/B validation on V7 before treating it as the final submission bot.

---

## Updated Strategic Roadmap

Completed path:

```text
V4.1 frozen SafeTAG baseline
    -> V6 preflop chart upgrade
    -> V7 range-aware postflop equity
```

Current validation target:

```text
stress-test V7 across mixed, station-heavy, tight-heavy, aggro-heavy, and trap-heavy fields
```

Final submission rule:

```text
submit the strongest version only if it improves EV without increasing catastrophic downside risk
```

The central rule remains:

```text
do not break the baseline
```
