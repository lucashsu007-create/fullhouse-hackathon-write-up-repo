# Poker Bot — Technical Report

**Current frozen baseline: V7-M2 (fix)** — `v7_m2` with the 4-bet-defense gate bug patched.
Built for the Fullhouse Hackathon, qualifier 1 June 2026 (deadline 31 May 23:59 UTC), finals 5 June.

V7 → V7-M2 promoted (postflop marginal-cutoff tightening). V7.2 (street-narrowing) and V10 (squeezer-defense) families evaluated and rejected. The single largest gain in the project came from fixing one inverted boolean in `_preflop_history` that had rendered `_four_bet_branch` dead code — see [The dead-gate fix](#the-dead-gate-fix).

---

## Version lineage

| Version | Main change | Scope | Status |
|---|---|---|---|
| V4.1 | SafeTAG baseline; classifier reads collected but unused | Baseline | Superseded |
| V6 | Per-position GTO-Wizard preflop charts, opens, 3-bet/4-bet defense, jam ranges | Preflop | Superseded |
| V7 | Range-aware postflop equity (`equity_vs_range`) replacing random-hand equity | Postflop equity | Superseded |
| V8.1 | Board-conditioned NUTTED range on coordinated boards | Postflop range | Rejected |
| V9 / V9a–c | Opponent-conditional exploit modules | Exploits | Rejected |
| V7-M2 | Marginal-call cutoff 0.38 → 0.42 (postflop tighten) | Postflop equity | Promoted (~+1.5 bb/100) |
| V7.2 / a00–a100 | Street-narrowing of WIDE/OPEN villain pools across streets | Postflop range | Rejected (panel had no targets) |
| V10 / sqz_{t,m,w,4b} | Wider `_VS_3BET` tables + 4-bet bluffs | Preflop defense | Rejected (after fix) |
| **V7-M2 (fix)** | **`first_raiser_was_us` — corrects inverted 4-bet-defense gate** | **Preflop logic** | **Frozen baseline** |

Validated absolute performance of V7-M2 (fix) against the panel: barrel +36.7 bb/100, polar3bet +27.9, adaptive +34.2 — mean +32.9 across the three fields. Roughly +9 bb/100 above pre-fix V7 on the same panel; approximately +8 of that is attributable to the dead-gate fix alone and +1.5 to the V7→V7-M2 cutoff change.

---

## The dead-gate fix

The most consequential change in the project came from finding that `_four_bet_branch` — the only function that reads `_VS_3BET` — was never being called in 100bb play. The gate at the call site was:

```python
if history["last_raiser_was_us"]:
    return _four_bet_branch(state, hand, rng)
```

`last_raiser_was_us` is True only when the *most recent* preflop raiser is the hero. In every scenario where we want 4-bet defense — we opened, someone re-raised us — the most recent raiser is *them*, not us. The flag was always False at the gate, `_four_bet_branch` was never reached, and the bot folded 100% of all hands (including AA) to any 3-bet via the `_d(0, 0)` fallback inside `_four_bet_distribution`.

The fix: add `first_raiser_was_us` to `_preflop_history`, set it inside the `if info["first_raiser_seat"] is None:` block, and gate `_four_bet_branch` on that flag instead. Three-line patch.

**Why it was missed.** Every visible interface looked correct. `_preflop_history` returned a dict with all the right keys. `_VS_3BET` was a populated lookup table. `_four_bet_distribution` did the right call. The bug was one inverted boolean at the *call site*, not in any of the data-flow surfaces a casual reader would inspect.

**How it surfaced.** Three rounds of V10 AB testing (v10.1–v10.3) produced byte-identical chip outcomes across all five arms despite the variant files having genuinely different `_VS_3BET` tables. After ruling out a framework loader bug, a deployment bug, and an in-process module-cache bug, the only mathematically consistent explanation was that `_VS_3BET` was not being read in any of the ~40,000 hands per panel. Direct simulation of `_preflop_history` on a canonical "hero opened, SB 3-bet" state confirmed it.

**Audit hygiene.** Every read of `last_raiser_*` or `first_raiser_*` elsewhere in the bot should be reviewed for the same inversion. The audit cost is one `grep`.

---

## What V7-M2 (fix) is

V7-M2 (fix) is a tight-aggressive bot with three layers:

- **Preflop (V6 charts):** per-position RFI charts (UTG/HJ/CO/BTN/SB), per-seat open sizes (2.1–3.5bb), opener-tier × defender-role 3-bet defense, dedicated 4-bet defense table **now actually reached** via the corrected `first_raiser_was_us` gate, sub-20bb jam ranges, heads-up SB override.
- **Postflop (V7-M2):** Monte Carlo equity via `eval7`, sampling each live opponent from a **range bucket** (`WIDE / OPEN / THREEBET / NUTTED / UNKNOWN`) inferred from their action log. A passive-rocket override upgrades a stats-confirmed passive villain to `NUTTED` when they raise. Equity feeds a fixed 4-bucket decision tree; marginal-call cutoff at 0.42 (tightened from V7's 0.38).
- **Stats/classifier:** RAM-only opponent stats and behavior classifiers are populated but **collection-only** — they do not drive actions.

Robustness property unchanged from V7: no opponent-model-dependent branch in the action path. Range estimation only changes the equity *number*; it never swaps the policy. Equity engine degrades gracefully (`equity_vs_range` → `equity_vs_random` on any failure).

---

## Why V8/V9 were rejected

V8 (board-conditioned NUTTED) and V9 (three opponent-conditional exploit modules) are **conditional overlays** on V7: each only changes a decision when a narrow trigger fires, otherwise falling through to V7. This gives them capped upside and open-ended downside — a false-positive trigger replaces a good baseline decision with a regime-specific one.

Paired A/B (shared spot seeds), 100 matches × 500 hands per field:

| field | V7 | V8.1 | V9 (all) | V9a perma | V9b station | V9c folder |
|---|---|---|---|---|---|---|
| barrel | **30.49** | 30.49 | 29.76 | 30.49 | 30.65 | 29.60 |
| polar3bet | 16.61 | 16.87 | **17.12** | 16.87 | 16.87 | 17.12 |
| adaptive | **23.33** | 22.23 | 22.31 | 22.23 | 22.04 | 22.52 |

(bb/100, pre-fix V7 numbers). Findings:

- **No extension beats V7 by a meaningful margin in any field.** V7 was best-or-tied in two of three; worst extension gap was V9b −1.3 bb/100 on adaptive.
- **Overlays were often pure no-ops.** V8.1 ≡ V7 byte-for-byte on barrel; V9a ≡ V8.1 everywhere. The modules target archetypes absent from these fields.
- **When they fired, they bled.** V8.1 −55k chips on adaptive; V9b −65k on adaptive; V9c −44k on barrel.

Improving the *accuracy of an input* to a uniform policy (what V7 did) is robust. *Conditionally overriding* that policy on a noisy classifier read (what V8/V9 did) is a bet that needs the read right and the target archetype present.

---

## Why V7.2 (street-narrowing) was rejected

V7.2 narrowed the WIDE / OPEN villain pools on later streets when the villain had postflop calls in history: air dropped on any postflop call, river dead-draw filter for weak draws. Four variants by air-keep rate (a00 / a25 / a50 / a100) tested across the panel.

All four were negative-to-flat: a100 −0.18, a00 −0.19, a50 −0.29, a25 −0.30 mean bb/100 vs V7-M2, with floors −0.9 to −1.9. Cause: the panel — barrel (aggressive bots), polar3bet (3-bettors), adaptive_test (TAGs/LAGs) — has no passive limp-callers, exactly the archetype street-narrowing is designed to defend against. The lever fires against a population not in the test set. Same structural failure mode as V8/V9.

---

## Why V10 (squeezer-defense) was rejected

After the dead-gate fix, the V10 family — four variants widening `_VS_3BET` (sqz_t 25 hands, sqz_m 39, sqz_w 46) plus a 4-bet-bluff variant (sqz_4b 44 hands) — was re-tested against patched V7-M2. None beat the patched baseline.

Pairwise paired diffs averaged across all three fields (5 seed bases × 100 matches × 500 hands):

```
                  v7_m2   sqz_t   sqz_m   sqz_w   sqz_4b
v7_m2 row           --   +0.40   +0.20   +1.02   +0.43
```

Per-field floor (worst-field mean bb/100 vs V7-M2): sqz_t −0.54, sqz_m −0.97, sqz_w −1.41, sqz_4b −1.20. Every variant has a negative floor. CIs straddle zero in each field individually, but the floor rule rejects everything.

Findings:

1. V7's original 19-hand `_VS_3BET` is well-calibrated for 6-max at 100bb stacks; the apparent tightness was not a leak.
2. **Calling wider against 3-bets is a leak, not a fix.** Reaching more OOP postflop spots with weak ranges loses more than rare call-and-win recovers. The widest variant (sqz_w) lost the most.
3. **4-bet bluffs (sqz_4b) don't help.** Calling stations in the 6-max field undercut the fold-equity premise.

The previous "+4.18 bb/100 squeezer leak" estimate in this README was measured against a bot that folded AA to 3-bets due to the dead gate. The leak it described was real but mis-diagnosed: not a calibration issue, a bug. Fixing the bug closed it.

---

## Testing methodology

Backtester is launched via a Streamlit dashboard; output is per-arm aggregate JSON only. Workflow is built around that.

**Paired difference is exact, not noisy.** In AB mode all arms run on identical seeded hands, so `total_delta(candidate) − total_delta(baseline)` is the *exact* paired chip difference (hands where bots agree contribute 0). The per-arm `ci95` measures within-arm bounce and is irrelevant for A/B decisions — ignore it for comparisons; use it only for absolute bb/100.

**Confidence via seed replication.** One `seed_base` = one exact paired-diff sample. Run K seed_bases (5 default; ~10 for high-variance fields) and take the mean/CI of the paired diffs. This resolves ~1 bb/100, which the per-arm CI cannot.

**Batch candidates into one job.** Put baseline + all candidate variants in a single AB job per field per seed_base. Run count is `seeds × fields`, independent of candidate count.

**Frozen panel.** A fixed set of fields, never changed mid-development:
- *Robustness tier:* barrel, polar3bet, adaptive_test, plus archetype fields (folder, station, perma-jam).
- *Leak-finding tier:* one isolation preset per opponent.

**Promote/reject gate — select on the floor, not the average.** A change promotes only if its paired-diff CI lower bound stays above a small negative tolerance in *every* field. A +2/−1.5 profile is rejected even with positive mean — that asymmetry is exactly the V8/V9 failure.

**One change per candidate.** Never bundle two ideas; batched arms isolate for free anyway.

**Identity probes at load time.** `load_decide` prints one stderr line per unique `(path, BOT_NAME)` pair. This surfaces deployment-level "same file under multiple paths" failure modes at load time rather than after the run completes. Added after the v10.1–v10.3 episode where the framework appeared buggy until the probe forced the search into the bot's own logic.

---

## Known leaks and open items

**Squeezer leak — RESOLVED.** The previous +4.18 bb/100 estimate was measured pre-fix when the bot folded AA to 3-bets. Post-fix, the leak is plugged; further `_VS_3BET` tuning is neutral-to-negative (see V10 rejection above).

**Hidden postflop leaks from newly-reached states.** Because the bot now actually calls some 3-bets it previously always folded, it reaches OOP postflop spots that the equity-based postflop logic was never tuned against. Worth a targeted eval on barrel/polar3bet to see whether postflop play in 3-bet pots is leaking. Unmeasured.

**Sibling bugs in `_preflop_history`.** Audit pending. Same inverted-gate pattern could exist elsewhere. 30-second grep; potentially several bb/100 of recoverable EV.

**`_defense_branch` is now the dominant preflop lever.** With `_four_bet_branch` alive and showing that wider defense is worse, the open question is whether `_defense_branch` (cold-calls vs opens, multi-way pots) is correctly calibrated. Unexamined.

**Sizing menu.** Bot uses one size per spot. Multi-size policy conditioned on board texture is plausibly +2 to +4 bb/100 against opponents who don't adapt. Deferred to patch window.

**Scoring objective.** Qualifier uses cumulative chip delta over Swiss rounds (favors chip-EV maximization). Finals is single-elimination bracket (variance-bounded play is better). Current bot leans chip-EV — correct for D1, possibly wrong for D5.

---

## Competition roadmap

**D1 ship (May 31 deadline): V7-M2 (fix) as-is.** No further extension work pre-deadline. The dead-gate fix is the single biggest extension win in the project; shipping anything experimental on top risks regression for marginal expected gain.

Pre-deadline checklist:
1. Audit `grep` for other `last_raiser_*` / `first_raiser_*` reads. Fix any inversions and re-AB.
2. Save `[load_decide] BOT_NAME=...` stderr from a final AB run as deployment verification.
3. Ship.

**Patch window (June 2–4):** This is the higher-EV sprint than anything available pre-deadline, because the opponent field becomes concrete (hand histories from D1 land June 2).
1. Build hand-history parser ready for the June 2 JSON drop.
2. Per-opponent leak detection on D1 logs — identify likely D5 opponents and their archetypes.
3. Then run the experiments that don't pay off in the dark: sizing menu, position/stats-conditional 3-bet defense, mixed-strategy postflop bluff frequencies. These were lower-EV pre-deadline because the target field was unknown; in the patch window the field is concrete.

**Finals (June 5):** single-elim bracket. Even with the best bot in the field, ~3–5% trophy probability after variance. The bot is the ticket; the rest is the lottery.

---

**Submission rule:** ship the version with the highest worst-field floor, not the highest mean. V7-M2 (fix) clears that bar; no other variant in the project does.
