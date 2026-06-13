"""Authoring-time check: each planted bug must materially change the outcome,
and the oracle engine must agree exactly with the verifier reference."""
import json, sys, os, sqlite3, tempfile

TASK = os.path.join(os.path.dirname(__file__), "..", "meridian-ledger-recon")
sys.path.insert(0, os.path.join(TASK, "tests"))
sys.path.insert(0, os.path.join(TASK, "solution"))
sys.path.insert(0, os.path.join(TASK, "environment", "app"))

import reference_impl

EVENTS = os.path.join(TASK, "environment", "data", "events.ndjson")
HOLDOUT = os.path.join(TASK, "tests", "holdout_events.ndjson")


def variant(events_path, dedup, sort, fee_mode, check_mode):
    """Parametrized engine: each flag toggles one bug between buggy/correct."""
    raw = []
    with open(events_path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                raw.append(json.loads(line))
    if sort:
        raw.sort(key=lambda ev: (ev["ts"], ev["seq"]))
    balances, currencies, rejected, seen = {}, {}, set(), set()
    dup_count = 0
    for ev in raw:
        if dedup:
            if ev["event_id"] in seen:
                dup_count += 1
                continue
            seen.add(ev["event_id"])
        t = ev["type"]
        if t == "account_open":
            currencies[ev["account_id"]] = ev["currency"]
            balances.setdefault(ev["account_id"], 0)
        elif t == "deposit":
            balances[ev["account_id"]] = balances.get(ev["account_id"], 0) + ev["amount_cents"]
        elif t == "withdrawal":
            a, amt = ev["account_id"], ev["amount_cents"]
            if balances.get(a, 0) < amt:
                rejected.add((ev["event_id"], t, "insufficient_funds"))
            else:
                balances[a] -= amt
        elif t == "transfer":
            s, d, amt = ev["src"], ev["dst"], ev["amount_cents"]
            if fee_mode == "correct":
                fee = max(1, (amt * 25) // 10000)
            else:
                fee = round(amt * 0.0025)
            need = amt + fee if check_mode == "correct" else amt
            if balances.get(s, 0) < need:
                rejected.add((ev["event_id"], t, "insufficient_funds"))
            else:
                balances[s] = balances.get(s, 0) - (amt + fee)
                balances[d] = balances.get(d, 0) + amt
        elif t == "adjustment":
            balances[ev["account_id"]] = balances.get(ev["account_id"], 0) + ev["delta_cents"]
    return balances, rejected, dup_count


def compare(tag, got_bal, got_rej, exp):
    diffs = [a for a in exp["balances"] if exp["balances"][a] != got_bal.get(a)]
    drift = sum(abs(exp["balances"][a] - got_bal.get(a, 0)) for a in exp["balances"])
    rej_sym = len(exp["rejected"] ^ got_rej)
    print(f"{tag:48s} accounts_wrong={len(diffs):3d}  drift_cents={drift:9d}  rejected_sym_diff={rej_sym:3d}")
    return len(diffs), drift, rej_sym


for path, name in [(EVENTS, "MAIN"), (HOLDOUT, "HOLDOUT")]:
    exp = reference_impl.compute(path)
    print(f"== {name}: distinct={len(exp['balances'])} accounts, meta={exp['meta']}")
    fully_buggy = variant(path, dedup=False, sort=False, fee_mode="buggy", check_mode="buggy")
    compare("fully buggy (shipped engine)", fully_buggy[0], fully_buggy[1], exp)
    # one bug fixed at a time -> must STILL mismatch (each remaining bug material)
    compare("only dedup fixed", *variant(path, True, False, "buggy", "buggy")[:2], exp)
    compare("only ordering fixed", *variant(path, False, True, "buggy", "buggy")[:2], exp)
    compare("only fee fixed", *variant(path, False, False, "correct", "buggy")[:2], exp)
    compare("only sufficiency fixed", *variant(path, False, False, "buggy", "correct")[:2], exp)
    # three fixed, one remaining buggy -> must STILL mismatch (no bug is masked)
    compare("all but dedup fixed", *variant(path, False, True, "correct", "correct")[:2], exp)
    compare("all but ordering fixed", *variant(path, True, False, "correct", "correct")[:2], exp)
    compare("all but fee fixed", *variant(path, True, True, "buggy", "correct")[:2], exp)
    compare("all but sufficiency fixed", *variant(path, True, True, "correct", "buggy")[:2], exp)
    # all fixed -> must match exactly
    n, d, r = compare("all fixed (sanity: must be zeros)", *variant(path, True, True, "correct", "correct")[:2], exp)
    assert n == 0 and d == 0 and r == 0, "variant(all-correct) disagrees with reference!"
    print()

# Cross-check the oracle's fixed engine against the reference via sqlite output.
import ledger_fixed
for path, name in [(EVENTS, "MAIN"), (HOLDOUT, "HOLDOUT")]:
    exp = reference_impl.compute(path)
    with tempfile.TemporaryDirectory() as td:
        db = os.path.join(td, "snap.db")
        ledger_fixed.rebuild(path, db)
        con = sqlite3.connect(db)
        bal = {r[0]: r[2] for r in con.execute("SELECT account_id, currency, balance_cents FROM accounts")}
        rej = set(con.execute("SELECT event_id, event_type, reason FROM rejected_events"))
        meta = {k: int(v) for k, v in con.execute("SELECT key, value FROM meta")}
        con.close()
    assert bal == exp["balances"], f"{name}: oracle balances mismatch"
    assert rej == exp["rejected"], f"{name}: oracle rejected mismatch"
    assert meta == exp["meta"], f"{name}: oracle meta mismatch"
    print(f"{name}: oracle ledger_fixed.py == reference  OK  (meta={meta})")
