# RUN_REPORT - meridian-ledger-recon

**Author:** Kushagra Gupta (iamkushagragupta@gmail.com)
**Date:** 2026-06-13
**Slug:** `collinear-candidate/meridian-ledger-recon`

## 1. Task in one paragraph

A payment platform's snapshot rebuilder violates its written spec in several interacting
ways: no idempotency/dedup, file-order replay instead of `(ts, seq)` order, non-idempotent
account-open handling, near-correct but wrong transfer-fee rounding, a fee-exclusive
affordability check, and missing rejection for overdrawing adjustments. The agent must
audit code against spec, fix **all** deviations, regenerate the
SQLite snapshot from the 2,600-line event log, and emit a reconciliation report. A
held-out event log forces a genuine code fix rather than a hand-edited database.

## 2. Ground-truth scale of the corruption (measured)

Reference replay of `data/events.ndjson` (40 accounts, 2,600 lines, 2,480 distinct
events, 120 retransmissions, 413 correctly rejected events) vs the shipped engine's
output:

- **All 40 account balances are wrong.**
- Total absolute drift: **23,760,143 cents (~$237,601)**.
- Rejected-event symmetric difference: **809 event ids**.
- Visible symptom seeded for the incident narrative: stale snapshot contains negative
  balances, which SPEC Section 3 forbids for withdrawal, transfer, and adjustment
  activity.

Materiality of the required fixes was verified by ablation (fix all but one): every
single-remaining-bug variant scores far below the 0.99 solve threshold. No bug masks
another; finding "most" of them cannot pass.

## 3. Oracle result (executed)

Run on a clean simulated container state (seed files copied, stale snapshot materialized
by the shipped engine exactly as `environment/Dockerfile` does), then graded through the
real entrypoints (`bash solution/solve.sh`, `bash tests/test.sh`):

```json
{
  "overall": 1.0,
  "functional_correctness": 1.0,
  "constraint_satisfaction": 1.0,
  "robustness": 1.0,
  "artifact_quality": 1.0
}
solved: True
```

Harbor command:

```bash
harbor run -p ./meridian-ledger-recon -a oracle
```

## 4. Verifier quality evidence (executed)

Executed locally with Python 3.12:

```text
oracle                    -> 1.0000  PASS-as-expected
do-nothing                -> 0.0219  FAIL-as-expected
db-only cheat (no patch)  -> 0.7500  FAIL-as-expected
partial fix: missing_dedup          -> 0.0679  FAIL-as-expected
partial fix: missing_ordering       -> 0.1181  FAIL-as-expected
partial fix: missing_fee_rule       -> 0.1180  FAIL-as-expected
partial fix: missing_fee_in_check   -> 0.1205  FAIL-as-expected
partial fix: missing_adjustment_clamp -> 0.1139  FAIL-as-expected
verifier self-check: ALL OK
```

| Attempt | Score | Expected |
|---|---:|---|
| Oracle | 1.0000 | Pass |
| Do Nothing | 0.0219 | Fail |
| DB-only Cheat | 0.7500 | Fail |
| Missing Dedup | 0.0679 | Fail |
| Missing Ordering | 0.1181 | Fail |
| Missing Fee Rule | 0.1180 | Fail |
| Missing Fee Check | 0.1205 | Fail |
| Missing Adjustment Clamp | 0.1139 | Fail |

Interpretation:

- **No false negative:** the included oracle scores exactly 1.0.
- **No false positive:** the do-nothing baseline, a database-only "fix" that skips the
  code patch (caught by the held-out-log robustness check), and all all-but-one partial
  fixes score far below the 0.99 solve threshold.
- **Ablation coverage:** these ablation tests demonstrate that the verifier rejects
  incomplete solutions and rewards only a complete implementation.

Docker Desktop Linux daemon access initially required elevation from this PowerShell
session. After elevation, the environment was validated with a digest-pinned no-cache
Docker build and oracle/verifier run:

```text
docker build --no-cache -t meridian-ledger-recon-check-pinned ./meridian-ledger-recon/environment
docker run --rm ... meridian-ledger-recon-check-pinned bash -lc \
  "bash /solution/solve.sh && VERIFIER_LOGS_DIR=/logs/verifier bash /tests/test.sh"

{
  "overall": 1.0,
  "functional_correctness": 1.0,
  "constraint_satisfaction": 1.0,
  "robustness": 1.0,
  "artifact_quality": 1.0
}
```

## 5. Threat Model

The verifier is designed against the main shallow or adversarial paths a model might take:

| Threat | Detection |
|---|---|
| Agent patches only `/app/state/ledger.db` and leaves the engine broken. | The robustness check runs `/app/app/ledger.py` on a held-out event log; the DB-only cheat scores 0.7500 and fails. |
| Agent partially implements the ledger rules. | The ablation cases each leave one required fix missing, and every one scores far below the 0.99 solve threshold. |
| Agent overfits the visible production log. | The held-out replay requires the patched engine to generalize to another spec-conforming event log. |
| Agent ignores or fabricates `report.json`. | `artifact_quality` recomputes all four report values from the corrected reference replay and a pristine stale baseline. |
| Agent tampers with `/app/state/baseline_stale.db`. | The grader recomputes the stale baseline from a vendored copy of the shipped buggy engine, so the report oracle is not affected by baseline tampering. |

## 6. Target-model run

Commands (either approved target):

