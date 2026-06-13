#!/usr/bin/env python3
"""Verifier self-check for meridian-ledger-recon (authoring/CI tool).

Proves the grader has no false positives or negatives on the solution space
that matters:

  1. oracle solution                      -> overall == 1.0   (no false negative)
  2. do-nothing                           -> overall  < 0.10
  3. hand-edited-DB cheat (no code fix)   -> overall  < 0.99  (robustness holdout catches it)
  4. every all-but-one partial fix        -> overall  < 0.99  (each bug independently fatal)

Run:  python tests/test_verifier_selfcheck.py
(pytest-compatible: functions are named test_*.)
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

TESTS = os.path.dirname(os.path.abspath(__file__))
TASK = os.path.dirname(TESTS)
ENV = os.path.join(TASK, "environment")
SOL = os.path.join(TASK, "solution")

FIXED_SRC = open(os.path.join(SOL, "ledger_fixed.py"), encoding="utf-8").read()

# Targeted regressions applied to the fixed engine: each reintroduces exactly
# one of the planted bugs, simulating an agent that found all but one.
REGRESSIONS = {
    "missing_dedup": [
        ("        if eid in seen_ids:", "        if False:"),
        # an agent that missed dedup would keep the defensive OR REPLACE the
        # shipped engine used, instead of crashing on duplicate rejections
        (
            "INSERT INTO rejected_events",
            "INSERT OR REPLACE INTO rejected_events",
        ),
    ],
    "missing_ordering": [
        ('    raw.sort(key=lambda ev: (ev["ts"], ev["seq"]))', "    pass"),
        # an agent that missed ordering would tolerate deposits arriving (in
        # file order) before the account_open, like the shipped engine did
        ("    balances = {}", '    balances = __import__("collections").defaultdict(int)'),
        (
            '            balances[ev["account_id"]] = 0',
            '            balances.setdefault(ev["account_id"], 0)',
        ),
    ],
    "missing_fee_rule": [
        (
            "    return max(1, (amount_cents * 25) // 10000)",
            "    return max(1, round(amount_cents * 25 / 10_000))",
        ),
    ],
    "missing_fee_in_check": [
        (
            "            if available(src) < amount + fee:",
            "            if available(src) < amount:",
        ),
    ],
    "missing_adjustment_clamp": [
        (
            '            acct = ev["account_id"]\n'
            '            delta = ev["delta_cents"]\n'
            "            if balances[acct] + delta < held(acct):\n"
            '                rejected.append((eid, etype, "adjustment_would_overdraw"))\n'
            "            else:\n"
            "                balances[acct] += delta",
            '            balances[ev["account_id"]] += ev["delta_cents"]',
        ),
    ],
    "missing_hold_semantics": [
        (
            '        elif etype == "hold_create":\n'
            '            acct = ev["account_id"]\n'
            '            amount = ev["amount_cents"]\n'
            "            if available(acct) < amount:\n"
            '                rejected.append((eid, etype, "hold_insufficient_funds"))\n'
            "            else:\n"
            '                add_hold(ev["hold_id"], acct, amount)\n'
            "\n"
            '        elif etype == "hold_capture":\n'
            '            hold_id = ev["hold_id"]\n'
            '            expected = (ev["account_id"], ev["amount_cents"])\n'
            "            if active_holds.get(hold_id) != expected:\n"
            '                rejected.append((eid, etype, "unknown_hold"))\n'
            "            else:\n"
            "                acct, amount = expected\n"
            "                balances[acct] -= amount\n"
            "                remove_hold(hold_id)\n"
            "\n"
            '        elif etype == "hold_release":\n'
            '            hold_id = ev["hold_id"]\n'
            '            expected = (ev["account_id"], ev["amount_cents"])\n'
            "            if active_holds.get(hold_id) != expected:\n"
            '                rejected.append((eid, etype, "unknown_hold"))\n'
            "            else:\n"
            "                remove_hold(hold_id)",
            '        elif etype == "hold_create":\n'
            "            pass\n"
            "\n"
            '        elif etype == "hold_capture":\n'
            '            balances[ev["account_id"]] -= ev["amount_cents"]\n'
            "\n"
            '        elif etype == "hold_release":\n'
            "            pass",
        ),
    ],
}


def build_sim_app(dst):
    """Replicate what environment/Dockerfile produces inside the container."""
    shutil.copytree(os.path.join(ENV, "app"), os.path.join(dst, "app"))
    shutil.copytree(os.path.join(ENV, "docs"), os.path.join(dst, "docs"))
    shutil.copytree(os.path.join(ENV, "data"), os.path.join(dst, "data"))
    os.makedirs(os.path.join(dst, "state"), exist_ok=True)
    run(
        [
            sys.executable,
            os.path.join(dst, "app", "ledger.py"),
            "rebuild",
            "--events",
            os.path.join(dst, "data", "events.ndjson"),
            "--db",
            os.path.join(dst, "state", "ledger.db"),
        ]
    )
    shutil.copy(
        os.path.join(dst, "state", "ledger.db"),
        os.path.join(dst, "state", "baseline_stale.db"),
    )


def run(cmd, **kw):
    proc = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if proc.returncode != 0:
        raise RuntimeError("command failed: %r\n%s" % (cmd, proc.stderr[-3000:]))
    return proc


def grade(app_dir):
    with tempfile.TemporaryDirectory() as logs:
        run(
            [sys.executable, os.path.join(TESTS, "grade.py"), "--app-dir", app_dir, "--logs-dir", logs]
        )
        with open(os.path.join(logs, "reward.json"), encoding="utf-8") as fh:
            return json.load(fh)


def agent_finishes(app_dir):
    """Steps 3 & 4 of the instruction: regenerate snapshot, write report."""
    run(
        [
            sys.executable,
            os.path.join(app_dir, "app", "ledger.py"),
            "rebuild",
            "--events",
            os.path.join(app_dir, "data", "events.ndjson"),
            "--db",
            os.path.join(app_dir, "state", "ledger.db"),
        ]
    )
    run([sys.executable, os.path.join(SOL, "make_report.py"), "--app-dir", app_dir])


def test_oracle_passes():
    with tempfile.TemporaryDirectory() as td:
        build_sim_app(td)
        shutil.copy(os.path.join(SOL, "ledger_fixed.py"), os.path.join(td, "app", "ledger.py"))
        agent_finishes(td)
        r = grade(td)
        assert r["overall"] == 1.0, "oracle must score 1.0, got %s" % r
        print("oracle                    -> %.4f  PASS-as-expected" % r["overall"])


def test_do_nothing_fails():
    with tempfile.TemporaryDirectory() as td:
        build_sim_app(td)
        r = grade(td)
        assert r["overall"] < 0.10, "do-nothing must score < 0.10, got %s" % r
        print("do-nothing                -> %.4f  FAIL-as-expected" % r["overall"])


def test_db_only_cheat_fails():
    """Perfect snapshot + perfect report, but the engine is left buggy."""
    with tempfile.TemporaryDirectory() as td:
        build_sim_app(td)
        fixed = os.path.join(td, "_fixed_tool.py")
        with open(fixed, "w", encoding="utf-8") as fh:
            fh.write(FIXED_SRC)
        run(
            [
                sys.executable,
                fixed,
                "rebuild",
                "--events",
                os.path.join(td, "data", "events.ndjson"),
                "--db",
                os.path.join(td, "state", "ledger.db"),
            ]
        )
        run([sys.executable, os.path.join(SOL, "make_report.py"), "--app-dir", td])
        r = grade(td)
        assert r["robustness"] == 0.0 or r["robustness"] < 0.99, r
        assert r["overall"] < 0.99, "db-only cheat must not pass, got %s" % r
        print("db-only cheat (no patch)  -> %.4f  FAIL-as-expected" % r["overall"])


def test_partial_fixes_fail():
    """An agent that fixed all bugs except one must not pass — for each bug."""
    for name, replacements in REGRESSIONS.items():
        src = FIXED_SRC
        for old, new in replacements:
            assert old in src, "regression anchor not found: %s" % name
            src = src.replace(old, new)
        with tempfile.TemporaryDirectory() as td:
            build_sim_app(td)
            with open(os.path.join(td, "app", "ledger.py"), "w", encoding="utf-8") as fh:
                fh.write(src)
            agent_finishes(td)
            r = grade(td)
            assert r["overall"] < 0.99, "%s must not pass, got %s" % (name, r)
            print("partial fix: %-22s -> %.4f  FAIL-as-expected" % (name, r["overall"]))


if __name__ == "__main__":
    test_oracle_passes()
    test_do_nothing_fails()
    test_db_only_cheat_fails()
    test_partial_fixes_fail()
    print("verifier self-check: ALL OK")
