#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
TEXT_EXTENSIONS = {
    ".md",
    ".py",
    ".toml",
    ".yml",
    ".yaml",
    ".txt",
    ".ps1",
    ".sh",
    ".json",
}
SKIP_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", "build", "dist", "reports"}
FORBIDDEN_PATTERNS = {
    "internal_codename": re.compile(r"\b(unicorn|nebula|lazarus|dreamscape|techlabs|quantum|gladiator)\b", re.IGNORECASE),
    "local_path": re.compile(r"/Users/|Library/Containers|Desktop/media compression|Telegram|sauliuskruopis", re.IGNORECASE),
}


def should_check(path: Path) -> bool:
    if any(part in SKIP_DIRS for part in path.parts):
        return False
    if path.name == "public_surface_check.py":
        return False
    return path.suffix.lower() in TEXT_EXTENSIONS


def main() -> int:
    failures: list[str] = []
    for path in sorted(REPO_ROOT.rglob("*")):
        if not path.is_file() or not should_check(path):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for label, pattern in FORBIDDEN_PATTERNS.items():
            for match in pattern.finditer(text):
                failures.append(f"{label}: {path.relative_to(REPO_ROOT)}: {match.group(0)}")

    if failures:
        print("Public surface check failed:")
        for failure in failures:
            print(f" - {failure}")
        return 1

    print("Public surface check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
