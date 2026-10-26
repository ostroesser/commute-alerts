from __future__ import annotations

import asyncio
import json
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest
from conftest import FIXTURES, MONDAY, THI, TZ, S, T, at, make_config, sa_feed, tu_feed

from commute_alerts.__main__ import replay
from commute_alerts.config import Config, Ntfy
from commute_alerts.engine import evening_digest
from commute_alerts.models import PlannedTrain
from commute_alerts.notifier import DryRunNotifier, Notification, NtfyNotifier
from commute_alerts.realtime import decode_alerts
from commute_alerts.service import Service
from commute_alerts.static_gtfs import plan_for
from commute_alerts.store import Store


class FakeFeeds:
    """Transport httpx simulant le proxy transport.data.gouv.fr."""

    def __init__(self) -> None:
        self.tu_status = 200
        self.tu_age = timedelta(0)
        self.now = at("06:40")
        self.calls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if "trip-updates" in str(request.url):
            if self.tu_status != 200:
                return httpx.Response(self.tu_status)
            return httpx.Response(
                200, content=tu_feed(self.now - self.tu_age, [T("88501", [S(THI, "07:28")])])
            )
        return httpx.Response(200, content=sa_feed(self.now, []))


def make_service(cfg: Config, store: Store, fake: FakeFeeds) -> tuple[Service, DryRunNotifier]:
    notifier = DryRunNotifier()
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake.handler))
    return Service(cfg, store, notifier, client=client), notifier


def test_feed_outage_single_notification_then_recovery(cfg: Config, store: Store) -> None:
    fake = FakeFeeds()
    svc, notifier = make_service(cfg, store, fake)

    async def cycles() -> None:
        fake.tu_status = 503
        for i in range(5):
            fake.now = at("06:40") + timedelta(minutes=2 * i)
            await svc.poll_cycle(fake.now)
        assert [n.title for n in notifier.sent] == ["📡 Données temps réel SNCF indisponibles"]
        assert notifier.sent[0].priority == 2
        fake.tu_status = 200
        fake.now = at("06:55")
        await svc.poll_cycle(fake.now)

    asyncio.run(cycles())
    assert [n.title for n in notifier.sent][-1] == "📡 Données temps réel SNCF rétablies"
    assert len(notifier.sent) == 2


def test_stale_feed_is_unavailable(cfg: Config, store: Store) -> None:
    fake = FakeFeeds()
    fake.tu_age = timedelta(minutes=7)
    svc, notifier = make_service(cfg, store, fake)
    asyncio.run(svc.poll_cycle(fake.now))
    assert len(notifier.sent) == 1 and "indisponibles" in notifier.sent[0].title
    assert "périmé" in notifier.sent[0].message


def test_no_fetch_outside_windows(cfg: Config, store: Store) -> None:
    fake = FakeFeeds()
    svc, _ = make_service(cfg, store, fake)
    asyncio.run(svc.poll_cycle(at("12:00")))
    assert fake.calls == 0


