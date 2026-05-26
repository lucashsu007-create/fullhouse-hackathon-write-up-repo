# fullhouse-hackathon-write-up-repo
# Fullhouse Hackathon Poker Bot

## Project Overview

This project builds an adaptive no-limit Texas Hold'em poker bot for the Fullhouse Hackathon. The goal is not to approximate full game-theoretic optimal poker. Instead, the bot is designed around a more practical competition objective:

\[
\max_\theta \mathbb{E}[\text{chip delta}]
\]

under strict runtime, sandbox, and implementation constraints.

The current system combines:

- a safe tight-aggressive baseline,
- Monte Carlo equity estimation,
- RAM-only opponent statistics,
- implementation and behavior classification,
- confidence-weighted exploit gating,
- seeded EV backtesting,
- paired A/B testing for strategy changes.

The core philosophy is:

\[
\textbf{safe baseline first, exploit only when evidence is strong}
\]

This avoids the main failure mode in noisy poker-bot competitions: overfitting to a small custom opponent zoo or trusting high-variance backtest improvements.

---

## Current Version: V4.1 Classifier-Retuned Baseline

The current frozen baseline is:

```text
bots/v4_1_classifier_retuned.py
