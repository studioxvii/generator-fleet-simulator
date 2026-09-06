#!/usr/bin/env python3
"""Fail CI when committed files contain obvious credential material."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


EXCLUDED_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "venv",
}
SKIPPED_EXTENSIONS = {
    ".7z",
    ".DS_Store",
    ".bmp",
    ".egg-info",
    ".gif",
    ".ico",
    ".jpeg",
    ".jpg",
    ".mov",
    ".mp4",
    ".pdf",
    ".png",
    ".pyc",
    ".svg",
    ".tar",
    ".tgz",
    ".ttf",
    ".webp",
    ".woff",
    ".woff2",
    ".zip",
}
MAX_FILE_BYTES = 1_000_000

SECRET_PATTERNS = [
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("aws access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{30,}|github_pat_[A-Za-z0-9_]{50,})\b")),
    ("openai key", re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{32,}\b")),
    ("anthropic key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{40,}\b")),
    ("stripe live secret", re.compile(r"\b(?:sk|rk)_live_[A-Za-z0-9]{16,}\b")),
    ("slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
]


def should_skip(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        relative = path
    if any(part in EXCLUDED_DIRS or part.endswith(".egg-info") for part in relative.parts):
        return True
    return path.suffix.lower() in SKIPPED_EXTENSIONS


def scan_file(path: Path, root: Path) -> list[str]:
    if should_skip(path, root) or not path.is_file():
        return []
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return []
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []

    findings: list[str] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        for label, pattern in SECRET_PATTERNS:
            if pattern.search(line):
                findings.append(f"{path.relative_to(root)}:{line_number}: {label}")
    return findings


def scan(root: Path) -> list[str]:
    findings: list[str] = []
    for path in root.rglob("*"):
        findings.extend(scan_file(path, root))
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan repository files for obvious committed secrets.")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="Repository root to scan.")
    args = parser.parse_args()

    root = args.root.resolve()
    findings = scan(root)
    if findings:
        print("Credential guard failed. Remove or rotate these secrets before committing:")
        for finding in findings:
            print(f" - {finding}")
        return 1
    print("Credential guard passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
