# Fullhouse Hackathon Poker Bot

## Project Overview

This project builds an adaptive no-limit Texas Hold'em poker bot for the Fullhouse Hackathon.

The objective is not to approximate full game-theoretic optimal poker. Instead, the bot is designed around a practical competition goal:

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

```text
safe baseline first, exploit only when evidence is strong