```bash
harbor run -p ./meridian-ledger-recon -a claude-code -m claude-opus-4-7
harbor run -p ./meridian-ledger-recon -a codex -m gpt-5.5-high
harbor view ./jobs
```

Executed target trial:

```bash
harbor run -p ./meridian-ledger-recon -a codex -m gpt-5.5-high
harbor view ./jobs
```

Job: `jobs/2026-06-13__10-18-16/meridian-ledger-recon__PAB4AdQ`

Verifier result:

```json
{
  "overall": 0.9625,
  "functional_correctness": 1.0,
  "constraint_satisfaction": 1.0,
  "robustness": 1.0,
  "artifact_quality": 0.75
}
```

This is a target-model failure because `overall < 0.99`. The model fixed the engine
correctly and generalized on the held-out log, but failed one report requirement. The
grader expected:

```json
{
  "duplicate_events_ignored": 120,
  "rejected_event_ids": "<413 ids>",
  "accounts_with_corrected_balance": 40,
  "total_absolute_drift_cents": 23760143
}
```

The produced report had `accounts_with_corrected_balance = 39`, while the other three
report fields were correct. The precise missed edge case: the stale baseline contains
only 29 account rows because late duplicate `account_open` processing erased state in
the shipped engine, while the corrected snapshot contains all 40 accounts. One account
exists only in the corrected snapshot with a final balance of 0. The model's SQL used
`COALESCE(missing_balance, 0)` for the count comparison, so that missing stale account
was treated as unchanged (`0 == 0`) instead of as a corrected/missing account. The
verifier correctly counts account presence differences for
`accounts_with_corrected_balance`, while using 0 only for the drift magnitude. The
failure mode is therefore skipped/insufficient verification of a derived reconciliation
artifact after a successful code repair.

### Expected failure modes (what the verifier is instrumented to expose)

The ablation grid in Section 4 is exactly the space where frontier models land on this
task, and each cell is a distinct, diagnosable capability failure:

1. **Incomplete inspection (most likely):** dedup and ordering are heavily hinted by the
   spec, but the fee rule (`round(amount * 25 / 10000)` vs integer `//`), account-open
   idempotency, fee-*inclusive* sufficiency check (`balance < amount + fee`), and R6
   adjustment rejection are easy to skim past. Any such miss lands at about 0.07-0.12
   with a clean signature in `grade_details.json` (wrong balances + wrong rejection set).
2. **Bad state tracking / shallow patch:** correcting balances by editing the SQLite
   snapshot or post-processing the diff instead of fixing the engine caps at 0.75 via
   the held-out log (robustness = 0).
3. **Fee-rule near miss:** leaving `round()` in place or using float arithmetic -
   integer-arithmetic discipline (R3/R5) is graded exactly, and the generator guarantees
   half-cent and sub-minimum amounts occur in both logs.
4. **R6 near miss:** clamping overdrawing adjustments to zero, using the wrong rejection
   reason, or forgetting the rejected/meta counts fails `constraint_satisfaction`.
5. **Skipped verification / report hallucination:** the four report figures are
   recomputable from provided files; a model that asserts numbers without recomputing
   them loses `artifact_quality` even when the snapshot is right.

A trial counts as the target failure if `overall < 0.99`, with the failing criterion and
the per-criterion diagnostics recorded in `/logs/verifier/reward.json` and
`/logs/verifier/grade_details.json`.

## 7. Fairness audit

- **Solvable:** oracle passes at 1.0; a careful human needs ~60 min (read 120-line spec,
  audit 160-line engine, write a conforming replay, self-check with throwaway scripts).
- **Unambiguous:** instruction.md fixes the deliverables, exact report schema, CLI
  contract, and success criteria; SPEC.md is normative and complete (worked fee
  examples included).
- **Reproducible:** digest-pinned base image
  (`python:3.11.9-slim-bookworm@sha256:8fb099199b9f2d70342674bd9dbccd3ed03a258f26bbd1d556822c6dfc60c317`),
  stdlib only, no network, no secrets; the stale snapshot is deterministically rebuilt
  inside the Dockerfile from committed seed data.
- **Non-brittle:** semantic SQL-level comparison, order-insensitive report list, int
  coercion for JSON numbers, independent per-criterion grading, generous timeouts
  (agent 60 min, verifier 15 min for a sub-second workload).
- **No tricks:** no hidden files the agent needs, no time pressure, no flaky services;
  the only "hidden" material is the verifier itself (reference implementation + holdout
  log), which the agent does not need to pass.

## 8. Provenance

All scenario code, the spec, both event logs (deterministic generator
`tests/gen_events.py`, seeds 20260610 / 20260613), the verifier, and the oracle were
created from scratch for this exercise and hardened on 2026-06-13. No benchmark, CTF,
Kaggle, public-issue, or prior-task material was used. External components: Python 3.11
stdlib and the Docker Hub `python:3.11.9-slim-bookworm` base image pinned by digest only.

## 9. Repro quick reference

```bash
docker build -t meridian-ledger-recon ./meridian-ledger-recon/environment
harbor run -p ./meridian-ledger-recon -a oracle
harbor run -p ./meridian-ledger-recon -a claude-code -m claude-opus-4-7   # or: -a codex -m gpt-5.5-high
harbor view ./jobs
python meridian-ledger-recon/tests/test_verifier_selfcheck.py             # verifier self-check
```
