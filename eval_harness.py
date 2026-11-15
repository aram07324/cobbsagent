#!/usr/bin/env python3
"""
Eval harness (no AI, free). Runs the deterministic pipeline from cobbsagent.py
over every repo in eval/ and compares the outcome with eval/expected.json.

Run:  python eval_harness.py            (writes results.md)
"""
import json
import shutil
import time
from pathlib import Path

import cobbsagent as m1

def eval_repo(repo: Path) -> list:
    tmp, work, py = m1.make_sandbox(repo.resolve())
    try:
        ok, _ = m1.install(py, work)
        if not ok:
            return [{"name": "*", "result": "baseline_install_failed"}]
        ok, _ = m1.run_tests(py, work)
        if not ok:
            return [{"name": "*", "result": "baseline_tests_failed"}]
        findings, audit_succeeded = m1.audit_result(py, work)
        if not audit_succeeded:
            return [{"name": "*", "result": "audit_failed"}]
        req = work / "requirements.txt"
        original = req.read_text()
        results = []
        for f in findings:
            r = dict(f)
            if not f["fix_version"]:
                r["result"] = "no_fix_available"
            elif not m1.bump_requirement(work, f["name"], f["fix_version"]):
                r["result"] = "transitive_dependency_skipped"
            else:
                ok, _ = m1.install(py, work)
                if not ok:
                    r["result"] = "install_failed"
                else:
                    ok, _ = m1.run_tests(py, work)
                    r["result"] = "safe_upgrade" if ok else "tests_broke"
            req.write_text(original)  # reset for the next finding
            results.append(r)
        return results
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    root = Path("eval")
    expected = json.loads((root / "expected.json").read_text())
    rows, matched = [], 0
    category_counts = {}
    for name, meta in expected.items():
        t0 = time.time()
        results = eval_repo(root / name)
        got = next((r["result"] for r in results if r["name"].lower() == meta["package"]),
                   results[0]["result"] if results and results[0]["name"] == "*" else "not_flagged")
        ok = got == meta["expected"]
        matched += ok
        category = meta.get("category", "uncategorized")
        category_counts[category] = category_counts.get(category, 0) + 1
        rows.append((
            name, meta["package"], category, meta["expected"], got,
            round(time.time() - t0, 1), ok,
        ))
        print(f"{name}: expected {meta['expected']}, got {got} {'OK' if ok else 'MISMATCH'}")

    lines = [
        "| case | package | category | expected | got | seconds | match |",
        "|---|---|---|---|---|---|---|",
    ]
    lines += [
        f"| {name} | {package} | {category} | {wanted} | {got} | {seconds} | "
        f"{'yes' if ok else 'NO'} |"
        for name, package, category, wanted, got, seconds, ok in rows
    ]
    lines.append(f"\n{matched}/{len(rows)} cases behaved as expected.")
    Path("results.md").write_text("\n".join(lines))
    print("\n".join(lines))
    for category, count in sorted(category_counts.items()):
        print(f"{category}: {count} case(s)")


if __name__ == "__main__":
    main()
