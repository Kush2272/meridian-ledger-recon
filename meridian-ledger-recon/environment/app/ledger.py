#!/usr/bin/env python3
"""Meridian snapshot rebuilder.

Replays the published gateway event log and materializes the account snapshot
database. See docs/SPEC.md for the event-log format and the snapshot schema.

Usage:
    python3 app/ledger.py rebuild --events <events.ndjson> --db <snapshot.db>
"""

import argparse
import json
import os
import sqlite3
from collections import defaultdict

def transfer_fee(amount_cents):
    # Fee schedule: 25 bps, rounded down, minimum 1 cent.
    return max(1, round(amount_cents * 25 / 10_000))


def rebuild(events_path, db_path):
    balances = defaultdict(int)
    currencies = {}
    rejected = []  # (event_id, event_type, reason)
    lines_read = 0
    applied = 0

    with open(events_path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            lines_read += 1
            ev = json.loads(line)
            etype = ev["type"]

            if etype == "account_open":
                acct = ev["account_id"]
                if acct not in balances:
                    balances[acct] = 0
                    currencies[acct] = ev.get("currency", "USD")
                else:
                    balances[acct] = 0
                applied += 1

            elif etype == "deposit":
                balances[ev["account_id"]] += ev["amount_cents"]
                applied += 1

            elif etype == "withdrawal":
                acct = ev["account_id"]
                amount = ev["amount_cents"]
                if balances[acct] < amount:
                    rejected.append((ev["event_id"], etype, "insufficient_funds"))
                else:
                    balances[acct] -= amount
                    applied += 1

            elif etype == "transfer":
                src = ev["src"]
                dst = ev["dst"]
                amount = ev["amount_cents"]
                fee = transfer_fee(amount)
                if balances[src] < amount:
                    rejected.append((ev["event_id"], etype, "insufficient_funds"))
                else:
                    balances[src] -= amount + fee
                    balances[dst] += amount
                    applied += 1

            elif etype == "adjustment":
                balances[ev["account_id"]] += ev["delta_cents"]
                applied += 1

            elif etype == "hold_create":
                applied += 1

            elif etype == "hold_capture":
                balances[ev["account_id"]] -= ev["amount_cents"]
                applied += 1

            elif etype == "hold_release":
                applied += 1

            else:
                raise ValueError("unknown event type: %r" % etype)

    write_snapshot(db_path, balances, currencies, rejected, lines_read, applied)


def write_snapshot(db_path, balances, currencies, rejected, lines_read, applied):
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
            "INSERT OR REPLACE INTO rejected_events (event_id, event_type, reason) VALUES (?, ?, ?)",
            (event_id, event_type, reason),
        )

    meta = [
        ("events_total", str(lines_read)),
        ("duplicates_ignored", "0"),  # TODO: wire up redelivery tracking
        ("rejected_count", str(len(rejected))),
        ("events_applied", str(applied)),
    ]
    cur.executemany("INSERT INTO meta (key, value) VALUES (?, ?)", meta)

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
