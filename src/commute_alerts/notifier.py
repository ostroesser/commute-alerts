"""Publication ntfy (POST JSON) et mode dry-run (stdout)."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from .config import Ntfy

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Notification:
    title: str
    message: str
    priority: int = 3
    tags: list[str] = field(default_factory=list)
    click: str | None = None
    window: str | None = None

    def payload(self, topic: str) -> dict[str, object]:
        body: dict[str, object] = {
            "topic": topic,
            "title": self.title,
            "message": self.message,
            "priority": max(1, min(5, self.priority)),
            "tags": self.tags,
        }
        if self.click:
            body["click"] = self.click
        return body


class Notifier(Protocol):
    async def send(self, n: Notification) -> bool: ...


class DryRunNotifier:
    def __init__(self) -> None:
        self.sent: list[Notification] = []

    async def send(self, n: Notification) -> bool:
        self.sent.append(n)
        print(
            f"\n[ntfy dry-run] prio={n.priority} tags={','.join(n.tags)}\n{n.title}\n{n.message}",
            file=sys.stdout,
            flush=True,
        )
        return True


class NtfyNotifier:
    """POST JSON sur la racine du serveur ntfy (`{topic, title, message, …}`) : UTF-8/emojis sans souci."""

    def __init__(self, cfg: Ntfy, client: httpx.AsyncClient | None = None, retries: int = 3) -> None:
        if not cfg.configured:
            raise ValueError("ntfy non configuré (NTFY_URL / NTFY_TOPIC)")
        self.cfg = cfg
        self.client = client or httpx.AsyncClient(timeout=15)
        self.retries = retries

    async def send(self, n: Notification) -> bool:
        if not (self.cfg.url and self.cfg.topic):  # pas d'assert : supprimé sous python -O
            raise ValueError("ntfy non configuré (NTFY_URL / NTFY_TOPIC)")
        headers = {"Content-Type": "application/json"}
        if self.cfg.token:
            headers["Authorization"] = f"Bearer {self.cfg.token}"
        if n.click is None and self.cfg.click_url:
            n.click = self.cfg.click_url
        data = json.dumps(n.payload(self.cfg.topic), ensure_ascii=False).encode()
        delay = 2.0
        for attempt in range(1, self.retries + 1):
            try:
                r = await self.client.post(self.cfg.url.rstrip("/"), content=data, headers=headers)
                if r.status_code < 300:
                    return True
                log.warning(
                    "ntfy a refusé la notification", extra={"status": r.status_code, "body": r.text[:200]}
                )
                if 400 <= r.status_code < 500 and r.status_code != 429:
                    return False
            except httpx.HTTPError as exc:
                log.warning("échec publication ntfy", extra={"attempt": attempt, "error": str(exc)})
            await asyncio.sleep(delay)
            delay *= 2
        return False
