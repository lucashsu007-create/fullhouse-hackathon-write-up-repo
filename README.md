# Poker Bot — Technical Report

**Current frozen baseline: V7-M2 (fix)** — `v7_m2` with the 4-bet-defense gate bug patched.
Built for the Fullhouse Hackathon, qualifier 1 June 2026 (deadline 31 May 23:59 UTC), finals 5 June.

> **Update — 30 May 2026 (V16 wave).** A third wave corrected a latent **equity-engine bug** (the Monte-Carlo denominator counted dead/conflicted trials, deflating equity) plus seven other foundation fixes, and tested four flag-gated experimental changes. In paired AB the **V16 foundation beats V7-M2 (fix) by +3.8 to +4.2 bb/100 on the mean** — the largest, most consistent gain in the project from a pure input/correctness fix — **but it does not yet clear the floor rule**: it has negative floors in 3–4 high-variance fields that are unresolved at 3 seeds. Of the four flags, `action_filter` bled outright (the V7.2 failure re-confirmed), `spr` and `blocker_bluff` were inert, and `ev_call` is +mean/−floor. **The D1 ship decision is unchanged: V7-M2 (fix).** The V16 foundation is the leading patch-window promote *if* its floor resolves at ≥10 seeds. See [The equity-denominator fix](#the-equity-denominator-fix) and [V16 — foundation and flag evaluation](#v16--foundation-and-flag-evaluation).

V7 → V7-M2 promoted (postflop marginal-cutoff tightening). V7.2 (street-narrowing) and V10 (squeezer-defense) families evaluated and rejected. The single largest gain in the project came from fixing one inverted boolean in `_preflop_history` that had rendered `_four_bet_branch` dead code — see [The dead-gate fix](#the-dead-gate-fix). The second-largest came from fixing the equity-MC denominator (V16) — same shape: a latent bug on an interface that looked correct, not a calibration issue.

A second wave of work (V11–V14) extended the search into opponent-conditional exploits, range-construction refinements, balance/sizing fixes, and a complete postflop **leak audit**, alongside two **absolute validation runs** against a static reference field and a hyper-aggressive pressure field, and the construction of the **patch-window toolchain**. Every V11–V14 family was rejected or deferred for the same structural reason the earlier overlays were. The validation runs confirm V7-M2 (fix) is robust across the full style spectrum. The toolchain is built and waiting for the June 2 hand-history drop. A third wave (V16) corrected the equity-MC denominator bug, bundled it with seven other foundation fixes, and tested four flag-gated changes — strong +mean, floor not yet confirmed (below). **None of this changes the D1 ship decision: V7-M2 (fix) as-is.**

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
| V11 / v11.5–v11.12 | Read/policy exploit engine: overfold/station/bet-punisher reads driving thin-value + multi-street bluff policies, plus defensive flips | Exploits | Rejected (false-positive bleed) |
| V12 / cbet, cont | Hard-coded c-bet bluffs (balance) + continuous postflop policy | Postflop policy | Rejected (wash / barrel regression) |
| V13 / narrow, width, lag | Range-construction refinements: continuation narrowing, PFR-scaled opens, LAG range-widening | Postflop range/equity | Deferred to D1 (eyeballed widths) |
| V14 / sizing, trap, river, multiway | Leak fixes: decoupled sizing, uncapped checks, river value polarization, multiway tightening | Postflop policy | Built, scope-verified, **EV-untested** |
| **V16 (foundation)** | **Equity-MC denominator fix (#2) + board-aware ranges (#1/#6) + position-split opens (#5) + cold-4bet defense (#3) + thin value raises (#4) + jam/sanitize safety fixes (#7/#8)** | Postflop equity + preflop | **Candidate — +3.8–4.2 mean, floor unresolved at 3 seeds (not promoted)** |
| V16 flags / ev_call, spr, action_filter, blocker_bluff | Flag-gated experimental changes, default-off (V14 pattern); tested as `v16`/`v16.1`/`v16.5` (same foundation logic, names only) | Postflop policy/range | `action_filter` rejected (−2.7); `spr`/`blocker_bluff` inert; `ev_call` +mean/−floor (tuning pending) |

The V11–V14 families build on the same postflop/equity/preflop baseline and are **orthogonal to the dead-gate fix** (a preflop-logic correction that touches neither the equity path nor the exploit overlays), so they layer cleanly on V7-M2 (fix). The V16 foundation is *not* orthogonal — it touches the equity engine (the denominator fix) and the action path (cold-4bet, thin value), so it must be floor-tested as a unit before promotion.

Validated absolute performance of V7-M2 (fix) against the development panel: barrel +36.7 bb/100, polar3bet +27.9, adaptive +34.2 — mean +32.9 across the three fields. Roughly +9 bb/100 above pre-fix V7 on the same panel; approximately +8 of that is attributable to the dead-gate fix alone and +1.5 to the V7→V7-M2 cutoff change. Additional absolute validation on two new fields appears in [Absolute validation](#absolute-validation-reference-and-pressure-fields). The V16 foundation has **not** had an eval-mode absolute run, a `harden` pass, or a timing audit — its only evidence is paired AB vs V7-M2 (fix) (below).

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

## The equity-denominator fix

The second-largest correctness fix in the project, found in the V16 wave. `_equity_vs_multi_range` — the Monte-Carlo loop behind `equity_vs_range`, used for every range-aware postflop equity — incremented a **single counter on failed trials too.** A "failed" trial is one that never produced a showdown: a card-conflict when sampling opponent combos from a tight range, or a dead board fill. Those trials were counted in the denominator alongside the successful sims, so:

```
equity_reported = (wins + 0.5·ties) / (successful + failed)     # WRONG: failed in denominator
```

The result is **systematically deflated equity**, worst exactly where failed trials are most common: tight ranges (NUTTED/THREEBET, where many sampled combos conflict with hero/board cards) and multiway pots (more opponents → more conflicts). A deflated equity number feeds the uniform decision tree, so the bot played **too tight** — over-folding marginal spots, under-value-betting, under-committing — across the whole postflop game, in proportion to how narrow the modelled ranges were.

The fix separates the loop budget from the showdown count:

```python
attempts = trials = wins = ties = 0
while attempts < max_iters:
    attempts += 1
    ... # sample opponents + board; on any conflict/dead fill: `continue` (NOT counted)
    trials += 1
    ... # tally wins/ties
return (wins + 0.5 * ties) / trials      # denominator = successful showdowns only
```

The time-guard fires only after a meaningful number of *successful* trials (or after exhausting `max_iters`), so a heavily-conflicted range can't loop forever yet a normal spot still reaches its sample target.

**Why it matters and why it's the "right kind" of fix.** This is an *input* error to a uniform policy — the robust class to correct (the project's recurring lesson: improving the input is robust; overriding the policy on a read bleeds). Unlike the dead gate (a preflop-logic bug), this one silently mis-valued every postflop hand. The entire V7-M2 postflop validation predates it, so V7-M2's absolute numbers were produced on a downward-biased equity engine. The fix is the load-bearing component of the V16 foundation; the other seven V16 fixes are smaller or field-specific (below).

**How it surfaced.** A code review of the range-aware engine flagged the mismatch between the engine's sophistication and the looseness/bias of what fed it — the denominator bug being the most severe item. Confirmed by direct calls: a tight range on a hero-blocked board (e.g. hero set on a paired board) returned a sane high equity after the fix where the old denominator would have deflated it.

**Status.** Folded into the V16 foundation and verified by differential/scope tests; chip EV is the V16 foundation AB result below. Because it is a pure correctness fix independent of opponent type, it is the cleanest single candidate to promote on its own merit — see the decomposition note in [Competition roadmap](#competition-roadmap).

---

## What V7-M2 (fix) is

V7-M2 (fix) is a tight-aggressive bot with three layers:

- **Preflop (V6 charts):** per-position RFI charts (UTG/HJ/CO/BTN/SB), per-seat open sizes (2.1–3.5bb), opener-tier × defender-role 3-bet defense, dedicated 4-bet defense table **now actually reached** via the corrected `first_raiser_was_us` gate, sub-20bb jam ranges, heads-up SB override. Preflop ranges and 3-bet/4-bet decisions are **frequency-mixed** from the charts (`_sample_dist`), and 3-bet/4-bet sizing scales with the opener's size — so the preflop tree is *not* face-up and carries no sizing tell.
- **Postflop (V7-M2):** Monte Carlo equity via `eval7`, sampling each live opponent from a **range bucket** (`WIDE / OPEN / THREEBET / NUTTED / UNKNOWN`) inferred from their action log. A passive-rocket override upgrades a stats-confirmed passive villain to `NUTTED` when they raise. Equity feeds a fixed 4-bucket decision tree; marginal-call cutoff at 0.42 (tightened from V7's 0.38). **Note (V16):** the MC denominator this engine used was downward-biased — see [The equity-denominator fix](#the-equity-denominator-fix).
- **Stats/classifier:** RAM-only opponent stats and behavior classifiers are populated but **collection-only** — they do not drive actions.

Robustness property unchanged from V7: no opponent-model-dependent branch in the action path. Range estimation only changes the equity *number*; it never swaps the policy. Equity engine degrades gracefully (`equity_vs_range` → `equity_vs_random` on any failure). The action path is wrapped so a malformed state or eval7 failure emergency-folds rather than crashing — verified (see [Safety](#safety-and-validation)).

---

## Absolute validation: reference and pressure fields

Two `eval`-mode runs (free-for-all, not paired AB) measured V7-M2 (fix)'s absolute standing against fields outside the development panel. Both are clean: zero errors, zero timeouts, deltas sum to exactly zero.

**Static reference field** (the repo's five reference bots: shark, mathematician, refbot2, aggressor, template), 100 matches × 1000 hands, 6-handed:

| bot | bb/100 | CI95 | win% | avg place |
|---|---|---|---|---|
| **V7-M2** | **+28.79** | [+24.9, +32.6] | **80%** | **1.55** |
| shark | −6.05 | ±2.33 | 10% | 3.31 |
| aggressor | −5.00 | ±2.95 | 10% | 3.48 |
| mathematician | −8.75 | ±0.75 | 4% | 3.31 |
| refbot2 | −8.98 | ±0.62 | 2% | 3.35 |

V7-M2 wins 80% of matches and beats `shark` — the only competent TAG in the field, the closest proxy to a real submission — by ~35 bb/100.

**Hyper-aggressive pressure field** (3× pressure_bot + multi_barrel + balanced_lag; `pressure_bot` is a purpose-built strong adaptive polarized bluffer — see [Toolchain](#patch-window-toolchain)), 250 matches × 1000 hands, 6-handed:

| bot | bb/100 | CI95 | win% |
|---|---|---|---|
| **V7-M2** | **+17.87** | [+14.2, +21.5] | **47%** |
| pressure_bot | −2.26 | [−4.8, +0.2] | 13% |
| balanced_lag | −3.04 | [−5.4, −0.7] | 12% |
| pressure_bot_2 | −4.00 | [−6.2, −1.8] | 10% |
| pressure_bot_3 | −4.24 | [−6.4, −2.0] | 10% |
| multi_barrel | −4.33 | [−6.5, −2.2] | 10% |

V7-M2 is the only profitable bot at the table. Its placement is bimodal — **1st (118×) or 4th (132×), almost never worse** — and its per-match outcome is capped at −10,000 worst case while it stacks the table (+40k) in 113 of 250 matches. That right-skewed, downside-bounded variance is exactly the profile a top-64 cut rewards.

**Interpretation.** Together these bracket the strategy space: V7-M2 crushes both the **passive/static** pole and the **hyper-aggressive** pole. The aggressive result is the more important one, because it directly answers the imbalance objection (see [Strategic analysis](#strategic-analysis-why-exploitation-doesnt-pay-here)): a strong bluffer that folds to V7-M2's value bets nonetheless bleeds, because its relentless bluffing runs into V7-M2's accurate, range-aware calling. V7-M2 does not need a bluffing range to beat aggression — it needs to call correctly and not get bluffed off hands, which is what the equity engine plus pot-odds discipline plus the passive-rocket override do. **Caveat:** these margins are against deliberately-flawed bots and are not a forecast of qualifier margin, which will compress against a varied field. They are evidence of *robustness and soundness*, not of edge size. **Caveat 2 (V16):** these absolute numbers were measured on the pre-fix (downward-biased) equity engine; the V16 denominator fix changes the equity numbers, so V7-M2's absolute standing would shift somewhat under V16 (direction: the bot value-bets/commits a little more). The V16 foundation has not been re-run in eval mode.

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

Improving the *accuracy of an input* to a uniform policy (what V7 did) is robust. *Conditionally overriding* that policy on a noisy classifier read (what V8/V9 did) is a bet that needs the read right and the target archetype present. **V11 is the most rigorous re-demonstration of this finding** (below); **V16's `action_filter` is the latest** (it bled exactly like V7.2).

---

## Why V7.2 (street-narrowing) was rejected

V7.2 narrowed the WIDE / OPEN villain pools on later streets when the villain had postflop calls in history: air dropped on any postflop call, river dead-draw filter for weak draws. Four variants by air-keep rate (a00 / a25 / a50 / a100) tested across the panel.

All four were negative-to-flat: a100 −0.18, a00 −0.19, a50 −0.29, a25 −0.30 mean bb/100 vs V7-M2, with floors −0.9 to −1.9. Cause: the panel — barrel (aggressive bots), polar3bet (3-bettors), adaptive_test (TAGs/LAGs) — has no passive limp-callers, exactly the archetype street-narrowing is designed to defend against. The lever fires against a population not in the test set. Same structural failure mode as V8/V9. (V13's continuation narrowing is the same lever, re-derived independently and re-deferred to D1 for the same reason — below. **V16's `action_filter` is the same lever a third time, with stricter eyeballed widths, and bled hardest of all — see [V16](#v16--foundation-and-flag-evaluation).**)

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
2. **Calling wider against 3-bets is a leak, not a fix.** Reaching more OOP postflop spots with weak ranges loses more than rare call-and-win recovers. The widest variant (sqz_w) lost the most. *(V16 note: the V16 foundation's cold-4bet branch (#3) introduces the mirror risk — calling/4-betting some 3-bets V7-M2 always folded reaches new OOP spots; this is the leading suspect for V16's negative floors, below.)*
3. **4-bet bluffs (sqz_4b) don't help.** Calling stations in the 6-max field undercut the fold-equity premise.

The previous "+4.18 bb/100 squeezer leak" estimate in this README was measured against a bot that folded AA to 3-bets due to the dead gate. The leak it described was real but mis-diagnosed: not a calibration issue, a bug. Fixing the bug closed it.

---

## Why V11 (read-driven exploit) was rejected

V11 is the most serious exploit attempt in the project, and its rejection is the most rigorous confirmation of the V8/V9 thesis. It replaced the dead collection-only classifier with a live **read → policy** engine:

- **Reads** (Beta posteriors with empirical-Bayes shrinkage, confidence-gated): `overfold_read` fires when a villain's estimated fold-to-bet rate confidently exceeds the bluff break-even for the size in use (`breakeven_fold(s) = s/(1+s)`); `station_read` fires on confirmed over-callers; `bet_punisher_read` fires on confirmed check-raisers (elevated `raise_vs_bet`).
- **Policies**: `thin_value_vs_station` (size up vs stations) and `bluff_vs_overfolder` (multi-street, edge-scaled, depth-damped). Heads-up only; a salted RNG keeps the baseline stream pristine when the exploit declines.
- **v11.5** integrated this into V7-M2 with per-street fold counters.

**The trap_field bleed.** Paired AB on trap_field (check-raisers/trappers): of 3,800 match-instances, 3,446 were byte-identical to baseline (exploit declined) and 354 differed (exploit fired, ~9.3%). Net: **v11.5 lost 4,262,908 chips** to V7-M2 (~−1 bb/100 overall, ~−12 bb/100 among firing matches; 215 firing matches lost vs 139 won). Root cause exactly as the theory predicts: `fold_vs_bet` labels a check-raiser an "overfolder" (they *do* fold a lot to bets), but the times they continue they *raise*, and the bluff walks into it. Adverse selection — the read is correct on average and wrong on the hands that matter.

**The gate fixed the symptom and the disease remained.** v11.6 added a `bet_punisher` abort (don't bluff a villain who raises bets often). Scope-verified: 4,700 of 300k synthetic cases differed, all of them "bluff → check/fold." It took trap_field to a clean **0.00** (the exploit now correctly declines into raisers). But the broader test exposed the real problem — across the panel (300 matches/cell, 3 seeds):

| field | v11.6 vs V7-M2 | sig |
|---|---|---|
| adaptive_test (balanced floor) | −2.03 bb/100 | *** |
| trap_field | 0.00 | — |
| broad_mix | −1.62 | ** |
| barrel_field | −1.74 | *** |
| polar3bet_field | −0.56 | ** |

The exploit **loses, significantly, on every field except the one it learned to avoid.** adaptive_test contains *zero* exploitable opponents, yet the reads fire ~9% of the time and lose: every firing is a **false positive** — a confidence-gated read crossing its trigger against a competent opponent over 1000 hands, then betting into someone who punishes it. This is the original statistical prediction ("clean separation is false at n≈25; gto fires ~10%") confirmed in chips. The defensive variants (v11.10 fold-vs-traps, v11.11 call-vs-bluffers, v11.12) were null — the fold-flip never fired (byte-identical to v11.6 across all fields), the call-flip fired in single-digit matches and netted ~zero. **Rejected.** Read-driven exploitation has an irreducible false-positive rate at any usable threshold; in a mostly-competent field the cost of false positives swamps the gains from rare real targets.

---

## Why V12 (c-bet balance / continuous policy) was rejected

V12 attacked the "value-only / face-up betting" property directly.

- **c-bet (balance):** add a hard-coded flop c-bet bluff as the preflop raiser, heads-up, on dry boards (range-bet) and as a semi-bluff on wet boards, gated off vs confirmed check-raisers — a board-and-line trigger (not a read trigger), with the opponent signal only as a one-sided off-switch. **Result: a wash** (−0.13 bb/100 pooled, ns; no significant cell on any field). Safe (no bleed, gate worked) but inert.
- **continuous policy:** replace the discrete equity bands with a smooth bet-frequency/size curve and a monotonic call rule (which also fixes the 0.55-seam non-monotonicity where eq 0.54 calls but 0.56 folds). **Result: significant regression on barrel** (−2.35 bb/100, the only `**` cell), mixed elsewhere. The smoothing bets medium hands OOP more and the looser call cushion pays off barrelers. Rejected; the combined `cbet_cont` inherited the regression.

The c-bet wash is itself the measurement of how much the imbalance costs: ≈ zero against these fields, because they are call-heavy (stations punish bluffs, not value bets), so value-only is near-optimal and adding bluffs is counterproductive. See [Strategic analysis](#strategic-analysis-why-exploitation-doesnt-pay-here). *(V16 note: V16's `ev_call` flag re-attacks the 0.55-seam monotonicity, but on the calling side only — not the bet curve that sank the continuous policy — and it shows a positive mean with one polar3bet regression; the `blocker_bluff` flag is bluff-selection on the existing rare steal only, and was inert, consistent with the c-bet wash.)*

---

## Why V13 (range construction) was deferred to D1

V13 is the "right kind" of change — it improves the *equity input* to the uniform policy rather than overriding the policy — so it carries no false-positive override risk. Three flag-gated refinements to `_range_for_villain`:

- **narrow:** tighten a limp/call villain's range as they peel postflop bets (WIDE → CONTINUE → CONTINUE_TIGHT). Directly targets the river-overcall leak (#4).
- **width:** scale an opener's range by their measured PFR (loose → wider, tight → tighter).
- **lag:** widen a *confirmed* aggressor's range (the mirror of the passive-rocket override) so the bot stops overfolding to bluff-heavy 3-bets.

Paired across 5 fields, 3 seeds (n≈1900 pooled), Bonferroni over 20 per-field tests (α=0.0025):

| variant | pooled bb/100 | notable per-field |
|---|---|---|
| narrow | −0.13 (tight CI) | inert everywhere — CONTINUE width too close to effective range |
| width | +0.32 (ns) | leans positive, **no negative field** — underpowered, possibly real |
| lag | +0.19 (ns) | **+1.43 broad / −1.56 polar** — direction right, magnitude wrong |
| all | +0.35 (ns) | inherits lag's variance |

**No Bonferroni-significant cell.** The one raw-0.05 cell (lag on polar3bet, −1.56) is a *regression* and is the expected ~1 false positive over 20 tests. The lag broad-vs-polar split is the signature of a **magnitude error in eyeballed ranges**, not a logic error: widening is correct vs a true maniac (broad has multi_barrel) but the polar 3-bettors 3-bet tighter than the eyeballed `THREEBET_WIDE` assumes. **Deferred, not killed:** the control logic is built and scope-verified; the range *strings* are the eyeballed part, and D1 showdowns are the ground truth to fit them. `width` is the most promising survivor (positive lean, no downside). This is the highest-leverage range work for the patch window. **(V16 note: V16's `action_filter` is `narrow` re-implemented with per-street/per-action modes and stricter eyeballed widths — and it bled −2.7 bb/100, confirming the deferral was correct and that these widths must be calibrated on real showdowns, not hand-set.)**

---

## V14 (leak fixes) — built, scope-verified, EV-untested

Four flag-gated fixes to `_postflop_by_equity`, each isolated (one leak per flag), all off by default = byte-identical to V7-M2 (verified: 0 diffs over a 1440-cell grid). Each was confirmed to change *only* its own dimension:

- **sizing** (#1): one bet size (0.66-pot) across the whole value range — kills the sizing tell. (1440 diffs, all size-only; never changes which hands bet.)
- **trap** (#2): check strong hands 25% of the time so the checking range isn't capped; bet-into traps become check-raises through the existing strong branch. (324 diffs, all raise→check.)
- **river** (#3, value half): bigger river value sizing (0.95-pot). Deliberately does *not* add river bluffs (the washed V11/V12 territory). (414 diffs, all river, all size-only.)
- **multiway** (#5): tighten value/bet thresholds by 0.06 per extra live opponent. (297 diffs, zero heads-up.)

**Status: EV-untested.** eval7 does not run in the build sandbox, so only selection/scope is verified, not chip EV. These touch the *core* (every postflop hand) — the same blast radius that made V12's continuous policy regress — so the promote bar is the floor rule: beat the relevant probe field **and** hold the regression controls. Probe opponents and fields are built (below). Prior on outcomes: `multiway` is the likeliest clean small gain; `trap` is a field-dependent trade (gains vs aggression, costs vs passive); `sizing` will likely look inert on the panel but beat the sizing-reader probe; the value of all of them depends on whether the *real* field reads sizes / value-bets thin — a D1 question. *(The V16 flag set is the EV test of analogous levers; see below.)*

---

## V16 — foundation and flag evaluation

The V16 wave has two parts: a **foundation** (the equity-denominator fix bundled with seven other fixes) and a **flag set** (four experimental changes, default-off, V14 pattern). It was tested as the heroes `v16` / `v16.1` / `v16.5` — these are the same foundation logic, differing only in `BOT_NAME` and a comment block; the `v16.5` build carries the flag scaffolding (all off). All chip figures below are **paired AB vs V7-M2 (fix)** across the probe/control panel, 100 matches × 500 hands, 3 seed_bases, 10 fields.

### The foundation (8 fixes)

1. **`#2` equity-MC denominator** — the load-bearing fix; see [its section](#the-equity-denominator-fix).
2. **`#1` board-aware ranges** — villains who *continue* postflop are intersected with a permissive board-connection filter (drop pure air).
3. **`#6` passive-rocket postflop** — a stats-confirmed passive villain who *raises* postflop is modelled as genuinely strong made hands / big draws on this board, built from their wide preflop range (so low sets / two pair survive, not just JJ+/AK).
4. **`#5` position-split opens** — `OPEN_EARLY/MIDDLE/LATE` (~7/14/25%) replace the single flat ~22% open, keyed by the opener's seat (used for villain range modelling).
5. **`#3` cold-4bet defense** — facing an open + a 3-bet before we've invested (raises ≥2, not first raiser) routes to a tight cold-4bet-or-fold/flat branch instead of the open-defense table.
6. **`#4` thin value raises** — 0.72–0.85 equity facing a bet raises for thin value at mixed, texture-dependent frequency (a postflop *policy* change; the highest blast-radius item).
7. **`#7` `_is_facing_jam`** — based on owed/stack and live all-in state, dropping a stale "any all-in in the last 8 log entries" scan that false-positived.
8. **`#8` `_sanitize`** — an illegal check while facing a bet now folds rather than blind-calling.

Fixes #2/#1/#6/#5 are input corrections; #7/#8 are safety; #3/#4 are behavioral and carry the blast radius.

### Foundation vs V7-M2 (fix) — the result

Mean paired difference **+3.8 to +4.2 bb/100** across two batches (75×750 and 100×500, 3 seeds each). Per-field floor (worst seed), **positive in both runs** for six fields:

| field | mean (bb/100) | floor | clears floor? |
|---|---|---|---|
| polar3bet_field | +7.3 | +5.0 / +6.2 | ✓ |
| sizing_tell_field | +7–9 | +4.0 / +5.0 | ✓ |
| pressure_field | +4.5–7.1 | +2.7 / +4.3 | ✓ |
| trap_field | +4.1–5.3 | +4.0 / +4.5 | ✓ |
| broad_mix2 | +1.6–5.7 | +0.8 / +4.2 | ✓ |
| bluffing_field | +2.6–3.3 | +0.2 / +0.9 | ✓ |
| thin_value_field | +4.0–4.8 | **+0.88 → −2.65** | ✗ (flips between batches) |
| exploiter_mix | −0.3 / +0.3 | **−5.05 / −1.30** | ✗ |
| broad_mix | +1.9 / −0.2 | **−2.20 / −3.89** | ✗ |
| balanced_probe_field | +1.9–2.4 | **−4.79 / −3.89** | ✗ (and noise-contaminated — see RNG bug) |

**Verdict: strongest upgrade candidate in the project, not a floor-rule pass.** Six fields win with comfortable positive floors in both runs — a real, robust gain, and *larger and more consistent than any V8–V15 overlay ever produced* (those were ±1–2 bb/100). But four fields have negative floors, and **those floors are variance-dominated at 3 seeds**: `thin_value_field`'s floor swung **+0.88 → −2.65** between the two batches on an *unchanged bot*. So the negative floors may be real regressions or pure noise — at 3 seeds they cannot be distinguished. Promotion is blocked pending a ≥10-seed re-run on the four negative-floor fields.

**Mechanistic hypothesis for the negative floors:** the cold-4bet branch (#3) makes the bot call/4-bet some 3-bets V7-M2 always folded, reaching OOP postflop spots the equity logic was never tuned for — the "hidden postflop leaks from newly-reached states" open item, and the mirror of the V10 finding. The negative floors cluster in the *mixed* fields containing readers/aggression (exploiter_mix = sizing_reader + thin_value + pressure_bot + multi_barrel; broad_mix); the all-aggression fields (pressure_field, bluffing_field) are positive, so it is not aggression per se. The clean test is to isolate cold-4bet (decomposition note below).

### The flag set — results

The scaffolding is the V14 pattern: four flags, default-off, **byte-identical to the foundation when off — verified in the live backtester** (0 divergence across 9/10 fields × 100 matches; the only exception is `balanced_probe_field`, which diverges by the RNG-noise below, not by any flag). Each flag was then enabled in its own arm and read vs the all-off foundation:

| flag | isolated effect vs foundation (mean) | worst-field floor | fired | verdict |
|---|---|---|---|---|
| `action_filter` (#3, street/action range filters) | **−2.73** | −6.37 (broad_mix) | 1844/3000 | **Reject** — robust regression, negative in 9/10 fields |
| `ev_call` (#1, pot-odds/realization call rule) | **+1.06** | −2.56 (polar3bet) | 1457/3000 | Promising, **+mean/−floor** |
| `spr` (#2, SPR-aware commit margin) | +0.59 | −0.81 | 241/3000 | Inert on 100bb (fired ~8%; within noise) |
| `blocker_bluff` (#4, blocker-weighted steal) | +0.06 | −0.32 | 245/3000 | Inert — drop |

- **`action_filter` is rejected**, and it is the **V7.2 street-narrowing failure re-confirmed in chips** for the third time (after V7.2 and V13-narrow). It narrows villains' *continuing* ranges to strong hands, so the bot under-estimates its equity and folds/checks winners against a field of loose continuers. The decisive tell: the **only** field it doesn't hurt is `trap_field` (+0.39) — the one place where "strong when they raise" is actually true. The placeholder widths are too strict; this lever needs real showdown calibration (the V13 deferral), and even then the prior is poor.
- **`ev_call`** is the only candidate with real positive signal — positive in 7/10 (broad_mix +3.6, pressure +3.4, thin_value +2.33, bluffing +1.96), consistent with a call-heavy/aggressive field where not overfolding pays. But it has a seed-consistent **polar3bet regression (−2.56)**, so it is +X/−Y and does not clear the floor as-is. The leak is almost certainly the eyeballed constants (realization 0.94/0.96, risk premium 0.03) mishandling polar 3-bettors. Three tuning variants — size-aware premium (looser vs small bets, tighter vs big polarized bets), uniformly tighter, uniformly looser — are built to attack it, **EV-untested**, pending a ≥10-seed sweep on the gain + regression fields.
- **`spr`** fired in only ~8% of matches (commitment spots are rare at 100bb) and its +0.59 is within seed noise; inconclusive without a short-stack-heavy field. **`blocker_bluff`** is +0.06 across the board — the dry-board steal is rare and its strongest signal (the flush-blocker term) is dead on dry boards by construction. Dropped.

**Net.** None of the four flags clears the floor. `action_filter`/`blocker_bluff` are rejected, `spr` is inconclusive, `ev_call` is a tunable near-miss and the only one worth a second pass.

### What V16 confirms about the thesis

The V16 results **reinforce** the project's central finding rather than overturn it. The one change with a large, robust positive mean was a pure *input/correctness* fix (the denominator bug) — "improve the input is robust." The range-narrowing lever (`action_filter`), an input change but with eyeballed-too-strict widths, **bled exactly as V7.2 predicted**. The policy refinement (`ev_call`), though not a read, is the riskier +mean/−floor profile. The bug-fix earns its place; the hand-tuned levers do not, yet.

---

## Postflop leak audit

A code-grounded audit of V7-M2's exploitable surface, ordered by exploitability. The crucial framing: **most of these bite only against opponents that adapt and read**, which a hackathon field of static heuristic/solver bots largely will not — which is why V7-M2 crushes both validation fields. The exception is #4.

1. **Bet-sizing tell (compounds the no-bluff leak).** `_postflop_by_equity` sizes deterministically by equity (≥0.85 → 0.9-pot, 0.72–0.85 → 0.7, 0.55–0.72 → 0.55, weak steal → 0.5). The bet *size* leaks strength. An adaptive opponent over-folds to the big size (denying value its payoff) and floats/raises the small size. Worse than "no bluffs" because even value bets become readable. Fix = V14 sizing (decouple) — which is also the prerequisite for any future bluff range.
2. **Capped check-call range / no check-raise bluff.** Medium hands (0.42–0.55) check and then can only call or fold facing the follow-up bet; the only check-raise is value (≥0.72). The entire check-call range is capped with no raises, so a barreler faces no risk and can fire turn/river into a checked-and-called pot. Fix = V14 trap (check some strong hands so checks aren't uniformly weak); a check-raise bluff range is the higher-variance alternative.
3. **Street-blind strategy (no river polarization, no multi-street story).** The tree applies identical bands/sizings every street. River bets are capped-value at a readable size (no thin value, no bluffs); river checks are pure give-ups. And per-street independence means the bot can't represent a consistent multi-street range. Fix = V14 river (value-sizing half only; the bluff half is rejected V11/V12 territory).
4. **River over-calling from the stale range (the standout).** Because the opponent's range is held flat across streets, on the river the bot still models the bettor as holding their whole earlier range (including folded air), over-estimates its bluff-catcher equity, and **over-calls**. The mirror of #1: #1 means it can't get paid, #4 means it overpays. **This is the one leak exploitable by a non-adaptive opponent** — any plain thin-value bettor profits — which makes it the most real-world-relevant. Fix = V13 narrow, calibrated on D1 showdowns. Probe = `thin_value` bot. **(V16 note: `action_filter` is the street-aware version of this fix; on the proxy panel it bled, so the *widths*, not the idea, are the problem — calibrate on D1 showdowns before trusting it. The denominator fix (#2) is unrelated and addresses a *level* bias, not the stale-range *shape*.)**
5. **Multiway over-aggression.** Equity is computed vs N opponents (good) but the thresholds are fixed and bet frequencies don't tighten multiway, so a 0.55-equity bet into three opponents fires at the same rate as heads-up — a value-own in a 6-max field. Fix = V14 multiway. *(V16's `spr` flag is adjacent but addresses commitment depth, not opponent count; multiway-tightening remains a V14 item.)*
6. **Preflop is comparatively clean.** Ranges are frequency-mixed (not face-up) and sizing scales with the opener's size (no tell). Residual softness: static charts (a habitual blind-stealer/4-bet-bluffer isn't punished, but that requires an adaptive opponent) and a slightly tight `req+0.06` stack-off threshold. Minor. *(V16's `spr` flag makes that `req+0.06` stack-off threshold SPR-aware; it was inert on 100bb.)*
7. **Meta-leak: it is a static strategy (the dead read-loop).** The bot computes a full opponent model and never uses it, so it plays identically against everyone and never adjusts when an opponent starts exploiting it. Against a strong *adaptive* bot this is the thing that loses. The irony: closing this loop is exactly what bled in V9 and V11. Real leak, no cheap fix.

---

## Strategic analysis: why exploitation doesn't pay here

The recurring lesson across V8/V9/V11/V12 (and now V16) has a clean theoretical statement.

**Exploitation is two spots, and V7-M2's discipline already covers the dangerous halves.** When *we* bet, the EV of bluffing comes from the opponent over-folding relative to break-even (`f* = s/(1+s)`), and the EV of thin value comes from them over-calling — but the *safety* of a bluff comes from the opponent being capped/passive, not from their fold frequency. A check-raiser over-folds in aggregate yet punishes the residual; that distinction (frequency = EV, capped/passive = safety) is exactly what `fold_vs_bet` cannot see and what sank V11. When *they* bet, exploitation means calling lighter vs over-bluffers and folding more vs trappers — and V7-M2's range-aware calling plus passive-rocket override already approximate the correct response, which is why the pressure field (three relentless bluffers) loses to it outright.

**The imbalance is matched to the field.** A value-only, face-up strategy is theoretically exploitable, but only by an opponent that can *both* fold to value *and* avoid spewing into calls — i.e. a balanced GTO-style bot, not an aggressor. Against the call-heavy / over-aggressive bots that populate these fields, value-only is close to optimal: it never makes the one mistake (bluffing into a station) the fields punish, and it value-owns and snaps off the rest. The V12 c-bet wash is the direct measurement: adding balance recovered ~zero, because there was no imbalance-punishment to relieve. The single untested angle remains a *balanced, non-spewy* opponent (the `balanced_probe_field` / `gauntlet` fields target it); it is also the rarest archetype in a hackathon.

**Conclusion.** Improving an *input* to a uniform policy (V7's range-aware equity; **V16's equity-denominator fix**; V13's range fits, once calibrated) is robust. *Overriding* the policy on a read (V9, V11) bleeds via false positives. *Narrowing villain ranges on eyeballed widths* (V7.2, V13-narrow, **V16 `action_filter`**) bleeds when the field continues wider than the widths assume. *Adding balance* (V12, V16 `blocker_bluff`) is a no-op against fields that reward value. V7-M2's edge — and the V16 foundation's larger mean — is accurate equity + tight discipline + respecting aggression, with graceful failure: the skill-toward-bounded-variance profile a 400-hand, 6-max, top-64 contest rewards.

---

## Patch-window toolchain

Built and verified during the V11–V14 work; ready for the June 2 hand-history drop. All are pure-Python and tested offline (eval7 not required for the tooling logic).

- **`d1_profiler.py`** — schema-tolerant hand-history profiler + range calibrator. `sniff` inspects an unknown JSON schema (auto-maps via a single `KEYMAP` block with multi-name fallbacks); `profile` computes per-opponent VPIP/PFR/3-bet/fold-to-cbet/raise-vs-bet/aggression and flags which lever each opponent type invites; `ranges` fits the V13 range strings (`CONTINUE`, `OPEN_WIDE`, `THREEBET_WIDE`, …) to actual showdown holdings grouped by line. Validated end-to-end on synthetic data with planted profiles. **Caveat:** showdown holdings are the made-hand/value part of each line and under-count bluffs that folded earlier — emitted widths are a lower bound; the exploit *flags* are screening heuristics (use the underlying rates, not the flags). **(V16: this is also the tool to calibrate `action_filter`'s per-street widths, which bled when hand-set.)**
- **`harden_v7m2.py`** — pre-submission safety gate: static AST scan (replicates the validator's banned-construct rejections), 10k-state crash-safety fuzz of `decide()`, and a timebox audit. V7-M2 passes clean (no banned constructs, never raises, never returns an invalid action). Wall-clock timing must be confirmed on a box *with* eval7 (the validation runs show worst-action 45–61ms, far under the 2s budget). **(V16 foundation: passes the same crash/scope fuzz in dev — 0 exceptions over 2000 random flag-combination runs + 800 staged states — but has NOT had a full `harden` pass, eval-mode run, or timing audit; required before any V16 submission.)**
- **`pressure_bot.py`** — strong adaptive polarized bluffer: tight-aggressive preflop (credible range), bluffs relentlessly into checks and semi-bluffs draws, **folds correctly to bets** (refuses to pay off value), ramps bluffing vs confirmed over-folders. The sparring opponent for the imbalance question. (Lost decisively to V7-M2 in the pressure field — see above.)
- **`sizing_reader.py`** — probe for leak #1: reads bet *size* (folds to big, attacks small) regardless of board. Measures whether V14 sizing closes the tell.
- **`thin_value.py`** — probe for leak #4: pot-controls flop/turn, value-bets thin on the river. Profits exactly when an opponent over-calls rivers; measures whether V13 narrow reduces the leak.
- **Test fields** (one JSON per file): `sizing_tell_field`, `thin_value_field`, `exploiter_mix` (probes); `reference_field`, `pressure_field` (regression controls); `balanced_probe_field`, `broad_mix2` (proxies). Complemented by `limp_field`, `maniac_field`, `gauntlet`.

---

## Testing methodology

Backtester is launched via a Streamlit dashboard; output is per-arm aggregate JSON only. Workflow is built around that.

**Paired difference is exact, not noisy — except when a fallback RNG is unseeded.** In AB mode all arms run on identical seeded hands, so `total_delta(candidate) − total_delta(baseline)` is the *exact* paired chip difference (hands where bots agree contribute 0). The per-arm `ci95` measures within-arm bounce and is irrelevant for A/B decisions — ignore it for comparisons; use it only for absolute bb/100. **V16 caveat:** this exactness was empirically *broken* in `balanced_probe_field`, where two byte-identical-logic bots diverged by up to ~1.9 bb/100 (20–33 of 100 matches). Cause: unseeded `random.Random()` fallbacks in the equity path (and eval7's internal MC RNG) that fire when the range model falls back to random equity — most often against balanced/competent opponents. Until those sites are seeded from the spot seed, `balanced_probe_field` paired diffs carry ~±1.5 bb/100 of noise and its floor should be discounted. **`eval`-mode runs** (free-for-all, no shared baseline) report absolute per-bot `bb_per_100` with a meaningful `ci95` and placement distributions; use those for absolute standing, as in the validation runs above.

**Trust the granular fields, not the headline.** An early AB result carried a buggy top-level `stats`/`vs_baseline` block that reported two genuinely-different bots as identical; the truth was in `per_match_paired` (354 of 3800 instances differing). **Always read `per_match_paired` (count of nonzero entries = the real "did it fire / by how much"), not the summary block.** This is also how the V16 wave caught that two "different" arms (`v16` vs `v16.1`) were the same logic (0 nonzero matches), and that a flag arm was genuinely firing (1400–1800 nonzero matches).

**Confidence via seed replication — 3 seeds is not enough for a floor.** One `seed_base` = one exact paired-diff sample. Run K seed_bases (5 default; ~10 for high-variance fields) and take the mean/CI of the paired diffs. This resolves ~1 bb/100, which the per-arm CI cannot. **V16 concrete demonstration:** `thin_value_field`'s floor moved **+0.88 → −2.65** between two batches *on an unchanged bot*. A floor verdict at 3 seeds is not trustworthy; the V16 negative floors are unresolved for exactly this reason. For multi-field families, correct for multiple comparisons (Bonferroni over the field × variant grid) before calling a cell significant — the V13 analysis used α=0.0025 over 20 tests and found the one raw-0.05 cell was the expected false positive.

**Probe + regression methodology (for leak fixes).** A leak fix needs two runs to interpret: a **probe field** containing the exploiter (so the fix can show value) and the **regression controls** where the baseline already wins (so the fix can't bleed). A fix that beats its probe but holds the controls is a real fix the panel was blind to; a fix inert on the panel is not necessarily worthless — the panel may lack the exploiter. This is why the probe bots exist. **A flag whose trigger archetype is absent from a field is invisible there:** the V16 flags showed 0 divergence on the generic panel when off, and `spr`/`blocker_bluff` showed near-0 even when on, because their trigger spots (short-stack commitment; dry-board IP steals) barely occur at 100bb 6-max — test a lever against the field that contains its trigger.

**Batch candidates into one job.** Put baseline + all candidate variants in a single AB job per field per seed_base. Run count is `seeds × fields`, independent of candidate count.

**Frozen panel.** A fixed set of fields, never changed mid-development:
- *Robustness tier:* barrel, polar3bet, adaptive_test, plus archetype fields (folder, station, perma-jam).
- *Leak-finding tier:* one isolation preset per opponent (now including the sizing/thin-value probe fields).

**Promote/reject gate — select on the floor, not the average.** A change promotes only if its paired-diff CI lower bound stays above a small negative tolerance in *every* field. A +2/−1.5 profile is rejected even with positive mean — that asymmetry is exactly the V8/V9/V13-lag failure, and exactly the V16-foundation and `ev_call` profile (strong mean, negative floor in some fields) — which is why neither is promoted despite a positive mean.

**One change per candidate.** Never bundle two ideas; batched arms isolate for free anyway. (V14 enforces this with one flag per fix; the V16 flag builds do the same — one flag True per file.) **Corollary (V16):** the V16 *foundation* bundles eight fixes, which is why it cannot be cleanly judged as a unit — decompose it (below) so the safe correctness fix (#2) can promote without the behavioral suspects (#3/#4).

**Identity probes at load time.** `load_decide` prints one stderr line per unique `(path, BOT_NAME)` pair. This surfaces deployment-level "same file under multiple paths" failure modes at load time rather than after the run completes. Added after the v10.1–v10.3 episode where the framework appeared buggy until the probe forced the search into the bot's own logic. **(V16: distinct `BOT_NAME`s per flag build keep the probe able to separate arms — and confirmed the `v16`/`v16.1` arms were the same logic.)**

**Sandbox limitation.** eval7 does not build in the development sandbox (Python 3.12), so equity-path code cannot execute there. V11–V14 logic was verified by differential/scope tests on synthetic equity and direct calls; chip EV is measured only on the (eval7-enabled) backtester. Probe-bot strength likewise depends on eval7-backed reads — confirm via hand replay before trusting a null result. **(V16 note: the dev environment used for the V16 build *did* have eval7, so the foundation's mechanisms were unit-tested live — but chip EV is still only the backtester's, as above.)**

---

## Safety and validation

- **No banned constructs** (static AST scan clean: no socket/subprocess/file-write/eval/exec/`__import__`/reflection). *(V16 adds a module-level `_V16_FLAGS` dict and helper functions only — no new imports or banned constructs; re-run the AST scan on any V16 submission.)*
- **Never crashes:** 10k malformed/adversarial states through `decide()` — zero exceptions, zero invalid returns; the try/except net falls back to a safe check/fold. *(V16 foundation: 0 exceptions over 2000 random flag-combination runs + 800 staged states in dev; full 10k `harden` fuzz still required before submission.)*
- **Within time budget:** worst single action 45–61ms across the validation runs (2s limit), zero slow flags, zero timeouts. *(V16: the denominator fix changes the MC loop's stopping condition — re-confirm timing on an eval7 box, since the loop can now run more attempts on heavily-conflicted ranges before hitting `max_iters`.)*
- **Graceful degradation:** `equity_vs_range` → `equity_vs_random` → emergency-fold on any failure. *(V16 open item: the random-equity fallback's unseeded RNG breaks paired-AB determinism — see methodology; functionally safe, but seed it for clean measurement.)*

Run `harden_v7m2.py` on the final submission file, then save the `[load_decide] BOT_NAME=…` stderr from a final AB run as deployment verification.

---

## Known leaks and open items

**Squeezer leak — RESOLVED.** Pre-fix the bot folded AA to 3-bets; the +4.18 estimate was a bug, not a calibration issue. Post-fix, further `_VS_3BET` tuning is neutral-to-negative (V10 rejection).

**Equity-MC denominator bias — RESOLVED (V16).** `_equity_vs_multi_range` counted dead/conflicted trials in the denominator, deflating equity worst in tight/multiway spots; fixed by separating attempts from successful trials. The second-largest correctness fix in the project; folded into the V16 foundation. Because it is opponent-independent it is the cleanest single thing to promote — pending the decomposition test (roadmap).

**Equity-fallback nondeterminism — OPEN (V16).** Unseeded `random.Random()` at two equity-fallback sites (plus eval7's internal MC RNG) break exact paired-AB pairing when the range model falls back to random equity (balanced/competent opponents → `balanced_probe_field`). Two identical-logic bots diverged up to ~1.9 bb/100 there. Fix: seed the fallback RNG from the spot seed. Until fixed, discount `balanced_probe_field` paired diffs (~±1.5 bb/100 noise). Functionally harmless to play; purely a measurement-cleanliness issue.

**River over-call (leak #4) — the priority patch target.** The one leak exploitable by a non-adaptive opponent. Fix = V13 narrow, calibrated on D1 showdowns; probe = `thin_value`. Cheapest meaningful EV recovery if the field value-bets thin. **V16 status:** `action_filter` is the street-aware implementation of this fix, and it bled (−2.7) on the proxy panel with eyeballed widths — confirming the idea is sound but the widths must come from D1 showdowns, not hand-tuning.

**Bet-sizing tell + capped checks (leaks #1, #2).** Real but adaptive-only. V14 sizing/trap built and scope-verified, EV-untested; probe = `sizing_reader`. The bigger, higher-variance rebuild — and the prerequisite for ever adding a balanced bluff range.

**Hidden postflop leaks from newly-reached states — PARTIALLY MEASURED (V16).** Because the bot now actually calls some 3-bets it previously always folded (V7-M2 4-bet defense; **and V16's cold-4bet branch #3**), it reaches OOP postflop spots the equity logic was never tuned against. The V16 foundation's negative floors in `exploiter_mix` / `broad_mix` are the leading symptom. Isolate by AB-ing a foundation-with-cold-4bet-disabled arm vs the foundation (decomposition note, roadmap). Still needs ≥10 seeds.

**Sibling bugs in `_preflop_history`.** Audit pending. Same inverted-gate pattern could exist elsewhere. 30-second grep; potentially several bb/100 of recoverable EV.

**`_defense_branch` calibration.** With `_four_bet_branch` alive and wider defense shown worse, the open question is whether `_defense_branch` (cold-calls vs opens, multi-way pots) is correctly calibrated. Unexamined. *(V16's cold-4bet branch #3 is a new, adjacent defense path that the negative-floor evidence suggests is the highest-priority calibration target.)*

**Sizing menu / multi-size policy.** Bot uses one size per spot. Multi-size conditioned on board texture is plausibly +2 to +4 bb/100 against opponents who don't adapt. Same family as V14 sizing/river; deferred to patch window with D1 calibration.

**Balanced-opponent angle.** The one strategy profile that could punish the imbalance (folds to value AND bluffs non-spewily) is untested beyond `gauntlet`/`balanced_probe_field` — and `balanced_probe_field` is now known to be RNG-noise-contaminated (above), so it needs the RNG fix before it can answer the question. Worth a run; rare in the field.

**`ev_call` polar3bet regression — OPEN (V16).** The `ev_call` flag (pot-odds/realization call rule) is +1.06 mean but −2.56 on polar3bet. Three tuning variants (size-aware / tighter / looser premium) are built and EV-untested; sweep at ≥10 seeds on the gain + regression fields. If a variant holds the gains and brings polar3bet ≥~0, it would be the first floor-clearing *policy* change in the project.

**Scoring objective.** Qualifier = cumulative chip delta over Swiss (favors chip-EV). Finals = single-elim bracket (variance-bounded play better). Current bot leans chip-EV — correct for D1, possibly suboptimal for D5. A higher-variance D5 variant is testable on the tournament-sim (unbuilt).

---

## Competition roadmap

**D1 ship (May 31 deadline): V7-M2 (fix) as-is.** No further extension work pre-deadline. Every extension family — V8, V9, V11, V12, V13, and now V16 — has been evaluated and rejected, deferred, or left floor-unconfirmed; V14 is built but EV-untested and touches the core. The **V16 foundation has a strong +3.8–4.2 mean but its floor is unresolved at 3 seeds, it includes core-touching behavioral changes (cold-4bet, thin value), and it has had no `harden`/eval/timing pass** — shipping it pre-deadline risks the unresolved negative floors for an unconfirmed gain. The validation runs (+28.8 vs reference, +17.9 vs pressure, clean safety) over-determine the V7-M2 (fix) decision.

Pre-deadline checklist:
1. Audit `grep` for other `last_raiser_*` / `first_raiser_*` reads. Fix any inversions and re-AB.
2. Run `harden_v7m2.py` on the submission file; save the `[load_decide] BOT_NAME=…` stderr from a final AB run as deployment verification.
3. Ship.

**Patch window (June 2–4):** the higher-EV sprint, because the field becomes concrete (D1 hand histories land June 2). The toolchain is already built. In priority order:
1. **Resolve the V16 foundation floor** — re-run foundation vs V7-M2 (fix) on the four negative-floor fields (`exploiter_mix`, `broad_mix`, `thin_value_field`, `balanced_probe_field`) at **≥10 seed_bases**. The current floors are variance-dominated (one swung 3.5 bb/100 on an unchanged bot). This single run decides whether the foundation is a genuine upgrade or a +mean/−floor trap, and gates everything downstream.
2. **Decompose the foundation** to separate the safe fix from the suspects — AB arms: (a) `#2` denominator fix alone, (b) `#2`+board-aware ranges (`#1`/`#6`), (c) full foundation (adds cold-4bet `#3` + thin value `#4`). The denominator fix is opponent-independent and the most likely clean promote; if the negative floors live in (c)-minus-(b), cold-4bet/thin-value are the bleed and the safe subset can ship without them.
3. **Fix the equity-fallback RNG** (seed from the spot seed) so `balanced_probe_field` pairs exactly, then it can finally answer the balanced-opponent question.
4. **`ev_call` tuning sweep** — the three variants vs the all-off foundation + V7-M2 on the gain (`broad_mix`, `pressure`, `thin_value`, `bluffing`) and regression (`polar3bet`, `broad_mix2`) fields at ≥10 seeds. Promote a variant only if it holds the gains and brings polar3bet ≥~0.
5. **D1-calibrated range work** — if the field value-bets thin → calibrate V13 narrow / `action_filter` widths on `d1_profiler ranges` output (the eyeballed widths bled; real widths may not). If it reads sizes → V14 sizing. If competent/balanced → keep the best floor-confirmed bot.
6. Validate every patch with the probe + regression methodology against the *real* opponents (or reconstructions), not the proxy panel.

**Finals (June 5):** single-elim bracket. Even with the best bot in the field, ~3–5% trophy probability after variance. The bot is the ticket; the rest is the lottery. (Consider a variance-bounded D5 variant if the tournament-sim is built and shows it raises advancement probability at equal chip-EV.)

---

**Submission rule:** ship the version with the highest worst-field floor, not the highest mean. V7-M2 (fix) clears that bar; **no other variant in the project does** — V8/V9/V11 bleed on the floor via false positives, V12 regresses on barrel, V13 is inert-or-eyeballed, V14 is untested, and the **V16 foundation has a strong +mean (+3.8–4.2) but unresolved negative floors at 3 seeds** (its `action_filter` flag bled outright; `ev_call` is +mean/−floor). The frozen baseline is the submission. The **V16 foundation is the leading patch-window promote candidate** — and the equity-denominator fix (#2) within it is the single most likely clean win — *if* the ≥10-seed floor resolution clears it.
