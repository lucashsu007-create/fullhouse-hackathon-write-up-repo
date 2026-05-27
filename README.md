# Fullhouse Hackathon Poker Bot

## Project Overview

This project builds an adaptive no-limit Texas Hold'em poker bot for the Fullhouse Hackathon.

The objective is not to approximate full game-theoretic optimal poker. Instead, the bot is designed around a practical competition goal:

```text
maximize expected chip delta
```

The current system combines:

- a safe tight-aggressive baseline,
- Monte Carlo equity estimation,
- RAM-only opponent statistics,
- implementation and behavior classification,
- confidence-weighted exploit gating,
- seeded EV backtesting,
- paired A/B testing for strategy changes.

Core philosophy:

```text
Safe baseline first. Exploit only when evidence is strong.
```

This avoids a common failure mode in noisy poker-bot competitions: overfitting to a small custom opponent zoo or trusting high-variance backtest improvements.

---

## Current Baseline: V4.1 Classifier-Retuned SafeTAG

The current frozen baseline is:

```text
bots/v4_1_classifier_retuned.py
```

V4.1 reads opponents, but strategy remains:

```text
SafeTAG + Equity
```

That means the bot collects statistics, computes opponent reads, and stores classifier outputs, but does not yet alter decisions based on those reads.

This makes V4.1 a clean control bot for future V5 A/B tests.

---

## Architecture

The current architecture is:

```text
SafeTAG baseline
    +
Monte Carlo equity engine
    +
RAM-only opponent statistics
    +
implementation classifier
    +
behavior classifier
    +
confidence / sample-size exploit gate
```

In V4.1:

```text
V4.1 action = SafeTAG(state, equity)
```

while the read system separately computes:

```text
Read = top implementation, top behavior, relevant sample size, confidence, exploit weight
```

where:

```text
n      = relevant sample size
c      = confidence
lambda = exploit weight
```

The exploit weight is:

```text
lambda = min(1, n / 60) * max(0, (c - 0.50) / 0.30)
```

In V4.1, `lambda` is only recorded. In V5, it controls how much the bot is allowed to deviate from the baseline.

---

## SafeTAG Baseline

The baseline strategy is deliberately conservative.

### Preflop

Hands are classified into:

```text
premium
strong
playable
speculative
trash
```

The bot opens tighter from early position and wider from later position. Against normal raises, it continues with stronger ranges. Against committing all-ins, it uses an equity filter instead of relying only on a static range chart.

### Postflop

Postflop play is equity-driven when `eval7` is available.

The bot estimates hero equity versus random opponent hands using Monte Carlo simulation. It then applies simple threshold logic:

- high equity: value bet or raise,
- medium equity: value bet, call, or check,
- marginal equity: pot-odds call or check,
- weak equity: fold or occasionally steal on dry boards.

This is not full range-aware poker. It is a robust, low-complexity baseline designed to avoid catastrophic mistakes.

---

## Opponent Statistics

The bot reconstructs public action history and maintains RAM-only statistics for each opponent.

Tracked features include:

```text
actions
VPIP / PFR proxies
true once-per-hand VPIP / PFR
fold vs bet
call vs bet
raise vs bet
all-in rate
street aggression
cheap / medium / expensive call frequencies
```

The key price buckets are based on bet size relative to pot:

```text
cheap      <= 1/3 pot
medium     1/3 to 3/4 pot
expensive  > 3/4 pot
```

This matters because raw fold rate alone is not enough. A pot-odds bot may fold many expensive bets while still calling cheap ones. A true overfolder folds across all sizes.

---

## Classifier Retune Discovery

The first major discovery was that raw `fold_vs_bet` was too dominant.

In 6-max tests, some reasonable bots were incorrectly classified as `folding_bot` because they folded often in aggressive multiway environments. This was dangerous because a high-confidence wrong read could later trigger over-aggressive exploits.

The fix was a classifier retune, not a strategy change.

### Retune changes

1. Down-weighted raw `fold_vs_bet`.

