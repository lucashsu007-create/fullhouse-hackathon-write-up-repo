# Backtest Dashboard: Throughput and Workflow Upgrade

## Context

The Fullhouse backtest dashboard drives the poker engine match-by-match for hero evaluation and paired N-way comparisons. It consists of `app.py` (the Streamlit UI, roughly 1900 lines), `analysis.py` (pure orchestration, IO, and statistics, kept free of any Streamlit import so it remains unit-testable in isolation), and `backtest.py` (the harness primitives: bot loading, match execution, EV adjustment, summary statistics). Two pain points motivated this upgrade.

The first was a workflow problem. Multi-factor sweeps — the canonical example being a paired comparison of five hero arms across three opponent fields on five different random seeds — required queueing one job at a time, fifteen clicks in sequence, with the `seed_base` parameter manually changed between each. This was error-prone and scaled badly as comparison sets grew.

The second was a runtime problem. Every match ran sequentially in the Streamlit process. On a typical multi-core development machine the observed memory footprint during a run was around 200 MB against multi-GB available, because exactly one Python process was doing work at any moment. The bottleneck was not memory; it was that fifteen cores out of sixteen sat idle for the duration of every job. A standard sweep (15 jobs at 100 matches × 750 hands) was an overnight run on a machine that should have been able to finish in an hour.

Both pain points were addressed in the same change set.

## Change 1 — Batch Sweep Across Presets × Seeds

The first change extends the pre-existing batch-sweep expander in Tab 2 of the dashboard with a second axis. Previously the expander queued one job per selected preset, all using the single `seed_base` value from the params panel above. The new version accepts a comma- or space-separated list of seeds in a text input next to the preset multiselect, and the queueing loop iterates the full cross-product.

Three presets times five seeds is now fifteen jobs queued in one click. Each queued job receives a per-iteration copy of the params dict with `seed_base` overridden to its specific value, and a label of the form `{prefix} · {preset} · s{seed}` so the per-job result files are unambiguously identifiable downstream. A blank seeds field falls back to the single `seed_base` from the params panel above, preserving the previous one-seed-per-sweep behavior as a special case.

The seed parser deduplicates while preserving entry order, surfaces parse errors inline through a Streamlit error message, and disables the queue button when the list cannot be evaluated. A live caption above the button shows the cross-product count so the user can verify the queueing scale before committing — `Will queue 15 job(s) (3 preset(s) × 5 seed(s))`.

The paired-arm structure inside each AB job is untouched. When ab mode is the selected mode, every queued job receives all currently-selected heroes as paired arms against the same field on the same seed; the expander caption explicitly warns against splitting arms into separate 2-way jobs, since that would lose the per-match comparison the paired t-statistic depends on.

## Change 2 — Process-Pool Parallelism for Match Execution

The second and larger change introduces a process pool for per-match dispatch inside `analysis.py`, exposed through a Workers control in the dashboard params panel.

### Bottleneck identification

Each match in `_run_eval` and `_run_multi` is independent. The serial loops mutate per-bot accumulator arrays, but the per-match work itself is a pure function of the seating order, the seed derived from `seed_base + i`, and the freshly-built decide map. No iteration depends on any earlier iteration. No global state is read or written by the engine in a way that ties matches together. This is the textbook condition for embarrassingly parallel dispatch, and it had been left on the table.

The choice of parallel unit for `_run_multi` deserves explicit note. A `_run_multi` job at match index `i` runs N sub-matches — one per hero — all against the same field at the same seat on the same seed. Two granularities were available: one future per sub-match (finer-grained, marginally more parallelism on small comparison sets), or one future per *round* of N sub-matches (coarser-grained but preserves the paired structure as the unit of progress reporting). Round-granularity was chosen because it keeps round-aligned variables — `seed`, `hero_idx` — local to a single worker call, matches the existing `progress_callback(i + 1, matches)` contract one-for-one, and avoids the result-stitching ambiguity that sub-match granularity would introduce when futures complete out of order.

### Implementation

Three new top-level functions in `analysis.py`. The pool initializer runs once per worker process:

```python
def _pool_init(sys_path_entries, repo_path):
    """Reproduces the parent's import surface in each worker: rebuilds
    sys.path, imports backtest, binds harness primitives into analysis's
    module globals."""
```

On Linux with the default fork start method, workers inherit the parent's `sys.path` and already-imported modules, so the init is a cheap no-op (the `import backtest` resolves from the cache; `bind_harness` is harmlessly re-applied). On Windows and on macOS Python 3.8 and later, where spawn is the default, workers start with a fresh interpreter and the init does the real work — rebuilding the import surface, importing backtest, and binding the harness primitives.

