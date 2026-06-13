# Meridian Ledger - Gateway Event Log & Snapshot Specification

**Document:** MER-SPEC-2.3 (normative)
**Applies to:** the snapshot rebuilder (`app/ledger.py`) and any downstream consumer of the
gateway event log.

This document is the single source of truth for how the account snapshot must be derived
from the gateway event log. Where an implementation disagrees with this document, the
implementation is wrong.

---

## 1. System overview

The Meridian payment gateway commits financial events to an internal journal and publishes
them to consumers as an append-only NDJSON file (one JSON object per line). The snapshot
rebuilder replays the published log and materializes the resulting account state into a
SQLite database ("the snapshot").

Two properties of the publishing pipeline matter to every consumer:

1. **Delivery is at-least-once.** The publisher may retransmit an event. Retransmissions
   are byte-identical copies of the original event (same `event_id`, same payload).
2. **File order is not commit order.** Events are published by parallel shards, so the
   line order of the NDJSON file is NOT guaranteed to match the order in which the gateway
   committed the events. The authoritative commit order is defined in Section 3 (R1).

## 2. Event envelope

Every line of the log is a JSON object with at least the following fields:

| Field | Type | Meaning |
|---|---|---|
| `event_id` | string | Globally unique identifier of the *logical* event. Retransmissions reuse it. |
| `seq` | int | Gateway commit sequence number. Globally unique across the log. Strictly increasing in commit order. |
| `ts` | string | Commit timestamp, ISO-8601 UTC with second precision (e.g. `2026-04-07T13:05:22Z`). Multiple events may share a timestamp. |
| `type` | string | One of: `account_open`, `deposit`, `withdrawal`, `transfer`, `adjustment`, `hold_create`, `hold_capture`, `hold_release`. |

Type-specific fields:

- `account_open`: `account_id` (string), `currency` (string; always `"USD"` in this deployment).
- `deposit`: `account_id`, `amount_cents` (int, > 0).
- `withdrawal`: `account_id`, `amount_cents` (int, > 0).
- `transfer`: `src` (string), `dst` (string), `amount_cents` (int, > 0).
- `adjustment`: `account_id`, `delta_cents` (int, may be negative).
- `hold_create`: `hold_id` (string), `account_id`, `amount_cents` (int, > 0).
- `hold_capture`: `hold_id`, `account_id`, `amount_cents` (int, > 0).
- `hold_release`: `hold_id`, `account_id`, `amount_cents` (int, > 0).

## 3. Processing requirements (normative)

### R1 - Ordering

Events MUST be applied in ascending order of the pair **(`ts`, `seq`)**: primary key is the
timestamp string (ISO-8601 UTC sorts chronologically as a string), ties are broken by `seq`.
Because `seq` is globally unique, this ordering is total.

The line order of the log file MUST NOT be used as the application order.

### R2 - Deduplication

Each `event_id` MUST be processed at most once. After the first occurrence of an
`event_id` has been processed, every further occurrence MUST be ignored (it is a
retransmission). The rebuilder MUST count ignored retransmissions and record the count in
the snapshot metadata (`duplicates_ignored`, Section 5).

### R3 - Integer arithmetic

All monetary values are integer minor units (cents). Implementations MUST perform all
monetary computation, including fees, in exact integer arithmetic. Floating-point
arithmetic MUST NOT be used for any monetary quantity.

### R4 - Event semantics

Applied in (`ts`, `seq`) order, after deduplication:

For the purpose of spend checks, `available(account_id)` is
`balance(account_id) - sum(active holds for account_id)`. Holds reserve funds but do not
change `balance_cents` until they are captured.

- **`account_open`** - registers the account with the given currency and an opening balance
  of 0 cents.
- **`deposit`** - `balance(account_id) += amount_cents`. Always succeeds.
- **`withdrawal`** - if `available(account_id) < amount_cents`, the event MUST be
  **rejected** with reason `insufficient_funds` (recorded per Section 5, no state
  change). Otherwise `balance(account_id) -= amount_cents`.
- **`transfer`** - moves `amount_cents` from `src` to `dst` and charges the sender the fee
  defined in R5. Let `fee = transfer_fee(amount_cents)`. If
  `available(src) < amount_cents + fee`, the event MUST be **rejected** with reason
  `insufficient_funds` (no state change to either account). Otherwise:
  `balance(src) -= amount_cents + fee` and `balance(dst) += amount_cents`.
  Fee proceeds accrue to the platform's revenue system, which is outside the scope of this
  snapshot; they simply leave the sender's balance.