```text
fold_vs_bet weight: 1.0 -> 0.4
```

2. Added a bucket-gradient signal.

This distinguishes:

```text
folds because the price was bad
```

from:

```text
folds across all bet sizes
```

3. Added a folding-bot evidence guard.

The `folding_bot` label cannot reach high confidence unless the opponent also shows low call rates against cheap and medium bets.

4. Reinterpreted `folding_bot`.

In V4.1, `folding_bot` does not mean:

```text
this opponent was coded as a fold bot
```

It means:

```text
this opponent is observed to overfold at this table
```

This is the correct interpretation for downstream exploit logic.

---

## Classifier Probe Results

The classifier was tested against a custom opponent zoo.

### Original custom opponents

```text
calling_station
nit_folder
perma_jam
simple_tag
mc_pot_odds
```

### Expanded probe opponents

```text
balanced_tag
trap_tag
```

Key results:

| Opponent | Final observed read | Interpretation |
|---|---|---|
| calling_station | calling_bot | Correct |
| nit_folder | folding_bot | Correct behaviorally |
| perma_jam | perma_all_in | Correct |
| mc_pot_odds | rule_shark / price-sensitive | Dangerous anti-folder misread fixed |
| trap_tag | rule_shark | Correct, not overfolder |
| simple_tag | folding_bot | Acceptable because observed overfolding was real |
| balanced_tag | folding_bot | Acceptable because observed overfolding was real |

The important goal is not perfect bot-name classification. The goal is behaviorally useful leak detection.

```text
correct leak detection > correct bot-name detection
```

---

## Backtesting Framework

The project uses a custom in-process EV backtester:

```text
backtest.py
```

It supports two modes.

### Evaluation mode

Measures one hero bot against a field:

```bash
python3 backtest.py eval HERO OPPONENT_1 OPPONENT_2 ... --matches 100 --hands 400
```

It reports:

```text
mean chip delta per match
95% confidence interval
bb/100
win rate
```

### Paired A/B mode

Compares two bot variants on identical seeds, seats, and opponent fields:

```bash
python3 backtest.py ab \
  --a bots/candidate.py \
  --b bots/v4_1_classifier_retuned.py \
  --field bots/custom/perma_jam/bot.py ...
```

This is the preferred method for deciding whether a strategy change is real.

The paired setup reduces poker variance because both variants face the same card distribution and seating conditions.

---

## V4.1 Baseline Validation: 500-Match Suite

After initial 100-match evaluations, V4.1 was re-tested with a larger baseline suite to reduce variance and establish a reliable control version before adding V5 exploit modules.

Each field was tested with:

```text
500 matches
400 hands per match
200,000 hands per field
```

Across five fields:

```text
5 fields * 500 matches * 400 hands = 1,000,000 simulated hands
```

All tests were run locally in WSL using real `eval7`.

### Evaluated fields

| Field | Composition | Purpose |
|---|---|---|
| Custom | calling_station, nit_folder, perma_jam, simple_tag, mc_pot_odds | General custom opponent zoo |
| Aggro-heavy | 2x perma_jam, simple_tag, trap_tag, mc_pot_odds | Stress-test versus all-in / aggressive pools |
| Reference | aggressor, mathematician, shark, ref_bot_2 | Generalization outside the custom zoo |
| Tight-heavy | nit_folder, simple_tag, balanced_tag, trap_tag, mc_pot_odds | Stress-test versus tighter / more disciplined opponents |
| Station-heavy | 3x calling_station, simple_tag, nit_folder | Value-extraction test versus calling-heavy pools |

### Results

| Field | Mean delta / match | 95% CI | bb/100 | Win rate | Verdict |
|---|---:|---:|---:|---:|---|
| Custom | +7108.7 | [+5511.8, +8705.7] | +17.77 | 54.4% | Strong positive |
| Aggro-heavy | +7388.8 | [+5676.3, +9101.3] | +18.47 | 50.2% | Strong positive |
| Reference | +8404.2 | [+7407.0, +9401.5] | +21.01 | 76.6% | Strong positive |
| Tight-heavy | +2207.9 | [+944.7, +3471.2] | +5.52 | 45.6% | Strong positive |
| Station-heavy | +10977.2 | [+9487.3, +12467.1] | +27.44 | 69.8% | Strong positive |

