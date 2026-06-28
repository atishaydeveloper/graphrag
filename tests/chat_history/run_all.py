"""Run every chat-history / access-control integration suite and report.

Usage (from inside the graphrag container):
    docker exec -w /code/tests_chat_history graphrag python run_all.py
See README.md for the copy-in command.
"""
import sys

import test_task1_store
import test_task2_access_control
import test_task2_leakage_graph_hiding
import test_task3_agent_restrictions

SUITES = [
    test_task1_store,
    test_task2_access_control,
    test_task2_leakage_graph_hiding,
    test_task3_agent_restrictions,
]


def main():
    results = []
    for mod in SUITES:
        try:
            results.append(mod.run())
        except Exception as exc:  # noqa: BLE001 - report and continue
            print(f"  [ERROR] {mod.__name__}: {exc}")
            results.append(False)

    print("\n" + "=" * 60)
    passed = sum(1 for ok in results if ok)
    print(f"SUITES: {passed}/{len(results)} passed")
    ok = all(results)
    print("ALL SUITES PASSED" if ok else "SOME SUITES FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