The two match worker functions are pure: they accept a fully picklable payload (lists of strings, dicts of string-to-string, scalars), build their own decide map, call `run_match_inproc`, and return a tuple of the match index and the result. The match index travels in both the payload and the return value so the parent can fill per-bot arrays by index regardless of which future happens to complete first.

```python
def _eval_match_worker(payload) -> tuple[int, dict]:
    """One eval-mode match per future."""

def _multi_round_worker(payload) -> tuple[int, list[tuple[str, dict]]]:
    """One paired-multi match-index round per future."""
```

The serial paths in `_run_eval` and `_run_multi` were refactored to use the same index-based fill pattern as the parallel paths — pre-allocating `[None] * matches` per-bot arrays and writing to `per_bot[bid][i]` rather than appending. This produces output identical to the previous form for sequentially-completing matches, and it ensures the two execution paths cannot drift in subtle ways. A small `_absorb` helper inside each function captures the common per-match folding logic and is called from both branches.

The parallel branch dispatches via `concurrent.futures.ProcessPoolExecutor`, collects via `as_completed`, and folds each result into the per-bot arrays as it arrives. The progress callback fires on each completion, so the dashboard's ETA reflects real throughput rather than a sequential approximation.

### Why processes, not threads

Three considerations drove the choice of processes over threads. First, the dashboard's "Reload bots per match" guarantee — required for stateful bots such as `adaptive_exploit` to reset between matches — depends on each match getting a fresh module namespace via `backtest.load_decide`. With processes, each worker has its own `sys.modules` and `_load_seq` increments independently. With threads, `sys.modules` is shared, and although the unique-name scheme in `load_decide` would technically still produce distinct entries, the accumulated module table per process would grow without bound across the run and the isolation guarantee would be harder to reason about.

Second, the per-action `budget` enforced inside `run_match_inproc` is wall-clock-based. Any timeout path that uses `signal.alarm` only works from a process's main thread. With threads, timeout enforcement would silently break for non-main worker threads; with processes, each worker has its own main thread and timeouts work identically to the serial case.

Third, the engine and `eval7` are CPU-bound C extensions. Threading would remain GIL-contended on the Python-side bot decision logic that wraps them. Processes sidestep the GIL entirely.

### Reload-mode compatibility

Eval-mode parallelism is gated on `reload=True`. The serial path optimizes `reload=False` by building the decide map once at the top of `_run_eval` and reusing it across all matches, but loaded decide callables are not picklable across process boundaries — they are closures over freshly imported modules whose state lives in a specific process. The parallel branch therefore checks `use_parallel = workers > 1 and reload and matches > 1` and falls back to serial when reload is off. The dashboard surfaces a yellow warning under the Workers control when this combination is set, making the fallback visible rather than silent. AB mode is always reload-per-match by construction, so the guard never triggers there.

### Dashboard control

A new Workers `number_input` was added to the params panel in Tab 2. The default is `os.cpu_count() - 1`, leaving one core free for the operating system and Streamlit responsiveness; the maximum is the visible CPU count (or 32 on very small machines, to keep the input editable). A caption next to the input dynamically shows either an expected speedup line — for example `≈ 8× wall-clock speedup target on this job (machine reports 16 CPUs)` — or the reload-off fallback warning, depending on mode and reload state.

The worker count flows through `params["workers"]` into the job dict via `make_eval_job` and `make_ab_job`, through `run_job` where it is read back out of params, and on into `analysis._run_eval` or `_run_multi` as a keyword argument. `run_job` also forwards `worker_repo_path` from the sidebar's repo-path input so the pool's initializer can reproduce the parent's engine import surface in spawn-based worker processes.

## Correctness Verification

A smoke test (`smoke_parallel.py`) installs a deterministic stub harness in place of `backtest` and compares serial and parallel results across three properties.

The first property is that `_run_eval` with `workers=1` and `workers=4` produces identical `per_match_deltas` and `per_match_placement` arrays under the same inputs. The stub's `run_match_inproc` is a pure function of seating order, seed, and hand count, so deterministic equality is the right correctness criterion.

The second property is that `_run_multi` with `workers=1` and `workers=4` produces identical `per_match_deltas` and — critically — identical `per_match_paired` arrays. The paired (hero minus baseline) diff is the core signal the dashboard reports for AB comparisons; if the parallel path scrambled match-index alignment, this assertion would fail. It does not fail.

The third property is that the `progress_callback` fires exactly `matches` times under parallel dispatch and that the `done` counter is monotonic. The callback contract is part of the public interface — the UI's progress bar and ETA depend on it — so violating either invariant would propagate to a user-visible bug.

The stub `run_match_inproc` deliberately injects between zero and twenty milliseconds of jittered sleep before computing its (deterministic) chip delta. This forces futures to complete out of submission order during the test — the worst case for any index-alignment bug. All three properties hold under jitter; the parallel and serial outputs are bitwise identical for the per-match arrays.

