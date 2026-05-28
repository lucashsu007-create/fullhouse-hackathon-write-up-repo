# Poker Bot — Technical Report

**Current frozen baseline: V7 (`v7_range_equity`).**
V8 and V9 evaluated and rejected. See [Why V8/V9 were rejected](#why-v8v9-were-rejected).

---

## Version lineage

| Version | Main change | Scope | Status |
|---|---|---|---|
| V4.1 | SafeTAG baseline; classifier reads collected but unused | Baseline | Superseded |
| V6 | Per-position GTO-Wizard preflop charts, opens, 3-bet/4-bet defense, jam ranges | Preflop | Validated upgrade |
| V7 | Range-aware postflop equity (`equity_vs_range`) replacing random-hand equity | Postflop equity | **Frozen baseline** |
| V8.1 | Board-conditioned NUTTED range on coordinated boards | Postflop range | Rejected (no gain) |
| V9 / V9a-c | Opponent-conditional exploit modules (anti-perma / anti-station / anti-folder) | Exploits | Rejected (no gain, downside) |

Validated cumulative improvement V4.1 → V7 ≈ +25 bb/100 on mixed fields. V6 was statistically positive vs V4.1 (mixed +16.4 bb/100, t≈2.32; station-heavy +28.1, t≈4.19). V7 was strong positive vs V6 on trap-heavy (+20.3 bb/100, t≈4.18), directionally positive on mixed (+10.7, t≈1.49), with ~15% lower mixed-field variance.

---

## What V7 is

V7 is a tight-aggressive bot with three layers:

- **Preflop (V6):** per-position RFI charts (UTG/HJ/CO/BTN/SB), per-seat open sizes (2.1–3.5bb), opener-tier × defender-role 3-bet defense, dedicated 4-bet table, sub-20bb jam ranges, heads-up SB override.
- **Postflop (V7):** Monte Carlo equity via `eval7`, sampling each live opponent from a **range bucket** (`WIDE / OPEN / THREEBET / NUTTED / UNKNOWN`) inferred from their action log, instead of uniform-random hands. A passive-rocket override upgrades a stats-confirmed passive villain to `NUTTED` when they raise. Equity feeds a fixed 4-bucket decision tree; thresholds and sizings are unchanged from V6.
- **Stats/classifier:** RAM-only opponent stats and implementation/behavior classifiers are populated but **collection-only** — they do not drive actions.

Design property that matters for robustness: **V7 has no opponent-model-dependent branch in the action path.** Range estimation only changes the equity *number*; it never swaps the policy. The equity engine degrades gracefully (`equity_vs_range` → `equity_vs_random` fallback on any failure). V7 makes the same well-calibrated decision regardless of regime.

---

## Why V8/V9 were rejected

V8 (board-conditioned NUTTED) and V9 (three opponent-conditional exploit modules) are **conditional overlays** on V7: each only changes a decision when a narrow trigger fires, otherwise falling through to V7. This gives them capped upside and open-ended downside — a false-positive trigger replaces a good baseline decision with a regime-specific one.

Paired A/B (shared spot seeds), 100 matches × 500 hands per field:

| field | V7 | V8.1 | V9 (all) | V9a perma | V9b station | V9c folder |
|---|---|---|---|---|---|---|
| barrel | **30.49** | 30.49 | 29.76 | 30.49 | 30.65 | 29.60 |
| polar3bet | 16.61 | 16.87 | **17.12** | 16.87 | 16.87 | 17.12 |
| adaptive | **23.33** | 22.23 | 22.31 | 22.23 | 22.04 | 22.52 |

(bb/100). Findings:

- **No extension beats V7 by a meaningful margin in any field.** V7 is best-or-tied in two of three; worst extension gap is V9b −1.3 bb/100 on adaptive.
- **Overlays were often pure no-ops.** V8.1 ≡ V7 byte-for-byte on barrel (board filter never fired); V9a ≡ V8.1 everywhere (no perma-jammer was ever detected). The modules target archetypes that were absent from these fields, so they could only misfire.
- **When they fired, they bled.** V8.1 −55k chips on adaptive; V9b −65k on adaptive; V9c −44k on barrel. The +aggression module (V9c, anti-folder) was the most consistently negative.
- All per-field differences are within per-arm noise at n=100, but the paired-difference point estimates are exact and consistently flat-to-negative.

Conclusion: improving the *accuracy of an input* to a good uniform policy (what V7 did) is robust. *Conditionally overriding* that policy on a noisy classifier read (what V8/V9 did) is a bet that needs the read right and the target archetype present; across mixed fields the misfires arrive more reliably than the wins.

---

## Testing methodology

Backtester is launched via a Streamlit dashboard; output is per-arm aggregate JSON only. Workflow is built around that.

**Paired difference is exact, not noisy.** In AB mode all arms run on identical seeded hands, so `total_delta(candidate) − total_delta(V7)` is the *exact* paired chip difference (hands where bots agree contribute 0). The per-arm `ci95` measures within-arm bounce and is irrelevant for A/B decisions — ignore it for comparisons; use it only for absolute bb/100.

**Confidence via seed replication.** One `seed_base` = one exact paired-diff sample. Run K seed_bases (5 default; ~10 for high-variance fields like perma-jam, where divergence hands are coinflips) and take the mean/CI of the paired diffs. This resolves ~1 bb/100, which the per-arm CI cannot.

**Batch candidates into one job.** Put V7 + all candidate variants in a single AB job per field per seed_base. Run count is `seeds × fields`, independent of candidate count. (Reuses the working 6-way AB setup.)

**Frozen panel.** A fixed set of fields, never changed mid-development:
- *Robustness tier:* barrel, polar3bet, adaptive, plus archetype fields (folder, station, perma-jam).
- *Leak-finding tier:* one isolation preset per opponent (find per-opponent leaks the aggregates hide).

**Promote/reject gate — select on the floor, not the average.** A change promotes only if its paired-diff is positive with CI excluding 0 in the target field, **and** its paired-diff CI lower bound stays above a small negative tolerance in every other field. A +2/−1.5 profile is rejected even with positive mean — that asymmetry is exactly the V8/V9 failure.

**One change per candidate.** Never bundle two ideas; batched arms isolate for free anyway.

---

## Known leaks and open items

**Squeezer leak (priority).** In the barrel field, per-opponent eval decomposition shows the squeezer is the *only* opponent net-positive against V7 (+4.18 bb/100; everything else loses, balanced_lag −12.9). Diagnosis: V7 opens wide per chart, then defends 3-bets with the tight `_VS_3BET` table and over-folds, donating opens + dead money to a wide re-raiser. Caveat: at one seed the +4.18 CI brushes zero (per-match 2090 ± ~2520) — confirm with seed replication + `rotate_seats: true` before acting. Fix direction is **uniform-policy** (defend 3-bets less foldy / more 4-bet bluffs from squeeze-prone seats, and/or tighten those opens), not a conditional anti-squeezer module.

**Exploit modules have no confirmed use yet.** Archetype eval fields (single seed, rotation off) show V7 already handles the targeted archetypes without help: folder field +26.3 bb/100 (0.72 win); station +19.4 bb/100 with near-zero variance (stdev 4243). The exploit question — do V9a/b/c beat V7 *in their own best-case field* — is unresolved and requires AB runs with the candidate arms before any module is reconsidered. Default expectation: delete them unless they clear the floor gate.

**Scoring objective unresolved (blocks anti-perma).** Perma-jam field is high variance (field stdev ~30k; V7 +40.6 bb/100 at only 0.51 win rate). Calling wider vs jammers raises chip-EV but lowers match win rate. Whether anti-perma is even desirable depends on whether the competition scores cumulative chips or match/tournament placement. Resolve this before testing V9a.

**Seat rotation.** Eval-mode absolute numbers used `rotate_seats: false`; per-seat spreads (e.g. identical bots at −8.6 vs −0.2) confirm positional contamination. Harmless in paired AB (cancels), but turn rotation on for any absolute eval comparison.

---

## Roadmap

1. Confirm the squeezer leak (isolation preset, K seeds, rotation on) and size it.
2. 3-bet-defense recalibration candidates, AB on barrel, gated on: up vs squeezer, flat-or-better vs balanced_lag / multi_barrel.
3. Coarse coordinate sweep of postflop equity thresholds (0.72 / 0.55 / 0.38) and sizing fractions — they were tuned against the old random-equity estimate and are likely miscalibrated under V7's more accurate equity.
4. Resolve scoring objective; only then run the exploit modules AB in their archetype fields.
5. Build the JSON-folder aggregation layer (per-(candidate, field) paired-diff mean/CI across seeds + gate verdict).

**Submission rule:** submit the strongest version only if it improves EV without increasing catastrophic downside. **Do not break the baseline.** Select on the worst-field floor, not the mean.