### Interpretation

The 500-match suite confirms that V4.1 is a statistically profitable baseline across all tested opponent pools.

```text
V4.1 mean EV > 0 in every tested field
```

All 95% confidence intervals are strictly above zero.

The weakest field is tight-heavy:

```text
tight-heavy = +5.52 bb/100
```

The strongest field is station-heavy:

```text
station-heavy = +27.44 bb/100
```

The reference field result is especially important:

```text
reference = +21.01 bb/100, with 76.6% match win rate
```

This suggests that the bot is not only overfitting to the custom opponent zoo.

---

## Development Conclusion From V4.1

V4.1 is now treated as the frozen control bot for V5 development.

The goal of V5 is no longer to make the bot profitable. V4.1 already is profitable.

The goal is:

```text
improve the weakest positive fields without damaging reference performance
```

Every V5 module must be tested against V4.1 using paired A/B tests.

A module is only kept if it improves the target field and does not clearly damage general-field performance.

---

## V5 Candidate Modules

The current V5 candidates are isolated exploit modules built from V4.1.

### V5a: Anti-Perma-All-In

File:

```text
bots/v5a_antiperma.py
```

Purpose:

```text
avoid marginal stack-offs against detected perma-all-in opponents
```

Trigger conditions:

```text
top_impl == "perma_all_in" with high confidence
```

or:

```text
actions >= 15
allins >= 4
allins / actions > 0.25
```

Allowed behavior changes:

- tighten committing calls versus detected perma-jam opponents,
- avoid marginal stack-offs,
- let the perma-jam bot punt into strong hands.

Explicitly not added:

- anti-folder logic,
- anti-station logic,
- anti-Monte-Carlo logic,
- new bluffing,
- general aggression changes.

---

### V5b: Anti-Calling-Station

File:

```text
bots/v5b_antistation.py
```

Purpose:

```text
extract more value from opponents that call too much
```

Trigger conditions:

```text
top_impl == "calling_bot" with high confidence
```

or:

```text
top_behav == "station" with confidence >= 0.65
```

or:

```text
faced_bet >= 20
call_vs_bet / faced_bet >= 0.65
```

Allowed behavior changes:

- value bet thinner,
- use larger value sizing with strong hands,
- reduce or eliminate bluffs,
- call less marginally when a passive station suddenly raises or jams.

Explicitly not added:

- anti-perma logic,
- anti-folder logic,
- anti-Monte-Carlo logic,
- general aggression changes.

---

### V5c: Capped Anti-Overfolder

File:

```text
bots/v5c_antifolder_capped.py
```

Purpose:

```text
apply small, capped pressure against confirmed overfolders
```

This is the most dangerous module because it adds aggression, so it is heavily gated.

Trigger conditions:

```text
top_impl == "folding_bot"
confidence >= 0.70
lambda > 0.3
```

or:

```text
faced_bet >= 25
fold_vs_bet / faced_bet >= 0.70
```

Additional safety gates:

- do not trigger against stations,
- do not trigger against calling_bot,
- do not trigger against perma_all_in,
- heads-up only,
- dry board only,
- checked pot only,
- no bluffing after villain aggression,
- small sizing only,
- hard frequency cap.

Bluff condition:

```text
fold_needed = bet_size / (pot + bet_size)
```

Only bluff if:

```text
observed_fold_rate > fold_needed + safety_margin
```

Explicitly not added:

- anti-perma logic,
- anti-station logic,
- anti-Monte-Carlo logic,
- broad strategy rewrite.

---

## V5 Testing Protocol

Each V5 module must be tested isolated against V4.1.

Current first-stage target tests:

