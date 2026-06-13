#!/usr/bin/env python3
"""Oracle report generator: reconcile the corrected snapshot against the
read-only stale baseline and write /app/report.json (see instruction.md)."""

import argparse
import json
import os
import sqlite3


def read_balances(db_path):
    con = sqlite3.connect(db_path)
    bal = {a: int(c) for a, c in con.execute("SELECT account_id, balance_cents FROM accounts")}
    con.close()
    return bal


def transfer_fee(amount_cents):
    return max(1, (amount_cents * 25) // 10000)


def replay_holds(events_path):
    raw = []
    with open(events_path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                raw.append(json.loads(line))
    raw.sort(key=lambda ev: (ev["ts"], ev["seq"]))

    balances = {}
    seen = set()
    active_holds = {}
    held_by_account = {}
    captured = set()

    def held(acct):
        return held_by_account.get(acct, 0)

    def available(acct):
        return balances[acct] - held(acct)

    def add_hold(hold_id, acct, amount):
        active_holds[hold_id] = (acct, amount)
        held_by_account[acct] = held(acct) + amount

    def remove_hold(hold_id):
        acct, amount = active_holds.pop(hold_id)
        held_by_account[acct] = held(acct) - amount

    for ev in raw:
        eid = ev["event_id"]
        if eid in seen:
            continue
        seen.add(eid)

        etype = ev["type"]
        if etype == "account_open":
            balances[ev["account_id"]] = 0
            held_by_account[ev["account_id"]] = 0
        elif etype == "deposit":
            balances[ev["account_id"]] += ev["amount_cents"]
        elif etype == "withdrawal":
            acct = ev["account_id"]
            amount = ev["amount_cents"]
            if available(acct) >= amount:
                balances[acct] -= amount
        elif etype == "transfer":
            src = ev["src"]
            dst = ev["dst"]
            amount = ev["amount_cents"]
            fee = transfer_fee(amount)
            if available(src) >= amount + fee:
                balances[src] -= amount + fee
                balances[dst] += amount
        elif etype == "adjustment":
            acct = ev["account_id"]
            delta = ev["delta_cents"]
            if balances[acct] + delta >= held(acct):
                balances[acct] += delta
        elif etype == "hold_create":
            acct = ev["account_id"]
            amount = ev["amount_cents"]
            if available(acct) >= amount:
                add_hold(ev["hold_id"], acct, amount)
        elif etype == "hold_capture":
            hold_id = ev["hold_id"]
            expected = (ev["account_id"], ev["amount_cents"])
            if active_holds.get(hold_id) == expected:
                acct, amount = expected
                balances[acct] -= amount
                remove_hold(hold_id)
                captured.add(hold_id)
        elif etype == "hold_release":
            hold_id = ev["hold_id"]
            expected = (ev["account_id"], ev["amount_cents"])
            if active_holds.get(hold_id) == expected:
                remove_hold(hold_id)
        else:
            raise ValueError("unknown event type: %r" % etype)

    return sorted(captured), sorted(active_holds)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--app-dir", default=os.environ.get("APP_DIR", "/app"))
    args = ap.parse_args()
    app = args.app_dir

    new_db = os.path.join(app, "state", "ledger.db")
    stale_db = os.path.join(app, "state", "baseline_stale.db")
    events_path = os.path.join(app, "data", "events.ndjson")

    new_bal = read_balances(new_db)
    stale_bal = read_balances(stale_db)

    con = sqlite3.connect(new_db)
    rejected_ids = sorted(e for (e,) in con.execute("SELECT event_id FROM rejected_events"))
    meta = {k: int(v) for k, v in con.execute("SELECT key, value FROM meta")}
    con.close()

    ids = set(new_bal) | set(stale_bal)
    captured_hold_ids, open_hold_ids = replay_holds(events_path)
    report = {
        "duplicate_events_ignored": meta["duplicates_ignored"],
        "rejected_event_ids": rejected_ids,
        "accounts_with_corrected_balance": sum(
            1 for a in ids if stale_bal.get(a) != new_bal.get(a)
        ),
        "total_absolute_drift_cents": sum(
            abs(new_bal.get(a, 0) - stale_bal.get(a, 0)) for a in ids
        ),
        "captured_hold_ids": captured_hold_ids,
        "open_hold_ids": open_hold_ids,
    }

    out = os.path.join(app, "report.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    print("wrote %s" % out)


if __name__ == "__main__":
    main()
