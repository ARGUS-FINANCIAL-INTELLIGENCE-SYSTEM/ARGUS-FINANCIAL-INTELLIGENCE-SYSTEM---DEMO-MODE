
"""ARGUS Pattern Lab: deterministic in-memory tests; never writes to SQLite."""
from datetime import datetime, timedelta, timezone
import time

from detection_engine import score_live_batch

BASE = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def event(txid, sender, receiver, seconds, amount=1000):
    return {
        "Transaction ID": txid,
        "Sender": sender,
        "Receiver": receiver,
        "Amount (INR)": amount,
        "Timestamp": (
            BASE + timedelta(seconds=seconds)
        ).isoformat(),
    }


def check(name, condition, detail=""):
    result = "PASS" if condition else "FAIL"
    suffix = f" — {detail}" if detail else ""
    print(f"{result}: {name}{suffix}")
    return bool(condition)


def main():
    tests = []

    fanin = [
        event(f"FI{i}", f"S{i}", "CENTRAL", i * 5)
        for i in range(6)
    ]
    scored = score_live_batch(fanin, [])
    fanin_rows = [
        row for row in scored
        if any(pattern.get("code") == "incoming_velocity_60" for pattern in row.get("risk_patterns", []))
    ]
    tests.append(check(
        "Rapid incoming pattern appears at its threshold",
        bool(fanin_rows),
        f"{len(fanin_rows)} event(s) carry the pattern",
    ))

    daily = [
        event(f"D{i}", f"D{i}", "HUB", i * 86400)
        for i in range(6)
    ]
    tests.append(check(
        "Fan-in across days",
        not any(
            pattern.get("code") == "incoming_velocity_60"
            for row in score_live_batch(daily, [])
            for pattern in row.get("risk_patterns", [])
        ),
    ))

    fanout = [
        event(f"FO{i}", "SOURCE", f"R{i}", i * 5)
        for i in range(6)
    ]
    tests.append(check(
        "Rapid fan-out marks group participants",
        sum(
            pattern.get("code") == "outgoing_velocity_60"
            for row in score_live_batch(fanout, [])
            for pattern in row.get("risk_patterns", [])
        ),
    ))

    pair_history = [
        event("P1", "X", "Y", 0),
        event("P2", "X", "Y", 86400),
    ]
    repeated = score_live_batch(
        [event("P3", "X", "Y", 172800)],
        pair_history,
    )[0]
    tests.append(check(
        "Repeated pair across days",
        "transferred money several times" in repeated["risk_reasons"].lower(),
    ))

    out_of_order = [
        event("O3", "S", "C", 30),
        event("O1", "S", "C", 10),
        event("O2", "S", "C", 20),
    ]
    out_scored = score_live_batch(out_of_order, [])
    tests.append(check(
        "Out-of-order timestamps accepted",
        len(out_scored) == 3
        and all("risk_score" in row for row in out_scored),
    ))

    normal = [
        event(f"N{i}", f"NORMAL{i}", "SHOP", i * 10 * 86400)
        for i in range(3)
    ]
    tests.append(check(
        "Low-frequency lookalike not temporal fan-in",
        not any(
            pattern.get("code") == "incoming_velocity_60"
            for row in score_live_batch(normal, [])
            for pattern in row.get("risk_patterns", [])
        ),
    ))

    # A high-volume merchant with widely spaced transactions should not
    # trigger the rolling temporal fan-in rule solely due to lifetime volume.
    benign = [
        event(f"B{i}", f"CUSTOMER{i}", "MERCHANT", i * 50 * 86400, 500)
        for i in range(40)
    ]
    benign_scores = score_live_batch(benign, [])
    tests.append(check(
        "Benign high-volume many-customer merchant",
        not any(
            "Temporal fan-in" in row["risk_reasons"]
            for row in benign_scores
        ),
    ))

    incoming = event("IN", "A", "B", 0, 1000)
    matched = event("OUT", "B", "C", 120, 950)
    unmatched = event("OUT2", "B", "D", 180, 100)

    matched_result = score_live_batch([matched], [incoming])[0]
    unmatched_result = score_live_batch([unmatched], [incoming])[0]

    tests.append(check(
        "Amount-matched timely pass-through detected",
        "received a similar amount and sent it onward"
        in matched_result["risk_reasons"].lower(),
    ))
    tests.append(check(
        "Amount-mismatched pass-through not flagged",
        "pass-through" not in unmatched_result["risk_reasons"].lower(),
    ))

    cycle = score_live_batch(
        [event("C3", "C", "A", 120)],
        [
            event("C1", "A", "B", 0),
            event("C2", "B", "C", 60),
        ],
    )[0]
    tests.append(check(
        "Short cycle evidence",
        "moving back through a recent transfer path" in cycle["risk_reasons"].lower(),
    ))

    # Performance smoke test. This checks runtime in this environment,
    # not a guaranteed production throughput figure.
    large_batch = [
        event(f"L{i}", f"S{i % 100}", f"R{i % 37}", i, 100)
        for i in range(5000)
    ]
    start = time.perf_counter()
    large_scored = score_live_batch(large_batch, [])
    elapsed = time.perf_counter() - start

    tests.append(check(
        "5,000-event performance smoke test",
        len(large_scored) == 5000 and elapsed < 10,
        f"{elapsed:.2f}s",
    ))

    passed = sum(tests)
    print(f"\nPATTERN LAB RESULT: {passed}/{len(tests)} tests passed")

    if passed != len(tests):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
