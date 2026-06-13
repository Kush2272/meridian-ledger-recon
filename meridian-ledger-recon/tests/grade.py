#!/usr/bin/env python3
"""Verifier for meridian-ledger-recon.

Programmatic grading, no LLM judging. Four criteria:

  functional_correctness (0.45)  regenerated /app/state/ledger.db matches the
                                 spec-conforming replay of data/events.ndjson
                                 (account set, currencies, exact balances).
  constraint_satisfaction (0.15) rejected_events table and meta counters match
                                 the spec-conforming replay.
  robustness (0.25)              the agent's patched engine is run on a HELD-OUT
                                 event log it has never seen and must reproduce
                                 the reference snapshot exactly. This rejects
                                 hand-edited databases and hardcoded fixes.
  artifact_quality (0.15)        /app/report.json contains the four required
                                 reconciliation figures with exact values.

Writes reward.json (and reward.txt with the overall score) into the logs dir.
A run is considered solved at overall >= 0.99.

Paths are parameterized via --app-dir / --logs-dir so the grader can also be
exercised outside the container during task authoring.
"""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import reference_impl  # noqa: E402
import buggy_baseline  # noqa: E402  (pristine copy of the shipped engine)

WEIGHTS = {
    "functional_correctness": 0.45,
    "constraint_satisfaction": 0.15,
    "robustness": 0.25,
    "artifact_quality": 0.15,
}

META_KEYS = ["events_total", "duplicates_ignored", "rejected_count", "events_applied"]

EXPECTED_SCHEMA = {
    "accounts": [
        ("account_id", "TEXT", 1),
        ("currency", "TEXT", 0),
        ("balance_cents", "INTEGER", 0),
    ],
    "rejected_events": [
        ("event_id", "TEXT", 1),
        ("event_type", "TEXT", 0),
        ("reason", "TEXT", 0),
    ],
    "meta": [
        ("key", "TEXT", 1),
        ("value", "TEXT", 0),
    ],
}


def read_snapshot(db_path):
    """Return (balances, currencies, rejected_set, meta, schema_ok) or None if unreadable."""
    if not os.path.isfile(db_path):
        return None
    try:
        con = sqlite3.connect(db_path)
        schema_ok = validate_schema(con)
        bal, cur_map = {}, {}
        for acct, currency, cents in con.execute(
            "SELECT account_id, currency, balance_cents FROM accounts"
        ):
            bal[acct] = int(cents)
            cur_map[acct] = currency
        rejected = set(
            (str(e), str(t), str(r))
            for e, t, r in con.execute("SELECT event_id, event_type, reason FROM rejected_events")
        )
        meta = {}
        for k, v in con.execute("SELECT key, value FROM meta"):
            try:
                meta[str(k)] = int(v)
            except (TypeError, ValueError):
                meta[str(k)] = None
        con.close()
        return bal, cur_map, rejected, meta, schema_ok
    except sqlite3.Error:
        return None


