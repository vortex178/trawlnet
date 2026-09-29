"""Fail if the repo contains personal data or secrets. Run in CI and before every commit/release.

  python tests/pii_scan.py [--deny FILE]

Checks every text file (except .git, .venv): e-mail addresses outside example domains, phone numbers,
connector UUIDs (mcp__<uuid>), Google Sheet ids, private keys / API-key shapes. `--deny FILE` (or env
PII_DENYLIST) adds your own terms, one per line (your name, employer, sheet id) — keep that file outside the repo.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", ".venv", "__pycache__", "node_modules"}
OK_EMAIL = re.compile(r"@(example\.(com|org|net)|[a-z0-9-]+\.example|anthropic\.com|users\.noreply\.github\.com)$", re.I)
CHECKS = {
    "email": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "phone": re.compile(r"(?<![\w.])(\+\d{1,3}[ -]?\d{4,5}[ -]?\d{5,6}|\(\d{3}\) ?\d{3}-\d{4}|\d{3}-\d{3}-\d{4}"
                        r"|[6-9]\d{9})(?![\w.])"),  # international, US formats, bare Indian mobile
    "connector-uuid": re.compile(r"mcp__[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"),
    "sheet-id": re.compile(r"docs\.google\.com/spreadsheets/d/(?!<)[A-Za-z0-9_-]{30,}|\b1[A-Za-z0-9_-]{42,43}\b"),
    "private-key": re.compile(r"-----BEGIN (RSA |EC )?PRIVATE KEY-----|\"private_key\"\s*:\s*\"-"),
    "api-key": re.compile(r"\b(fc-[0-9a-f]{32}|sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{35})\b"),
}


def files():
    for p in ROOT.rglob("*"):
        if p.is_file() and not SKIP_DIRS & set(p.relative_to(ROOT).parts):
            try:
                yield p, p.read_text()
            except (UnicodeDecodeError, OSError):
                continue


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--deny", default=os.environ.get("PII_DENYLIST"))
    a = ap.parse_args()
    deny = [t.strip() for t in Path(a.deny).read_text().splitlines() if t.strip()] if a.deny else []
    hits = []
    for p, text in files():
        rel = p.relative_to(ROOT)
        for i, line in enumerate(text.splitlines(), 1):
            for name, rx in CHECKS.items():
                for m in rx.finditer(line):
                    if name == "email" and OK_EMAIL.search(m.group(0)):
                        continue
                    hits.append(f"{rel}:{i}: {name}: {m.group(0)[:60]}")
            low = line.lower()
            hits += [f"{rel}:{i}: denylist term" for t in deny if t.lower() in low]
    for h in hits:
        print(h)
    print(f"pii scan: {len(hits)} finding(s)" + (f" (+{len(deny)} denylist terms)" if deny else ""))
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
