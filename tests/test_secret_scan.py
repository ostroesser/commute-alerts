"""Le dépôt ne doit contenir aucune référence interne (hôte Tailscale, IP privée, jeton, valeurs du .env)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import secret_scan  # noqa: E402


def test_repository_is_clean() -> None:
    problems = secret_scan.scan(secret_scan.candidate_files(), secret_scan.local_secrets())
    assert problems == [], "\n".join(f"{f}:{n}: {label}" for f, n, label in problems)


def test_patterns_detect() -> None:
    # motifs construits dynamiquement pour ne pas se détecter eux-mêmes
    samples = {
        "raspberry." + "tail1234" + ".ts" + ".net": "Tailscale",
        "http://" + "100." + "101.12.3" + ":8080": "100.x",
        "192." + "168.1.20": "192.168",
        "Bearer " + "tk_" + "abcdefghijklmnop": "tk_",
    }
    for text, label in samples.items():
        hits = secret_scan.scan_text(text)
        assert hits and any(label.split(".")[0] in h for h in hits), (text, hits)


def test_no_false_positives() -> None:
    for text in (
        "version 1.100.2.3.4",
        "trains 88501, 88804",
        "https://ntfy.example.org",
        "token: ${NTFY_TOKEN}",
    ):
        assert secret_scan.scan_text(text) == [], text


def test_env_values(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "NTFY_URL=https://notify.home.internal\nNTFY_TOPIC=my-private-topic\nNTFY_TOKEN=\nLOG_LEVEL=DEBUG\n",
        encoding="utf-8",
    )
    secrets = secret_scan.local_secrets(env)
    assert secrets == {"NTFY_URL": "notify.home.internal", "NTFY_TOPIC": "my-private-topic"}
    assert secret_scan.scan_text("topic: my-private-topic", secrets) == ["valeur de NTFY_TOPIC (.env)"]
    public = tmp_path / ".env2"
    public.write_text("NTFY_URL=https://ntfy.sh\n", encoding="utf-8")
    assert secret_scan.local_secrets(public) == {}
