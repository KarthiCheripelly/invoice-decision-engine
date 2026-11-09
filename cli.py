"""
Command-line runner, handy for quick checks and for rehearsing without the browser.

    python cli.py samples/01_happy_path_acme.pdf
    python cli.py samples/*.pdf            # run several in order
    python cli.py --reset                  # clear history and ledger
"""
from __future__ import annotations

import sys

from dotenv import load_dotenv

load_dotenv()

from pipeline import db  # noqa: E402
from pipeline.runner import run_pipeline  # noqa: E402

ICON = {"pass": "ok  ", "info": "info", "warn": "WARN", "fail": "FAIL", "skip": "skip"}


def main(argv: list[str]) -> None:
    if not argv or argv == ["-h"]:
        print(__doc__)
        return
    if argv[0] == "--reset":
        db.reset_db()
        print("history and ledger reset")
        argv = argv[1:]
    for path in argv:
        with open(path, "rb") as f:
            data = f.read()
        print("=" * 100)
        print(path)
        for ev in run_pipeline(data, path.replace("\\", "/").split("/")[-1]):
            if ev["stage"] == "__result__":
                res = ev["result"]
                print("-" * 100)
                print(f"DECISION: {res.decision['status']}")
                print(f"  why : {res.decision['summary']}")
                print(f"  next: {res.decision['next_action']}")
                for c in res.decision["checks"]:
                    print(f"  [{ICON[c['status']]}] {c['id']:<4}{c['name']:<34}{c['message']}")
                print(f"  run #{res.run_id} in {res.duration_ms} ms")
            elif ev["status"] != "running":
                print(f"  {ev['stage']:<16}{ev['status']:<6}{ev['detail']}")


if __name__ == "__main__":
    main(sys.argv[1:])