- **`adjustment`** - if `balance(account_id) + delta_cents < sum(active holds for account_id)`, the event MUST be
  **rejected** with reason `adjustment_would_overdraw` (recorded per Section 5, no state
  change). Otherwise `balance(account_id) += delta_cents`.
- **`hold_create`** - reserves `amount_cents` on `account_id` under `hold_id`. If
  `available(account_id) < amount_cents`, the event MUST be **rejected** with reason
  `hold_insufficient_funds` (no state change). Otherwise the hold becomes active. Active
  holds reduce availability but do not change `balance_cents`.
- **`hold_capture`** - finalizes an active hold. The event MUST match an active hold with
  the same `hold_id`, `account_id`, and `amount_cents`. If no such active hold exists,
  the event MUST be **rejected** with reason `unknown_hold` (no state change). Otherwise
  `balance(account_id) -= amount_cents` and the hold is removed from the active set.
- **`hold_release`** - cancels an active hold. The event MUST match an active hold with
  the same `hold_id`, `account_id`, and `amount_cents`. If no such active hold exists,
  the event MUST be **rejected** with reason `unknown_hold` (no state change). Otherwise
  the hold is removed from the active set with no balance change.

A balance may never go below its active held amount as the result of a `withdrawal`,
`transfer`, or `adjustment`. Withdrawals and transfers that would overdraw available funds
use the R4 `insufficient_funds` rejection. Adjustments that would overdraw available funds
use the `adjustment_would_overdraw` rejection.

### R5 - Transfer fee schedule

The fee for a transfer of `amount_cents` is **25 basis points, rounded down, with a minimum
of 1 cent**, computed in integer arithmetic:

```text
transfer_fee(amount_cents) = max(1, (amount_cents * 25) // 10000)
```

where `//` is floor division. Examples: `transfer_fee(40) = 1` (minimum applies),
`transfer_fee(600) = 1` (1.5 rounds down), `transfer_fee(2000) = 5`,
`transfer_fee(123456) = 308`.

### R6 - Adjustment clamping

An `adjustment` event carries a `delta_cents` field (positive or negative). The adjusted
balance MUST NOT fall below the account's active held amount. If
`balance + delta_cents < active_held_amount`, the adjustment is **rejected** with reason
`adjustment_would_overdraw` and the balance is unchanged.

Note: this clamping applies only to adjustments. Withdrawals and transfers that would
overdraw available funds are handled by R4.

### R7 - Rejection recording

Every rejected event MUST be recorded exactly once in the `rejected_events` table
(Section 5) with its `event_id`, its `type`, and the reason string. Retransmissions of a
rejected event are deduplicated like any other event (R2) and are not recorded again.

## 4. Log guarantees (what consumers MAY assume)

- Every event that references an account is preceded - in (`ts`, `seq`) order - by that
  account's `account_open`.
- `amount_cents` is always positive; `delta_cents` may have either sign.
- All accounts use currency `USD`; there are no cross-currency transfers.
- The log is finite and fits comfortably in memory.
- `event_id` collisions between *different* logical events do not occur.

## 5. Snapshot schema (normative)

The snapshot is a SQLite database with exactly this schema:

```sql
CREATE TABLE accounts (
    account_id     TEXT PRIMARY KEY,
    currency       TEXT NOT NULL,
    balance_cents  INTEGER NOT NULL
);

CREATE TABLE rejected_events (
    event_id   TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    reason     TEXT NOT NULL
);

CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
```

The `meta` table MUST contain exactly these keys (values stored as decimal strings):

| Key | Definition |
|---|---|
| `events_total` | Number of event lines read from the log file (including retransmissions). |
| `duplicates_ignored` | Number of lines ignored as retransmissions (R2). |
| `rejected_count` | Number of distinct events rejected (R4/R6). |
| `events_applied` | Number of distinct events successfully applied = distinct events - `rejected_count`. |

`accounts` MUST contain one row per distinct opened account, with the final balance after
the full replay.

## 6. Rebuilder command-line contract

The rebuilder MUST be invocable as:

```bash
python3 app/ledger.py rebuild --events <path-to-ndjson> --db <output-sqlite-path>
```

It MUST work for **any** event log that satisfies this specification, not only the log
shipped with a particular deployment. It MUST overwrite the output database if it exists.