```text
V5a anti-perma      -> aggro-heavy field
V5b anti-station    -> station-heavy field
V5c anti-overfolder -> tight-heavy field
```

A module is not kept because it sounds strategically correct.

A module is kept only if the paired A/B test supports it.

### Keep / reject logic

```text
Target CI positive:
    keep candidate for combination

Target mean positive but CI crosses zero:
    retest at 300 matches

Target negative:
    reject

Reference CI negative:
    reject unless target gain is very large

Custom CI negative:
    suspicious, retest or reject
```

### Candidate target A/B command

```bash
mkdir -p results && \
python3 backtest.py ab --a bots/v5a_antiperma.py --b bots/v4_1_classifier_retuned.py --field bots/custom/perma_jam/bot.py bots/custom/perma_jam/bot.py bots/custom/simple_tag/bot.py bots/custom/trap_tag/bot.py bots/custom/mc_pot_odds/bot.py --matches 100 --hands 400 --json > results/ab_v5a_aggro_100.json && \
python3 backtest.py ab --a bots/v5b_antistation.py --b bots/v4_1_classifier_retuned.py --field bots/custom/calling_station/bot.py bots/custom/calling_station/bot.py bots/custom/calling_station/bot.py bots/custom/simple_tag/bot.py bots/custom/nit_folder/bot.py --matches 100 --hands 400 --json > results/ab_v5b_station_100.json && \
python3 backtest.py ab --a bots/v5c_antifolder_capped.py --b bots/v4_1_classifier_retuned.py --field bots/custom/nit_folder/bot.py bots/custom/simple_tag/bot.py bots/custom/balanced_tag/bot.py bots/custom/trap_tag/bot.py bots/custom/mc_pot_odds/bot.py --matches 100 --hands 400 --json > results/ab_v5c_tight_100.json
```

---

## Required Opponent Bots For Reproduction

The main custom opponent zoo:

```text
bots/custom/calling_station/bot.py
bots/custom/nit_folder/bot.py
bots/custom/perma_jam/bot.py
bots/custom/simple_tag/bot.py
bots/custom/mc_pot_odds/bot.py
bots/custom/balanced_tag/bot.py
bots/custom/trap_tag/bot.py
```

Reference bots:

```text
bots/aggressor/bot.py
bots/mathematician/bot.py
bots/shark/bot.py
bots/ref_bot_2/bot.py
```

Main hero and candidate files:

```text
bots/v4_1_classifier_retuned.py
bots/v5a_antiperma.py
bots/v5b_antistation.py
bots/v5c_antifolder_capped.py
```

Backtest harness:

```text
backtest.py
```

---

## Local Setup

Recommended environment:

```text
Ubuntu / WSL
Python virtual environment
real eval7 installed
```

Install:

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip

python3 -m venv .venv
source .venv/bin/activate

pip install --upgrade pip
pip install eval7
```

Verify `eval7`:

```bash
python3 - <<'PY'
import eval7
print(eval7.__file__)
print(len(eval7.Deck()))
PY
```

Expected deck size:

```text
52
```

Do not use EV results if `eval7` is missing or stubbed.

---

## Current Project Status

```text
V4.1 baseline: validated
Total baseline validation: 1,000,000 simulated hands
All fields: positive EV
Reference generalization: strong
Weakest field: tight-heavy
V5a: candidate, pending local A/B
V5b: candidate, pending local A/B
V5c: candidate, pending local A/B
Next step: isolated V5 A/B testing
```

---

## Strategic Roadmap

Current stage:

```text
V4.1 = profitable frozen baseline
```

Next stage:

```text
test V5 candidates one by one
```

Final stage:

```text
combine only modules that pass A/B
```

The intended development path is:

```text
V4.1 baseline
    -> V5a anti-perma
    -> V5b anti-station
    -> V5c capped anti-overfolder
    -> combine only winners
    -> final validation
    -> submit safest positive-EV version
```

The central rule remains:

```text
do not break the baseline
```
