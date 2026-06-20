#!/usr/bin/env python3
"""
Test plan — DAX morning brief + sweep watcher
Validates all 7 setup types against known historical dates from DB.
Run with --verbose to see sample briefs for key setups.
"""
import sys
from pathlib import Path
from datetime import date

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from db.local_db import LocalDB

DB_PATH = str(Path(__file__).resolve().parents[1] / "src" / "data" / "trade.db")

import importlib.util
spec = importlib.util.spec_from_file_location(
    "dax_morning_brief",
    Path(__file__).resolve().parent / "dax_morning_brief.py"
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def run(label, test_date_str, expected_setup, expected_sweep=None, print_brief=False):
    d      = date.fromisoformat(test_date_str)
    db     = LocalDB(DB_PATH)
    result = mod.run_brief(d, db=db, verbose=print_brief)
    db.close()

    actual_setup = result.get("setup_type", "?") if result else "None"
    actual_sweep = result.get("sweep_expected")   if result else None

    ok_setup = actual_setup == expected_setup
    ok_sweep = (actual_sweep == expected_sweep) if expected_sweep is not None else True
    status   = "PASS" if (ok_setup and ok_sweep) else "FAIL"

    print(f"[{status}] {label}")
    print(f"       date={test_date_str}  expected={expected_setup}/{expected_sweep}"
          f"  got={actual_setup}/{actual_sweep}")
    if result and status == "PASS":
        print(f"       gap={result['gap_atr']:.2f}x  dist={result['dist_to_sweep_atr']:.2f}x  "
              f"sweep_level={result.get('sweep_level',0):.0f}  tp={result.get('tp_level',0):.0f}")
    if result and not print_brief and status == "FAIL":
        print("       Brief excerpt:")
        print("       " + "\n       ".join(result["brief"].split("\n")[:12]))
    print()
    return status == "PASS"


print("=" * 64)
print("DAX MORNING BRIEF — TEST PLAN (8 scenarios)")
print("=" * 64)
print()

results = []

# 1. skip_small: gap < 0.3×ATR
results.append(run(
    "skip_small  gap 0.13×ATR (2025-09-02)",
    "2025-09-02",
    expected_setup="skip_small",
    expected_sweep=False,
))

# 2. skip_event: gap ≥ 6×ATR
results.append(run(
    "skip_event  gap 7.83×ATR (2025-07-14)",
    "2025-07-14",
    expected_setup="skip_event",
    expected_sweep=False,
))

# 3. Setup A: clean fill, dist far from extreme
results.append(run(
    "Setup A     gap 0.53×ATR  dist 6.75×ATR (2025-07-15)",
    "2025-07-15",
    expected_setup="A",
    expected_sweep=False,
))

# 4. Setup A_weak: gap 1.5–2×ATR, sweep unlikely
results.append(run(
    "Setup A_weak gap 1.61×ATR  dist 1.18×ATR (2025-07-09)",
    "2025-07-09",
    expected_setup="A_weak",
    expected_sweep=False,
))

# 5. Setup B: sweep expected (dist < 0.5×ATR), normal gap
results.append(run(
    "Setup B     gap 1.69×ATR  dist 0.40×ATR (2025-08-06)",
    "2025-08-06",
    expected_setup="B",
    expected_sweep=True,
))

# 6. Uncertain: dist 0.5–1×ATR — sweep possible
results.append(run(
    "Uncertain   gap 1.97×ATR  dist 0.98×ATR (2025-07-01)",
    "2025-07-01",
    expected_setup="uncertain",
    expected_sweep=True,
))

# 7. large_gap_sweep: gap > 2×ATR, dist < 0.5×ATR → wait for sweep, target 33%
results.append(run(
    "large_gap_sweep  gap 3.85×ATR  dist~0×ATR (2025-07-02)",
    "2025-07-02",
    expected_setup="large_gap_sweep",
    expected_sweep=True,
))

# 8. large_gap_serpe: gap > 2×ATR, dist ≥ 0.5×ATR → skip pre-Frankfurt
results.append(run(
    "large_gap_serpe  gap 2.42×ATR  dist 1.91×ATR (2025-07-07)",
    "2025-07-07",
    expected_setup="large_gap_serpe",
    expected_sweep=False,
))

passed = sum(results)
total  = len(results)
print("=" * 64)
print(f"Results: {passed}/{total} passed")
print("=" * 64)

if "--verbose" in sys.argv or passed < total:
    print("\n--- SAMPLE: Setup B (2025-08-06) ---")
    run("", "2025-08-06", "B", print_brief=True)
    print("\n--- SAMPLE: large_gap_sweep (2025-07-02) ---")
    run("", "2025-07-02", "large_gap_sweep", print_brief=True)
    print("\n--- SAMPLE: large_gap_serpe (2025-07-07) ---")
    run("", "2025-07-07", "large_gap_serpe", print_brief=True)
