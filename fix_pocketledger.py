"""fix_pocketledger.py — run from the pocketledger repo root.
Adds CI + Python badges to README (workflow file is tests.yml).
After running: review in GitHub Desktop, commit, push.
"""
import sys
from pathlib import Path

rd = Path("README.md")
if not rd.exists() or not Path("categorizer.py").exists():
    sys.exit("ERROR: run this from the pocketledger repo root.")
text = rd.read_text(encoding="utf-8")

title = "# PocketLedger\n"
badges = ("![CI](https://github.com/ektamishra4321/pocketledger/actions/workflows/tests.yml/badge.svg)\n"
          "![Python](https://img.shields.io/badge/python-3.10%2B-blue)\n")
if "actions/workflows/tests.yml/badge.svg" not in text:
    if title not in text:
        sys.exit("ERROR: README title line not found — aborting, nothing changed.")
    text = text.replace(title, title + badges, 1)
    print("[1/1] Added CI badge to README.")
else:
    print("[1/1] CI badge already present — skipped.")
rd.write_text(text, encoding="utf-8", newline="\n")
print("\nDone. Review in GitHub Desktop -> commit -> push.")