def test_tick_backoff(cfg: Config, store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeFeeds()
    fake.tu_status = 500
    svc, _ = make_service(cfg, store, fake)
    monkeypatch.setattr(svc, "gtfs_due", lambda now: False)
    delays = [asyncio.run(svc.tick(at("06:40") + timedelta(minutes=i))) for i in range(5)]
    assert delays == [90, 180, 300, 300, 300]
    # hors fenêtre : on dort jusqu'à la prochaine ouverture (max 5 min)
    assert asyncio.run(svc.tick(at("12:00"))) == 300
    assert asyncio.run(svc.tick(at("05:13"))) == 120


def test_evening_digest(cfg: Config, store: Store) -> None:
    tue = date(2026, 10, 27)
    plan = plan_for(store, cfg, cfg.windows[0], tue)
    removed = [PlannedTrain(tue, "88804", "", at("07:36", tue), at("08:06", tue), plan[0].mode)]
    alerts = decode_alerts(
        sa_feed(
            at("18:00"),
            [("g1", ["88714"], "Grève : circulation perturbée.", at("05:00", tue), at("10:00", tue))],
        ),
        TZ,
    )
    n = evening_digest(
        tue,
        [(cfg.windows[0], ("Thionville", "Luxembourg"), plan, removed, alerts.alerts)],
        send_all_clear=False,
        click=None,
    )
    assert n is not None and n.title == "🚆 Demain 27/10 : perturbations prévues" and n.priority == 3
    assert "• TER 88804 (départ 07:36) absent du plan (supprimé à l'avance)" in n.message
    assert "• TER 88714 (départ 07:48) : Grève : circulation perturbée." in n.message
    assert (
        evening_digest(tue, [(cfg.windows[0], ("A", "B"), plan, [], [])], send_all_clear=False, click=None)
        is None
    )


def test_digest_sent_once(cfg: Config, store: Store) -> None:
    fake = FakeFeeds()
    svc, notifier = make_service(cfg, store, fake)
    tue = date(2026, 10, 27)
    full = plan_for(store, cfg, cfg.windows[0], MONDAY)
    store.save_snapshot(
        tue,
        "Matin",
        date(2026, 10, 20),
        [
            PlannedTrain(tue, p.train_no, "", p.dep + timedelta(days=1), p.arr + timedelta(days=1), p.mode)
            for p in full
        ],
    )
    assert svc.digest_due(at("17:59")) is None
    day = svc.digest_due(at("18:00"))
    assert day == tue
    asyncio.run(svc.send_digest(tue, at("18:00")))
    assert len(notifier.sent) == 1 and "88804" in notifier.sent[0].message
    assert svc.digest_due(at("18:05")) is None


# --- ntfy -----------------------------------------------------------------------------
def test_ntfy_payload_and_auth() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "x"})

    cfg = Ntfy(
        url="https://ntfy.example.org/", topic="test-topic", token="secret", click_url="https://example.org/d"
    )
    notifier = NtfyNotifier(cfg, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    ok = asyncio.run(
        notifier.send(Notification("🚆 Matin", "TER 88501 (départ 07:28) SUPPRIMÉ.", 4, ["train", "x"]))
    )
    assert ok
    req = seen[0]
    assert str(req.url) == "https://ntfy.example.org"
    assert req.headers["Authorization"] == "Bearer secret"
    body = json.loads(req.content.decode("utf-8"))
    assert body == {
        "topic": "test-topic",
        "title": "🚆 Matin",
        "message": "TER 88501 (départ 07:28) SUPPRIMÉ.",
        "priority": 4,
        "tags": ["train", "x"],
        "click": "https://example.org/d",
    }


def test_ntfy_retries_then_gives_up(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(502)

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("commute_alerts.notifier.asyncio.sleep", no_sleep)
    cfg = Ntfy(url="https://ntfy.example.org", topic="t")
    notifier = NtfyNotifier(cfg, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert not asyncio.run(notifier.send(Notification("t", "m")))
    assert calls["n"] == 3


def test_ntfy_requires_config() -> None:
    with pytest.raises(ValueError, match="ntfy non configuré"):
        NtfyNotifier(Ntfy())


# --- rejeu de la capture réelle -----------------------------------------------------------
def test_replay_real_morning_capture(tmp_path: Path, gtfs_zip: Path) -> None:
    cfg = make_config(tmp_path)
    notifier = DryRunNotifier()
    svc = asyncio.run(replay(cfg, FIXTURES / "rt_20261026_morning", gtfs_zip, notifier))
    assert len(notifier.sent) == 1
    n = notifier.sent[0]
    assert n.title == "🚆 Retour Luxembourg→Thionville"
    assert n.message.startswith("TER 88721 (départ 08:39) SUPPRIMÉ.")
    assert "Alternative SNCF : 09:39 (88520)." in n.message
    assert (
        "Cause : Une opération de maintenance imprévue sur le train empêche sa mise en circulation."
        in n.message
    )
    states = svc.store.load_states(MONDAY, "Matin")
    assert states["88716"].status.value == "SCHEDULED"  # déjà parti à 08:29 : figé


def test_health_after_restart(cfg: Config, store: Store) -> None:
    from commute_alerts.health import evaluate

    svc, _ = make_service(cfg, store, FakeFeeds())
    assert svc.health.gtfs_imported_at  # GTFS déjà importé (redémarrage sans réimport)
    svc.health.last_loop_at = at("06:40").isoformat()
    ok, body = evaluate(svc.health, at("06:45"))
    assert ok and body["status"] == "ok"
    ok, _ = evaluate(svc.health, at("07:00"))
    assert not ok  # boucle figée depuis plus de 12 min


def test_gtfs_checked_once_per_refresh_slot(cfg: Config, store: Store) -> None:
    svc, _ = make_service(cfg, store, FakeFeeds())
    store.set_meta("gtfs_checked_at", at("09:00").isoformat())
    assert not svc.gtfs_due(at("17:39"))
    assert svc.gtfs_due(at("17:40"))
    store.set_meta("gtfs_checked_at", at("17:40").isoformat())  # 304 « inchangé » à 17:40
    assert not svc.gtfs_due(at("17:42"))
    assert not svc.gtfs_due(at("18:30"))
