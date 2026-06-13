"""Reference implementation of MER-SPEC-2.3 (verifier-side ground truth).

This module is part of the verifier; it is never visible to the agent.
It computes, from an event log, the exact snapshot state a spec-conforming
rebuilder must produce: balances, currencies, rejected events and meta counters.
"""

import json


def transfer_fee(amount_cents):
    # R5: 25 bps, floor division, minimum 1 cent, integer arithmetic only.
    return max(1, (amount_cents * 25) // 10000)


def compute(events_path):
    """Replay an event log per SPEC and return the expected snapshot state.

    Returns a dict with:
        balances:   {account_id: int}
        currencies: {account_id: str}
        rejected:   set of (event_id, event_type, reason)
        meta:       {key: int}
    """
    raw = []
    with open(events_path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                raw.append(json.loads(line))

    total = len(raw)

    # R1: total order by (ts, seq). ISO-8601 UTC sorts chronologically as a string.
    raw.sort(key=lambda ev: (ev["ts"], ev["seq"]))

    balances = {}
    currencies = {}
    rejected = set()
    seen_ids = set()
    duplicates = 0
    active_holds = {}
    held_by_account = {}
    captured_holds = set()

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
        # R2: at-most-once per event_id; retransmissions are byte-identical.
        if eid in seen_ids:
            duplicates += 1
            continue
        seen_ids.add(eid)

        etype = ev["type"]
        if etype == "account_open":
            currencies[ev["account_id"]] = ev["currency"]
            balances[ev["account_id"]] = 0
            held_by_account[ev["account_id"]] = 0
        elif etype == "deposit":
            balances[ev["account_id"]] += ev["amount_cents"]
        elif etype == "withdrawal":
            acct = ev["account_id"]
            amount = ev["amount_cents"]
            if available(acct) < amount:
                rejected.add((eid, etype, "insufficient_funds"))
            else:
                balances[acct] -= amount
        elif etype == "transfer":
            src, dst = ev["src"], ev["dst"]
            amount = ev["amount_cents"]
            fee = transfer_fee(amount)
            # R4: sender must cover amount + fee.
            if available(src) < amount + fee:
                rejected.add((eid, etype, "insufficient_funds"))
            else:
                balances[src] -= amount + fee
                balances[dst] += amount
        elif etype == "adjustment":
            acct = ev["account_id"]
            delta = ev["delta_cents"]
            if balances[acct] + delta < held(acct):
                rejected.add((eid, etype, "adjustment_would_overdraw"))
            else:
                balances[acct] += delta
        elif etype == "hold_create":
            acct = ev["account_id"]
            amount = ev["amount_cents"]
            if available(acct) < amount:
                rejected.add((eid, etype, "hold_insufficient_funds"))
            else:
                add_hold(ev["hold_id"], acct, amount)
        elif etype == "hold_capture":
            hold_id = ev["hold_id"]
            expected = (ev["account_id"], ev["amount_cents"])
            if active_holds.get(hold_id) != expected:
                rejected.add((eid, etype, "unknown_hold"))
            else:
                acct, amount = expected
                balances[acct] -= amount
                remove_hold(hold_id)
                captured_holds.add(hold_id)
        elif etype == "hold_release":
            hold_id = ev["hold_id"]
            expected = (ev["account_id"], ev["amount_cents"])
            if active_holds.get(hold_id) != expected:
                rejected.add((eid, etype, "unknown_hold"))
            else:
                remove_hold(hold_id)
        else:
            raise ValueError("unknown event type: %r" % etype)

    distinct = len(seen_ids)
    meta = {
        "events_total": total,
        "duplicates_ignored": duplicates,
        "rejected_count": len(rejected),
        "events_applied": distinct - len(rejected),
    }
    return {
        "balances": balances,
        "currencies": currencies,
        "rejected": rejected,
        "meta": meta,
        "captured_holds": captured_holds,
        "open_holds": set(active_holds),
    }
