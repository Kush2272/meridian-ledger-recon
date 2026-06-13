meridian-ledger-recon
Slug: collinear-candidate/meridian-ledger-recon Category: debugging / data forensics (finance) Network policy: none (stdlib-only Python; offline build apart from the digest-pinned base image)

Task idea
A fictional payment platform ("Meridian") derives its account snapshot from an append-only gateway event log. The shipped snapshot rebuilder (app/ledger.py) runs without errors but silently violates the written specification (docs/SPEC.md, MER-SPEC-2.3) in six interacting ways:

No deduplication - the log is at-least-once delivery; the engine replays retransmissions (R2).
File-order replay - the log is published out of order by parallel shards; the spec requires a total order by (ts, seq) (R1). Ordering changes which withdrawals/transfers get rejected, so errors cascade.
Non-idempotent account opens - late account_open retransmissions can reset an already-mutated balance instead of being ignored before state mutation (R2/R4).
Wrong fee arithmetic - max(1, round(amount * 25 / 10000)) looks close to the rule but rounds half-cent cases up instead of using integer floor division (R3/R5).
Fee-exclusive sufficiency check - transfers are accepted when the sender covers the amount but not the fee, which both changes the rejection set and drives balances negative (R4). The negative balances are the visible symptom that motivates the incident.
Missing adjustment rejection - negative back-office adjustments are applied even when they would overdraw the account; the spec requires rejection with the exact reason adjustment_would_overdraw (R6).
The agent must audit the code against the spec, fix all deviations, regenerate the snapshot, and write a reconciliation report (/app/report.json) against a read-only stale baseline.

Why this is long-horizon
There is no single command that solves it. The required chain is:

Read and internalize a normative spec, then diff a ~160-line implementation against it clause by clause (forensics, hypothesis formation).
Confirm hypotheses against the data (e.g., grep the log for repeated event_ids, check the stale snapshot for negative balances, find half-cent fee amounts).
Patch the engine - coupled fixes, where any partial fix still yields a wrong snapshot because the bugs interact through the rejection cascade (state tracking, planning).
Regenerate state and self-verify (every number in the report is checkable from the provided files).
Produce a durable deliverable: code patch + repaired SQLite state + reconciliation report.
The bugs were measured to be material: all-but-one partial fixes still fail well below the solve threshold (see tests/test_verifier_selfcheck.py and RUN_REPORT.md).

Verifier design
tests/test.sh -> tests/grade.py. Fully programmatic, no LLM judging, no trajectory inspection. It writes /logs/verifier/reward.json (and reward.txt with the overall score, plus grade_details.json for debugging):

Criterion	Weight	What it checks
functional_correctness	0.45	/app/state/ledger.db matches a hidden reference replay of data/events.ndjson: account set, currencies, exact balances.
constraint_satisfaction	0.15	rejected_events table and the four meta counters match the reference.
robustness	0.25	The agent's patched engine is executed on a held-out event log (tests/holdout_events.ndjson, never visible to the agent) and must reproduce that log's reference snapshot exactly. This is the anti-shallow check: hand-edited databases and hardcoded fixes score 0 here.
artifact_quality	0.15	/app/report.json has the four reconciliation figures, exactly correct. The expected stale baseline is recomputed by the verifier from a pristine vendored copy of the shipped engine, so grading is immune to tampering with /app/state/baseline_stale.db.
overall is the weighted sum; the task counts as solved at overall >= 0.99.

Non-brittleness choices: balances are compared as integers from SQL (no string/format matching); rejected_event_ids in the report is accepted in any order (compared as a sorted multiset); report integers are accepted as JSON ints or integral floats; every criterion is graded independently so one missing artifact cannot zero the rest.

Verifier self-check (tests/test_verifier_selfcheck.py, runnable standalone or via pytest) proves no false positives/negatives on the relevant solution space:

Scenario	overall	verdict
oracle solution	1.0000	passes
do-nothing	0.0219	fails
perfect DB + report but engine left buggy (cheat)	0.7500	fails (robustness)
all bugs fixed except dedup	0.0679	fails
all bugs fixed except ordering	0.1181	fails
all bugs fixed except fee rule	0.1180	fails
all bugs fixed except fee-in-sufficiency-check	0.1205	fails
all bugs fixed except adjustment clamp	0.1139	fails
Fairness rationale
Solvable: the spec is complete and unambiguous, the engine is ~160 lines of plain Python, the data fits in memory, and every required number is computable from provided files. The oracle (solution/solve.sh) scores 1.0.
No hidden requirements: the instruction states the deliverables, the exact report schema, the CLI contract, and that the engine will be evaluated on other spec-conforming logs.
No brittle grading: see above; semantics are graded, not formatting.
No environment traps: digest-pinned base image, stdlib only, no network, no secrets, no external services. The stale snapshot is built deterministically inside the Dockerfile by running the shipped engine.
Failure is capability-shaped: to pass, a model must do an exhaustive spec-vs-code audit (find all deviations, not just the obvious ones), reason about ordering and idempotency of event-sourced state, keep integer-arithmetic discipline, and self-verify against the data. Partial diligence scores about 0.1, and skipping the code fix in favor of patching the database caps at 0.75.
Reproduction
# Build/inspect the environment manually (optional)
docker build -t meridian-ledger-recon ./environment

# Harbor: oracle must pass (reward 1.0)
harbor run -p ./meridian-ledger-recon -a oracle

# Harbor: target model attempt (either target works)
harbor run -p ./meridian-ledger-recon -a claude-code -m claude-opus-4-7
harbor run -p ./meridian-ledger-recon -a codex -m gpt-5.5-high

# Inspect results
harbor view ./jobs
Authoring-time checks (no Docker needed, plain Python 3.11):

python tests/test_verifier_selfcheck.py
Provenance & licensing
Everything in this task is net-new, written for this exercise on 2026-06-10:

The scenario, spec (MER-SPEC-2.3), engine code, bugs, verifier, and oracle are original.
Both event logs were synthesized by the included deterministic generator (tests/gen_events.py; main log seed 20260610, holdout seed 20260613) - no real financial data, no public dataset, no benchmark ports.
Only the Python 3.11 standard library and the Docker Hub python:3.11.9-slim-bookworm base image pinned by digest are used. No other external sources.
Limitations
Single-currency deployment (USD) by design; the spec says so, so multi-currency handling is out of scope and ungraded.
The holdout log is generated by the same generator family as the main log. A spec-conforming engine passes both by construction; an engine overfit to artifacts of the main log would be caught, but a deliberately adversarial grader-aware engine is out of threat scope (agents never see tests/).
meta.events_applied for a do-nothing run is accidentally close to plausible values, which is why do-nothing scores 0.0219 rather than exactly 0; this is partial-credit noise, far below the 0.99 threshold.
