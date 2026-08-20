"""Security guards, run locally as part of `pnpm run verify`.

These are the checks that must not depend on someone remembering them in review. They are
cheap, they are deterministic, and each one corresponds to a ❌ rule in
docs/ENGINEERING-SPEC.md §3:

  1. no provider key is committed anywhere in the tree
  2. `.env` is not tracked by git
  3. `frontend/` references exactly ONE `NEXT_PUBLIC_*` variable

Guard 3 is the least obvious and the most valuable. The cockpit needs the backend socket
URL and nothing else. A second public variable is how a provider key starts drifting
toward a browser bundle — it never arrives as "let's leak a secret", it arrives as one
more innocuous-looking config value. Pinning the count to one makes that a failing check
instead of a judgement call.

Scans the working tree rather than the git index, so it catches a key BEFORE it is staged.

    uv run --project backend python scripts/guards.py
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SKIP_DIRS = {
    ".git", "node_modules", ".venv", ".next", "dist", "__pycache__",
    ".pytest_cache", ".ruff_cache", "runs", ".turbo",
}
TEXT_SUFFIXES = {
    ".py", ".ts", ".tsx", ".js", ".mjs", ".cjs", ".json", ".yaml", ".yml",
    ".md", ".toml", ".css", ".html", ".txt", ".env", "",
}

# Deliberately shaped to the real prefixes. A generic "long random string" pattern would
# fire on lockfile hashes every time and get switched off within a week.
KEY_PATTERNS = {
    "Groq": re.compile(r"gsk_[A-Za-z0-9]{20,}"),
    "OpenRouter": re.compile(r"sk-or-v1-[A-Za-z0-9]{20,}"),
    "Google/Gemini": re.compile(r"AIza[A-Za-z0-9_\-]{30,}"),
    "OpenAI": re.compile(r"sk-proj-[A-Za-z0-9_\-]{20,}"),
}

PUBLIC_ENV = re.compile(r"NEXT_PUBLIC_[A-Z0-9_]+")
ALLOWED_PUBLIC_ENV = {"NEXT_PUBLIC_WS_URL"}


def _walk() -> list[Path]:
    """Every file git tracks, plus untracked ones it is NOT ignoring.

    That set is exactly "files at risk of being committed", which is the thing this script
    is actually about. Asking git rather than walking the tree means a gitignored file —
    `.env` above all — is out of scope by construction: a real key living there is correct,
    not a finding, and a scanner that shouts about it is a scanner people learn to ignore.
    Guard 2 separately proves `.env` is still ignored, so the exemption cannot be abused.
    """
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:  # not a git repo — fall back to a plain walk
        candidates = [p for p in ROOT.rglob("*") if p.is_file()]
    else:
        candidates = [ROOT / name for name in result.stdout.split("\0") if name]

    files: list[Path] = []
    for path in candidates:
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts):
            continue
        if path.suffix.lower() in TEXT_SUFFIXES:
            files.append(path)
    return files


def check_no_committed_keys(files: list[Path]) -> list[str]:
    problems: list[str] = []
    for path in files:
        # .env.example carries empty placeholders by design.
        if path.name == ".env.example":
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for provider, pattern in KEY_PATTERNS.items():
            if pattern.search(text):
                rel = path.relative_to(ROOT)
                problems.append(f"{provider} key pattern found in {rel}")
    return problems


def check_env_untracked() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "--error-unmatch", ".env"],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    if result.returncode == 0:
        return [".env is tracked by git -- it must stay gitignored"]
    return []


def check_single_public_env(files: list[Path]) -> list[str]:
    frontend = ROOT / "frontend"
    if not frontend.exists():
        return []

    found: set[str] = set()
    for path in files:
        if not path.is_relative_to(frontend):
            continue
        try:
            found.update(PUBLIC_ENV.findall(path.read_text(encoding="utf-8", errors="ignore")))
        except OSError:
            continue

    extra = found - ALLOWED_PUBLIC_ENV
    if extra:
        return [
            "frontend/ must expose only NEXT_PUBLIC_WS_URL; found also: "
            + ", ".join(sorted(extra))
        ]
    if not found:
        return ["frontend/ no longer references NEXT_PUBLIC_WS_URL -- the cockpit needs it"]
    return []


def main() -> int:
    files = _walk()
    problems = [
        *check_no_committed_keys(files),
        *check_env_untracked(),
        *check_single_public_env(files),
    ]

    if problems:
        print("GUARDS FAILED:", file=sys.stderr)
        for problem in problems:
            print(f"  x {problem}", file=sys.stderr)
        return 1

    print(f"guards ok ({len(files)} files scanned): no committed keys, .env untracked, "
          "cockpit exposes one public env var")
    return 0


if __name__ == "__main__":
    sys.exit(main())
