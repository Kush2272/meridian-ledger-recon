#!/usr/bin/env python3
"""Deterministic generator for Meridian gateway event logs (authoring tool).

This script is provenance material: it is how data/events.ndjson and
tests/holdout_events.ndjson were created. It is NOT shipped to the agent and is
not needed at verification time (both logs are committed artifacts).

The generator deliberately produces traffic that makes every planted spec
deviation in the seeded ledger.py materially change the outcome:
  - retransmitted events (R2 dedup),
  - bounded out-of-order publishing (R1 ordering),
  - transfer amounts in the half-cent / sub-minimum fee zones (R5 fee math),
  - transfers exactly at, or barely below, the amount+fee affordability
    boundary (R4 sufficiency check),
  - overdrawing back-office adjustments (R6 adjustment rejection),
  - retransmitted account_open events late in the file (R2 before mutation),
  - authorization holds that reserve, capture, release, and overdraw availability.

Usage:
  python3 gen_events.py --seed 20260610 --accounts 40 --traffic 2400 --dups 120 \
      --start 2026-04-01T00:00:00Z --out ../environment/data/events.ndjson
"""

import argparse
import datetime
import json
import random


def transfer_fee(amount_cents):
    return max(1, (amount_cents * 25) // 10000)


def iso(epoch):
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def parse_iso(s):
    return int(
        datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ")
        .replace(tzinfo=datetime.timezone.utc)
        .timestamp()
    )


def boundary_amount(balance):
    """Find amount such that amount + fee(amount) == balance (exact affordability)."""
    guess = balance - max(1, (balance * 25) // 10000)
    for a in range(max(1, guess - 4), guess + 5):
        if a > 0 and a + transfer_fee(a) == balance:
            return a
    return None


def generate(seed, n_accounts, n_traffic, n_dups, start_epoch, window=40):
    rng = random.Random(seed)
    accounts = ["acct_%04d" % i for i in range(1, n_accounts + 1)]
    events = []  # chronological (commit) order
    next_hold = 1

    # Opens, then seed deposits so traffic has funds to move around.
    for a in accounts:
        events.append({"type": "account_open", "account_id": a, "currency": "USD"})
    for a in accounts:
        events.append(
            {"type": "deposit", "account_id": a, "amount_cents": rng.randrange(50_000, 500_000)}
        )

    # Track true balances under correct (spec) semantics so we can craft
    # boundary cases precisely.
    bal = {a: 0 for a in accounts}
    held = {a: 0 for a in accounts}
    active_holds = {}
    for ev in events:
        if ev["type"] == "deposit":
            bal[ev["account_id"]] += ev["amount_cents"]

    def available(a):
        return bal[a] - held[a]

    def add_hold(hold_id, a, amt):
        active_holds[hold_id] = (a, amt)
        held[a] += amt

    def remove_hold(hold_id):
        a, amt = active_holds.pop(hold_id)
        held[a] -= amt

    def apply_spec(ev):
        t = ev["type"]
        if t == "deposit":
            bal[ev["account_id"]] += ev["amount_cents"]
        elif t == "withdrawal":
            a, amt = ev["account_id"], ev["amount_cents"]
            if available(a) >= amt:
                bal[a] -= amt
        elif t == "transfer":
            s, d, amt = ev["src"], ev["dst"], ev["amount_cents"]
            fee = transfer_fee(amt)
            if available(s) >= amt + fee:
                bal[s] -= amt + fee
                bal[d] += amt
        elif t == "adjustment":
            a, delta = ev["account_id"], ev["delta_cents"]
            if bal[a] + delta >= held[a]:
                bal[a] += delta
        elif t == "hold_create":
            a, amt = ev["account_id"], ev["amount_cents"]
            if available(a) >= amt:
                add_hold(ev["hold_id"], a, amt)
        elif t == "hold_capture":
            hold_id = ev["hold_id"]
            expected = (ev["account_id"], ev["amount_cents"])
            if active_holds.get(hold_id) == expected:
                a, amt = expected
                bal[a] -= amt
                remove_hold(hold_id)
        elif t == "hold_release":
            hold_id = ev["hold_id"]
            expected = (ev["account_id"], ev["amount_cents"])
            if active_holds.get(hold_id) == expected:
                remove_hold(hold_id)

    made = 0
    while made < n_traffic:
        r = rng.random()
        if r < 0.23:
            a = rng.choice(accounts)
            ev = {"type": "deposit", "account_id": a, "amount_cents": rng.randrange(200, 250_000)}
        elif r < 0.43:
            a = rng.choice(accounts)
            b = available(a)
            u = rng.random()
            if b <= 0:
                amt = rng.randrange(100, 5_000)  # certain rejection
            elif u < 0.15:
                amt = b + rng.randrange(1, 2_000)  # overdraw attempt -> reject
            elif u < 0.25:
                amt = b  # exact drain -> succeeds, leaves 0
            else:
                amt = max(1, int(b * rng.uniform(0.05, 0.6)))
            ev = {"type": "withdrawal", "account_id": a, "amount_cents": amt}
        elif r < 0.70:
            src = rng.choice(accounts)
            dst = rng.choice([a for a in accounts if a != src])
            b = available(src)
            u = rng.random()
            amt = None
            if u < 0.16:
                amt = rng.randrange(40, 400)  # fee floor is 0 -> minimum-1-cent rule bites
            elif u < 0.38:
                amt = rng.randrange(1, 60) * 400 + 200  # amount*25/10000 = x.5 exactly
            elif u < 0.50 and b > 500:
                amt = boundary_amount(b)  # exact affordability: amount + fee == balance
            elif u < 0.60 and b > 500:
                a0 = boundary_amount(b)
                if a0 is not None:
                    amt = a0 + rng.randrange(1, 3)  # just over the line -> must reject
            if amt is None:
                amt = max(1, int(max(b, 1_000) * rng.uniform(0.02, 0.5)))
            ev = {"type": "transfer", "src": src, "dst": dst, "amount_cents": amt}
        elif r < 0.82:
            a = rng.choice(accounts)
            if bal[a] > held[a] and rng.random() < 0.45:
                delta = -(available(a) + rng.randrange(1, 25_000))
            else:
                delta = rng.randrange(-150_000, 150_000)
            ev = {"type": "adjustment", "account_id": a, "delta_cents": delta}
        elif r < 0.91:
            a = rng.choice(accounts)
            hold_id = "hold_%06d" % next_hold
            next_hold += 1
            b = max(available(a), 0)
            if b > 0 and rng.random() < 0.72:
                amt = max(1, int(b * rng.uniform(0.05, 0.45)))
            else:
                amt = b + rng.randrange(1, 50_000)
            ev = {"type": "hold_create", "hold_id": hold_id, "account_id": a, "amount_cents": amt}
        elif r < 0.965:
            if active_holds and rng.random() < 0.78:
                hold_id = rng.choice(list(active_holds))
                a, amt = active_holds[hold_id]
            else:
                hold_id = "missing_hold_%06d" % rng.randrange(1, 999999)
                a = rng.choice(accounts)
                amt = rng.randrange(1, 75_000)
            ev = {"type": "hold_capture", "hold_id": hold_id, "account_id": a, "amount_cents": amt}
        else:
            if active_holds and rng.random() < 0.82:
                hold_id = rng.choice(list(active_holds))
                a, amt = active_holds[hold_id]
            else:
                hold_id = "missing_hold_%06d" % rng.randrange(1, 999999)
                a = rng.choice(accounts)
                amt = rng.randrange(1, 75_000)
            ev = {"type": "hold_release", "hold_id": hold_id, "account_id": a, "amount_cents": amt}
        events.append(ev)
        apply_spec(ev)
        made += 1

    # Leave a deterministic tail of active holds so report generation must track
    # final hold state, not only captured/rejected events.
    for _ in range(max(3, n_accounts // 6)):
        candidates = [a for a in accounts if available(a) > 500]
        if not candidates:
            break
        a = rng.choice(candidates)
        hold_id = "hold_%06d" % next_hold
        next_hold += 1
        amt = max(1, int(available(a) * rng.uniform(0.05, 0.20)))
        ev = {"type": "hold_create", "hold_id": hold_id, "account_id": a, "amount_cents": amt}
        events.append(ev)
        apply_spec(ev)

    # Assign commit metadata: seq strictly increasing; ts non-decreasing with
    # deliberate ties (exercises the seq tie-break in R1).
    t = start_epoch
    for seq, ev in enumerate(events, start=1):
        if seq > 1 and rng.random() < 0.22:
            pass  # share the previous timestamp
        else:
            t += rng.randrange(1, 900)
        ev["seq"] = seq
        ev["ts"] = iso(t)
        ev["event_id"] = "evt_%012x" % rng.getrandbits(48)

    # Bounded out-of-order publishing: displace each line within `window`.
    out = list(events)
    for i in range(len(out)):
        j = rng.randrange(i, min(i + window, len(out)))
        out[i], out[j] = out[j], out[i]

    # At-least-once delivery: retransmit some events later in the file, including
    # account_open records after traffic has changed balances.
    open_dups = min(len(accounts), max(1, n_dups // 8))
    open_events = [ev for ev in out if ev["type"] == "account_open"]
    for dup in rng.sample(open_events, open_dups):
        pos = rng.randrange(len(out) // 2, len(out) + 1)
        out.insert(pos, dup)

    candidates = [k for k, ev in enumerate(out) if ev["type"] != "account_open"]
    for _ in range(max(0, n_dups - open_dups)):
        k = rng.choice(candidates)
        dup = out[k]
        pos = rng.randrange(k + 1, len(out) + 1)
        out.insert(pos, dup)
        candidates = [c if c < pos else c + 1 for c in candidates]

    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--accounts", type=int, required=True)
    ap.add_argument("--traffic", type=int, required=True)
    ap.add_argument("--dups", type=int, required=True)
    ap.add_argument("--start", required=True, help="ISO start, e.g. 2026-04-01T00:00:00Z")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    events = generate(args.seed, args.accounts, args.traffic, args.dups, parse_iso(args.start))
    with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
        for ev in events:
            fh.write(json.dumps(ev, sort_keys=True) + "\n")
    print("wrote %d lines to %s" % (len(events), args.out))


if __name__ == "__main__":
    main()
