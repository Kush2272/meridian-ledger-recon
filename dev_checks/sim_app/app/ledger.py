#!/usr/bin/env python3
"""Meridian snapshot rebuilder (spec-conforming, MER-SPEC-2.3).

Replays the published gateway event log and materializes the account snapshot
database. See docs/SPEC.md for the event-log format and the snapshot schema.

Usage:
    python3 app/ledger.py rebuild --events <events.ndjson> --db <snapshot.db>
"""

import argparse
import json
import os
import sqlite3


def transfer_fee(amount_cents):
    # R5: 25 bps, floor division, minimum 1 cent. Integer arithmetic only (R3).
    return max(1, (amount_cents * 25) // 10000)


def rebuild(events_path, db_path):
    raw = []
    with open(events_path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            raw.append(json.loads(line))

    lines_read = len(raw)

    # R1: apply in ascending (ts, seq); file order is not commit order.
    raw.sort(key=lambda ev: (ev["ts"], ev["seq"]))

    balances = {}
    currencies = {}
    rejected = []  # (event_id, event_type, reason)
    seen_ids = set()
    duplicates = 0

    for ev in raw:
        eid = ev["event_id"]
        # R2: at-most-once per event_id; later occurrences are retransmissions.
        if eid in seen_ids:
            duplicates += 1
            continue
        seen_ids.add(eid)

        etype = ev["type"]

        if etype == "account_open":
            currencies[ev["account_id"]] = ev["currency"]
            balances[ev["account_id"]] = 0

        elif etype == "deposit":
            balances[ev["account_id"]] += ev["amount_cents"]

        elif etype == "withdrawal":
            acct = ev["account_id"]
            amount = ev["amount_cents"]
            if balances[acct] < amount:
                rejected.append((eid, etype, "insufficient_funds"))
            else:
                balances[acct] -= amount

        elif etype == "transfer":
            src = ev["src"]
            dst = ev["dst"]
            amount = ev["amount_cents"]
            fee = transfer_fee(amount)
            # R4: the sender must cover amount + fee, else reject with no state change.
            if balances[src] < amount + fee:
                rejected.append((eid, etype, "insufficient_funds"))
            else:
                balances[src] -= amount + fee
                balances[dst] += amount

        elif etype == "adjustment":
            balances[ev["account_id"]] += ev["delta_cents"]

        else:
            raise ValueError("unknown event type: %r" % etype)

    distinct = len(seen_ids)
    meta_counts = {
        "events_total": lines_read,
        "duplicates_ignored": duplicates,
        "rejected_count": len(rejected),
        "events_applied": distinct - len(rejected),
    }
    write_snapshot(db_path, balances, currencies, rejected, meta_counts)


def write_snapshot(db_path, balances, currencies, rejected, meta_counts):
    if os.path.exists(db_path):
        os.remove(db_path)
    parent = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(parent, exist_ok=True)

    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.executescript(
        """
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
        """
    )

    for account_id, currency in currencies.items():
        cur.execute(
            "INSERT INTO accounts (account_id, currency, balance_cents) VALUES (?, ?, ?)",
            (account_id, currency, balances[account_id]),
        )

    for event_id, event_type, reason in rejected:
        cur.execute(
            "INSERT INTO rejected_events (event_id, event_type, reason) VALUES (?, ?, ?)",
            (event_id, event_type, reason),
        )

    cur.executemany(
        "INSERT INTO meta (key, value) VALUES (?, ?)",
        [(k, str(v)) for k, v in meta_counts.items()],
    )

    con.commit()
    con.close()


def main():
    parser = argparse.ArgumentParser(description="Meridian snapshot rebuilder")
    sub = parser.add_subparsers(dest="command", required=True)

    p_rebuild = sub.add_parser("rebuild", help="replay an event log into a snapshot db")
    p_rebuild.add_argument("--events", required=True, help="path to the NDJSON event log")
    p_rebuild.add_argument("--db", required=True, help="path of the snapshot SQLite db to write")

    args = parser.parse_args()
    if args.command == "rebuild":
        rebuild(args.events, args.db)
        print("snapshot written to %s" % args.db)


if __name__ == "__main__":
    main()
