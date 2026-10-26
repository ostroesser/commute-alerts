"""Garde-fou dépôt public : échoue si un hôte interne, une IP privée/Tailscale ou un jeton apparaît.

Usage :
    python tools/secret_scan.py            # fichiers suivis par git (ou tout le dépôt hors git)
    python tools/secret_scan.py --staged   # fichiers indexés (hook pre-commit)

Vérifie aussi que les valeurs sensibles du `.env` local (topic, token, clé, hôte privé) n'apparaissent
dans aucun fichier versionné.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]

PATTERNS: dict[str, re.Pattern[str]] = {
    "hôte Tailscale (*.ts.net)": re.compile(r"[A-Za-z0-9-]+\.ts\.net\b"),
    "IP 100.x.y.z (CGNAT/Tailscale)": re.compile(r"(?<![\d.])100\.\d{1,3}\.\d{1,3}\.\d{1,3}(?![\d.])"),
    "IP privée 192.168.x.y": re.compile(r"(?<![\d.])192\.168\.\d{1,3}\.\d{1,3}(?![\d.])"),
    "jeton ntfy (tk_…)": re.compile(r"\btk_[A-Za-z0-9]{6,}"),
}
SKIP_DIRS = {".git", ".venv", "data", "__pycache__", ".mypy_cache", ".ruff_cache", ".pytest_cache"}
SKIP_SUFFIXES = {".pb", ".zip", ".db", ".png", ".jpg", ".lock"}
SENSITIVE_KEYS = re.compile(r"TOPIC|TOKEN|KEY|SECRET|PASSWORD|PASS", re.I)
PUBLIC_HOSTS = {"ntfy.sh", "ntfy.example.org", "example.org", "localhost", "127.0.0.1"}


def _git_files(staged: bool) -> list[Path] | None:
    cmd = ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"] if staged else ["git", "ls-files"]
    try:
        out = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    return [ROOT / line for line in out.splitlines() if line]


def candidate_files(staged: bool = False) -> list[Path]:
    files = _git_files(staged)
    if files is None or (not staged and not files):
        files = [p for p in ROOT.rglob("*") if p.is_file() and not SKIP_DIRS & set(p.relative_to(ROOT).parts)]
    return [f for f in files if f.is_file() and f.suffix not in SKIP_SUFFIXES and f.name != ".env"]


def local_secrets(env_file: Path = ROOT / ".env") -> dict[str, str]:
    """Valeurs du .env local à ne jamais versionner (le .env lui-même est ignoré par git)."""
    out: dict[str, str] = {}
    if not env_file.exists():
        return out
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip("'\"")
        if len(value) < 6:
            continue
        if SENSITIVE_KEYS.search(key):
            out[key.strip()] = value
        elif key.strip().endswith("URL"):
            host = urlparse(value).hostname or ""
            if host and host not in PUBLIC_HOSTS:
                out[key.strip()] = host
    return out


def scan_text(text: str, secrets: dict[str, str] | None = None) -> list[str]:
    hits = [label for label, rx in PATTERNS.items() if rx.search(text)]
    hits += [f"valeur de {k} (.env)" for k, v in (secrets or {}).items() if v in text]
    return hits


def scan(files: list[Path], secrets: dict[str, str]) -> list[tuple[Path, int, str]]:
    problems: list[tuple[Path, int, str]] = []
    for f in files:
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(lines, 1):
            for label in scan_text(line, secrets):
                problems.append((f, n, label))
    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--staged", action="store_true")
    args = ap.parse_args()
    problems = scan(candidate_files(args.staged), local_secrets())
    for f, n, label in problems:
        print(f"{f.relative_to(ROOT)}:{n}: {label}", file=sys.stderr)
    if problems:
        print(f"\n{len(problems)} problème(s) : commit refusé (hygiène dépôt public).", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