def validate_schema(con):
    tables = {
        name
        for (name,) in con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    if tables != set(EXPECTED_SCHEMA):
        return False

    for table, expected_cols in EXPECTED_SCHEMA.items():
        rows = con.execute("PRAGMA table_info(%s)" % table).fetchall()
        got_cols = [(row[1], str(row[2]).upper(), int(row[5])) for row in rows]
        if got_cols != expected_cols:
            return False
    return True


def balance_fraction(expected, got_bal, got_cur, exp_cur):
    """Fraction of accounts (over the union of ids) with exact balance+currency."""
    ids = set(expected) | set(got_bal)
    if not ids:
        return 0.0
    ok = sum(
        1
        for a in ids
        if a in expected
        and a in got_bal
        and expected[a] == got_bal[a]
        and exp_cur.get(a) == got_cur.get(a)
    )
    return ok / len(ids)


def score_snapshot(exp, snap):
    """Returns (functional, constraint, detail-dict) for one snapshot vs reference."""
    detail = {}
    if snap is None:
        return 0.0, 0.0, {"error": "snapshot missing or unreadable"}
    bal, cur_map, rejected, meta, schema_ok = snap

    frac = balance_fraction(exp["balances"], bal, cur_map, exp["currencies"])
    exact_accounts = (
        set(bal) == set(exp["balances"])
        and frac == 1.0
        and schema_ok
    )
    functional = 1.0 if exact_accounts else 0.5 * frac
    detail["accounts_exact"] = exact_accounts
    detail["balance_match_fraction"] = round(frac, 4)
    detail["schema_exact"] = schema_ok

    if rejected == exp["rejected"]:
        rej_score = 1.0
    else:
        union = len(rejected | exp["rejected"])
        jac = (len(rejected & exp["rejected"]) / union) if union else 0.0
        rej_score = 0.5 * jac
    detail["rejected_exact"] = rejected == exp["rejected"]

    meta_hits = sum(1 for k in META_KEYS if meta.get(k) == exp["meta"][k])
    meta_keys_exact = set(meta) == set(META_KEYS)
    if not meta_keys_exact:
        meta_hits = 0
    meta_score = meta_hits / len(META_KEYS)
    detail["meta_keys_correct"] = meta_hits
    detail["meta_keys_exact"] = meta_keys_exact

    constraint = 0.6 * rej_score + 0.4 * meta_score
    return functional, constraint, detail


def as_int(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v == int(v):
        return int(v)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--app-dir", default=os.environ.get("APP_DIR", "/app"))
    ap.add_argument("--logs-dir", default=os.environ.get("VERIFIER_LOGS_DIR", "/logs/verifier"))
    args = ap.parse_args()

    test_dir = os.path.dirname(os.path.abspath(__file__))
    app = args.app_dir
    os.makedirs(args.logs_dir, exist_ok=True)

    scores = {k: 0.0 for k in WEIGHTS}
    details = {}

    events_path = os.path.join(app, "data", "events.ndjson")
    exp_main = reference_impl.compute(events_path)

    # --- functional_correctness + constraint_satisfaction: main snapshot -----
    try:
        snap = read_snapshot(os.path.join(app, "state", "ledger.db"))
        functional, constraint, d = score_snapshot(exp_main, snap)
        scores["functional_correctness"] = functional
        scores["constraint_satisfaction"] = constraint
        details["main_snapshot"] = d
    except Exception as e:  # noqa: BLE001 - grading must never crash
        details["main_snapshot"] = {"error": repr(e)}

    # --- robustness: run the agent's engine on a held-out log ----------------
    try:
        holdout = os.path.join(test_dir, "holdout_events.ndjson")
        exp_hold = reference_impl.compute(holdout)
        with tempfile.TemporaryDirectory() as td:
            out_db = os.path.join(td, "holdout.db")
            proc = subprocess.run(
                [
                    sys.executable,
                    os.path.join(app, "app", "ledger.py"),
                    "rebuild",
                    "--events",
                    holdout,
                    "--db",
                    out_db,
                ],
                capture_output=True,
                text=True,
                timeout=180,
            )
            if proc.returncode != 0:
                details["holdout"] = {
                    "error": "engine exited nonzero",
                    "stderr_tail": proc.stderr[-2000:],
                }
            else:
                snap_h = read_snapshot(out_db)
                if snap_h is None:
                    details["holdout"] = {"error": "engine produced no readable snapshot"}
                else:
                    bal, cur_map, rejected, meta, schema_ok = snap_h
                    frac = balance_fraction(
                        exp_hold["balances"], bal, cur_map, exp_hold["currencies"]
                    )
                    exact = (
                        set(bal) == set(exp_hold["balances"])
                        and frac == 1.0
                        and schema_ok
                        and rejected == exp_hold["rejected"]
                        and all(meta.get(k) == exp_hold["meta"][k] for k in META_KEYS)
                        and set(meta) == set(META_KEYS)
                    )
                    scores["robustness"] = 1.0 if exact else 0.25 * frac
                    details["holdout"] = {
                        "exact": exact,
                        "balance_match_fraction": round(frac, 4),
                        "rejected_exact": rejected == exp_hold["rejected"],
                        "schema_exact": schema_ok,
                    }
    except Exception as e:  # noqa: BLE001
        details["holdout"] = {"error": repr(e)}

    # --- artifact_quality: report.json ---------------------------------------
    try:
        # Recompute the stale baseline independently with a pristine copy of the
        # originally shipped engine, so grading is immune to tampering with
        # /app/state/baseline_stale.db.
        with tempfile.TemporaryDirectory() as td:
            stale_db = os.path.join(td, "stale.db")
            buggy_baseline.rebuild(events_path, stale_db)
            stale = read_snapshot(stale_db)
        stale_bal = stale[0]
        exp_bal = exp_main["balances"]
        ids = set(stale_bal) | set(exp_bal)
        expected_report = {
            "duplicate_events_ignored": exp_main["meta"]["duplicates_ignored"],
            "rejected_event_ids": sorted(e for (e, _, _) in exp_main["rejected"]),
            "accounts_with_corrected_balance": sum(
                1 for a in ids if stale_bal.get(a) != exp_bal.get(a)
            ),
            "total_absolute_drift_cents": sum(
                abs(exp_bal.get(a, 0) - stale_bal.get(a, 0)) for a in ids
            ),
            "captured_hold_ids": sorted(exp_main["captured_holds"]),
            "open_hold_ids": sorted(exp_main["open_holds"]),
        }
        details["expected_report"] = {
            k: (v if not isinstance(v, list) else "<%d ids>" % len(v))
            for k, v in expected_report.items()
        }

        report_path = os.path.join(app, "report.json")
        art = 0.0
        rep_detail = {}
        if os.path.isfile(report_path):
            with open(report_path, "r", encoding="utf-8") as fh:
                rep = json.load(fh)
            if isinstance(rep, dict):
                for key in (
                    "duplicate_events_ignored",
                    "accounts_with_corrected_balance",
                    "total_absolute_drift_cents",
                ):
                    ok = as_int(rep.get(key)) == expected_report[key]
                    rep_detail[key] = ok
                    art += (1 / 6) if ok else 0.0
                for key in ("rejected_event_ids", "captured_hold_ids", "open_hold_ids"):
                    got_ids = rep.get(key)
                    ids_ok = isinstance(got_ids, list) and sorted(
                        str(x) for x in got_ids
                    ) == expected_report[key]
                    rep_detail[key] = ids_ok
                    art += (1 / 6) if ids_ok else 0.0
        else:
            rep_detail["error"] = "report.json missing"
        scores["artifact_quality"] = art
        details["report"] = rep_detail
    except Exception as e:  # noqa: BLE001
        details["report"] = {"error": repr(e)}

    overall = round(sum(WEIGHTS[k] * scores[k] for k in WEIGHTS), 4)
    reward = {"overall": overall}
    reward.update({k: round(v, 4) for k, v in scores.items()})

    with open(os.path.join(args.logs_dir, "reward.json"), "w", encoding="utf-8") as fh:
        json.dump(reward, fh, indent=2)
    with open(os.path.join(args.logs_dir, "reward.txt"), "w", encoding="utf-8") as fh:
        fh.write("%s\n" % overall)
    with open(os.path.join(args.logs_dir, "grade_details.json"), "w", encoding="utf-8") as fh:
        json.dump(details, fh, indent=2, default=str)

    print(json.dumps(reward, indent=2))
    print("solved:", overall >= 0.99)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001 - always leave a reward file behind
        logs = os.environ.get("VERIFIER_LOGS_DIR", "/logs/verifier")
        for a, v in zip(sys.argv[1:], sys.argv[2:]):
            if a == "--logs-dir":
                logs = v
        try:
            os.makedirs(logs, exist_ok=True)
            zero = {"overall": 0.0}
            zero.update({k: 0.0 for k in WEIGHTS})
            zero["grader_error"] = repr(e)
            with open(os.path.join(logs, "reward.json"), "w", encoding="utf-8") as fh:
                json.dump(zero, fh, indent=2)
            with open(os.path.join(logs, "reward.txt"), "w", encoding="utf-8") as fh:
                fh.write("0.0\n")
        finally:
            raise
