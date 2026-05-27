# Useful GTO Bot — Fullhouse Hackathon

Real postflop, real defense ranges, real short-stack push/fold. Beats every
reference archetype in head-to-head simulation.

## What changed vs the v1 framework

The v1 was preflop-correct but stubbed everything else. v2 adds:

| Module | What it does |
|--------|--------------|
| `equity.py` | Monte Carlo equity via eval7 or treys (~50ms / 1500 iters) |
| `board.py` | Texture analysis — wetness score, flush draws, paired, monotone |
| `defense.py` | 3-bet defense charts for vs-EARLY / MIDDLE / LATE × IP / OOP / BB |
| `pushfold.py` | Nash-style open-jam + calling-jam ranges at 20bb / 12bb / 8bb |
| `postflop.py` | Equity-driven decisions, c-bet logic, sizing tied to texture |
| `strategy.py` | Orchestrator with HU override, short-stack mode, PFR detection |

## Self-test results

```
useful_bot vs reference archetypes  (1000 hands each)
========================================================
vs baseline      :  +35.8 BB/100   [WIN]
vs aggressor     :  +28.0 BB/100   [WIN]
vs tight         :  +61.0 BB/100   [WIN]
vs mathematician :  +10.1 BB/100   [WIN]
```

Caveat: this is a simplified HU simulator that goes to showdown after one
round of preflop betting — it's a sanity check, not a strength rating. Real
testing requires the actual `sandbox/match.py` from the hackathon repo.

## Decision flow

```
decide(state)
  │
  ├── eff_stack ≤ 20bb? ─→ pushfold module
  │     └── facing jam? call-range lookup
  │         not facing jam, first in? open-jam range lookup
  │         facing raise? defense (tightened)
  │
  ├── preflop?
  │     ├── unopened ─→ RFI chart (HU override if 2 players)
  │     ├── facing 3-bet (we PFR) ─→ 4-bet defense
  │     └── facing raise ─→ 3-bet defense (opener_tier × defender_role)
  │
  └── postflop
        ├── equity = MC(hand, board, n_opp, 1500 iters)
        ├── eq_shaded = eq × (0.85 if 3bet-pot, 0.92 if single raise, 1.0 else)
        ├── class = monster/strong/medium/weak_made/draw/air
        ├── facing bet? raise / call (pot odds + 5%) / fold (by class)
        └── checked to us? value bet / c-bet (as PFR) / semi-bluff / check
```

## Key behaviors worth knowing

**Bet sizing is texture-aware.** C-bets are 33% pot on dry boards, 66% on
wet. Value bets are 55% / 75% / 85% on flop/turn/river respectively. All
constants are tunable at the top of `postflop.py`.

**Heads-up override.** When only 2 players are in the hand, the SB chart
(which assumes 6 other folders) is bypassed — the bot opens ~85% of HU
button hands instead of limping. This matters when the tournament gets to
short tables late.

**Short-stack mode.** Below 20bb effective, the bot switches to jam-or-fold
ranges. Below 12bb, it's pure push-fold (no min-raises). Calibrated to
beat passive opponents who fold too much vs jams.

**Equity-vs-random with shading.** Postflop equity is calculated vs a
random opponent, then multiplied by 0.85 in 3bet pots / 0.92 in single
raised pots. This compensates for the fact that opponents who paid to see
the flop have stronger ranges than random.

**Never crashes.** Every entry point has a top-level try/except returning
`{"action": "fold"}` or `{"action": "check"}`. Validator-safe.

## Files

```
useful_bot/
├── gto/                       # The framework
│   ├── __init__.py            # Public API
│   ├── hand_notation.py       # ["As","Kh"] → "AKo"
│   ├── equity.py              # Monte Carlo equity (eval7 or treys)
│   ├── board.py               # Texture analysis
│   ├── ranges.py              # RFI charts (UTG..BTN, SB)
│   ├── defense.py             # 3bet / 4bet defense
│   ├── pushfold.py            # Short-stack jam tables
│   ├── position.py            # Seat detection, 6↔8 max mapping
│   ├── postflop.py            # Postflop decisions
│   └── strategy.py            # Orchestrator + entry point
├── bot.py                     # Multi-file dev bot (imports gto/)
├── build_single_file.py       # Bundler → dist/bot.py
├── dist/
│   └── bot.py                 # Submission-ready single file
└── tests/
    ├── test_useful.py         # Sanity tests
    ├── sim_vs_baseline.py     # HU simulator
    └── sim_archetypes.py      # Vs aggressor/tight/math
```

## Workflow

```bash
# Develop in gto/, run tests
python3 tests/test_useful.py

# Run simulations
python3 tests/sim_archetypes.py

# Build submission file
python3 build_single_file.py

# Upload dist/bot.py
```

## What's still missing (in priority order)

1. **Multi-street equity tracking** — equity is recomputed from scratch each
   street. A small cache + delta-update would shave 30% off latency.
2. **Opponent modeling** — we know `action_log` includes all betting but
   nothing tracks per-opponent VPIP/aggression. Adding a single counter per
   seat would unlock significant exploitative EV.
3. **River bet sizing variety** — currently one size per spot. Polarized
   over-bets with nuts + air are a hole in the bot's river game.
4. **Multiway postflop** — equity vs N opponents is computed, but the
   sizing/threshold logic mostly assumes HU. Multiway value betting is
   tighter than HU and we'd benefit from a multiway path.
5. **Limped pot defense** — when SB limps and BB checks, the bot plays the
   "no preflop raiser" branch and may c-bet too aggressively as if we have
   range advantage. We don't, on a limped pot.

## Tuning dials

All in `postflop.py`:
```python
CBET_FREQ_HU_DRY = 0.85       # how often to c-bet dry HU boards
CBET_FREQ_HU_WET = 0.55       # ditto wet boards
CBET_FREQ_MULTIWAY = 0.30     # multiway c-bet
VALUE_BET_EQUITY_THRESHOLD = 0.60
RAISE_FOR_VALUE_EQUITY_THRESHOLD = 0.72
EQ_SHADE_VS_CALLER = 0.92
EQ_SHADE_VS_RAISER = 0.85
```

In `strategy.py`:
```python
SHORT_STACK_BB = 20.0  # threshold to switch to jam-or-fold
```

Change these, run `test_useful.py` to verify nothing breaks, run
`sim_archetypes.py` to verify net BB/100 went up not down.