The test runs in roughly two seconds, requires no engine or eval7 installation, and is suitable as a CI gate. It lives in `smoke_parallel.py` alongside the code.

## Operational Characteristics

Memory scales approximately linearly with worker count. Each worker loads its own copy of the engine, eval7, and the freshly imported bot modules, observed at approximately 200 MB per worker process during a run. With the default `cpu_count - 1` on an eight-core machine that is roughly 1.4 GB total, well within reach of any modern development machine. On sixteen-core machines the default produces around 3 GB of worker memory, which is still small relative to typical 16–32 GB development environments.

Per-job pool lifecycle is one pool per job, not one shared across the queue. Each job creates a `ProcessPoolExecutor`, runs its matches, and tears the pool down before the next job starts. This keeps the per-job progress bar honest (one pool, one set of workers, one ETA), guarantees clean worker state across jobs by virtue of process termination wiping module caches, and avoids the complication of holding workers across heterogeneous payloads. The startup cost — roughly 600 ms per worker on spawn, near zero on fork — is amortized over 100 matches times 750 hands per job in a typical sweep, well under one percent of total job runtime.

Per-action budget semantics are unchanged from the serial case. The budget is enforced per-process; each worker has its own wall clock. A budget of 2.0 seconds means 2.0 seconds in that worker, not aggregate across workers. From the bots' perspective the parallel mode is indistinguishable from running on a sequence of fresh machines.

Expected wall-clock speedup is approximately `(N - 1) × 0.85` to `(N - 1) × 0.95` for N workers, where the lower bound accounts for pool startup and result-collection serialization in the parent process. On an eight-core machine running a 15-job AB sweep at 100 matches × 750 hands per job, this brings a job from roughly an hour of single-core work down to closer to ten minutes. On sixteen-core machines the speedup approaches `15×`. These are targets, not guarantees; actual scaling depends on how CPU-bound the heroes' decision logic is, since per-match work that yields to I/O or contends on file system locks will not parallelize linearly.

## Limitations and Follow-Up Work

The `_multi_round_worker` preserves a known inefficiency from the serial path: within a round, the field is reloaded N times, once for each hero, where reloading once and only swapping the hero's seat would suffice. For a five-arm AB job against a six-bot field this means 35 module loads per round when 11 would be enough. Optimizing this would cut another 30 to 40 percent off per-job wall clock on top of the parallelism gains. The fix is localized to `_multi_round_worker` and the corresponding serial branch in `_run_multi`; it was deliberately not bundled into this change set so the parallelism diff stays isolated and the smoke-test correctness boundary stays small.

Queue-level parallelism — running multiple jobs concurrently rather than parallelizing within a job — was considered and rejected. The progress reporting would need inter-process queues to merge per-job streams, the queue table would no longer linearly index "which job is running," and the optimum degree of inter-job parallelism is bounded by job count rather than match count. At fifteen jobs and eight cores, intra-job parallelism already gives full saturation; inter-job parallelism would add complexity without throughput.

The smoke test does not currently run against the real engine. A future addition could swap in a small live-harness check on a tiny job — for example four matches at fifty hands with two bots — to confirm worker-side import resolution is healthy in the deployment environment. This would catch path-configuration regressions that the stub-harness test cannot see.

Per-action budget behavior under heavy worker contention is worth monitoring in practice. If a workload pegs all cores and a worker's bot decision is starved of CPU, the wall-clock budget could be tripped by scheduling pressure rather than by genuine bot slowness. The dashboard reports per-bot timing in the result JSON; spurious timeout entries that correlate with worker count would be the diagnostic signal. Mitigation, if needed, would be to lower the worker count or raise the per-action budget on jobs run at high parallelism.

## File-Level Change Summary

In `analysis.py`: imports extended with `os`, `sys`, `ProcessPoolExecutor`, `as_completed`. Three new top-level functions added: `_pool_init`, `_eval_match_worker`, `_multi_round_worker`, plus a small `default_workers` helper. `_run_eval` and `_run_multi` refactored to accept `workers` and `worker_repo_path` keyword arguments; both gained a parallel branch and both had their serial branches converted to index-based per-bot array fill for symmetry with the parallel paths.

In `app.py`: the Tab 2 batch-sweep expander gained a Seeds text input, an inline parser, a cross-product queueing loop, and a per-job label scheme. The Tab 2 params panel gained a Workers `number_input` with a dynamic caption and a reload-off fallback warning. `run_job` was extended to read `params["workers"]` and the sidebar's repo path and forward both through to the analysis calls.

In `smoke_parallel.py` (new): three serial-versus-parallel correctness checks using a deterministic stub harness with intentional runtime jitter to force out-of-order future completion.
