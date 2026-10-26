"""Orchestration : GTFS quotidien, baseline, cycle de polling temps réel, panne de flux, résumé veille."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from . import static_gtfs
from .calendar import is_active_day
from .config import Config, Window
from .engine import evaluate_window, evening_digest
from .ids import Mode
from .matcher import find_added
from .models import PlannedTrain
from .notifier import Notification, Notifier
from .realtime import AlertSnapshot, FeedError, FeedSnapshot, decode_alerts, decode_trip_updates, fetch
from .store import Store

log = logging.getLogger(__name__)

SNAPSHOT_DAYS_AHEAD = 8
DIGEST_ALERT_MAX_H = 48.0


@dataclass(slots=True)
class FeedHealth:
    consecutive_errors: int = 0
    unavailable: bool = False
    notified_down: bool = False
    last_ok: datetime | None = None
    feed_ts: datetime | None = None
    last_error: str = ""

    def age_s(self, now: datetime) -> int | None:
        return int((now - self.feed_ts).total_seconds()) if self.feed_ts else None


@dataclass(slots=True)
class HealthState:
    """Exposé par /healthz (lu depuis un autre thread : uniquement des valeurs simples)."""

    started_at: str = ""
    last_loop_at: str = ""
    last_poll_at: str = ""
    last_poll_ok_at: str = ""
    feed_age_s: int | None = None
    feed_unavailable: bool = False
    gtfs_imported_at: str = ""
    active_windows: list[str] = field(default_factory=list)
    last_error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in self.__slots__}


class Service:
    def __init__(
        self, cfg: Config, store: Store, notifier: Notifier, client: httpx.AsyncClient | None = None
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.notifier = notifier
        self.tz = ZoneInfo(cfg.timezone)
        self.client = client or httpx.AsyncClient(
            timeout=cfg.polling.http_timeout_s,
            follow_redirects=True,
            headers={"User-Agent": static_gtfs.USER_AGENT},
        )
        self.feed_health = FeedHealth()
        self.health = HealthState()
        self.codes = set(cfg.stations.values())
        self.last_alerts: AlertSnapshot | None = None
        self._names: dict[str, str] = {}
        self.health.gtfs_imported_at = store.get_meta("gtfs_imported_at") or ""

    # --- helpers -------------------------------------------------------------
    def now(self) -> datetime:
        return datetime.now(self.tz)

    def name(self, key: str) -> str:
        if key not in self._names:
            code = self.cfg.station_code(key)
            self._names[key] = self.store.stop_name(code) or key.replace("_", " ").title()
        return self._names[key]

    def names(self, w: Window) -> tuple[str, str]:
        return self.name(w.origin), self.name(w.destination)

    def monitoring_bounds(self, w: Window, day: date) -> tuple[datetime, datetime]:
        start = datetime.combine(day, w.start, tzinfo=self.tz) - timedelta(
            minutes=self.cfg.polling.pre_window_min
        )
        end = datetime.combine(day, w.end, tzinfo=self.tz) + timedelta(
            minutes=self.cfg.polling.post_window_min
        )
        return start, end

    def active_windows(self, now: datetime) -> list[tuple[Window, date]]:
        out = []
        for w in self.cfg.windows:
            day = now.date()
            if not is_active_day(self.cfg, w, day):
                continue
            lo, hi = self.monitoring_bounds(w, day)
            if lo <= now <= hi:
                out.append((w, day))
        return out

    def next_window_start(self, now: datetime) -> datetime | None:
        best: datetime | None = None
        for offset in range(0, 15):
            day = now.date() + timedelta(days=offset)
            for w in self.cfg.windows:
                if not is_active_day(self.cfg, w, day):
                    continue
                lo, _ = self.monitoring_bounds(w, day)
                if lo > now and (best is None or lo < best):
                    best = lo
            if best:
                return best
        return best

    async def notify(self, n: Notification, now: datetime) -> bool:
        ok = await self.notifier.send(n)
        if ok:
            self.store.log_notification(now, n.window, n.title, n.message, n.priority, n.tags)
        log.info(
            "notification", extra={"sent": ok, "title": n.title, "priority": n.priority, "window": n.window}
        )
        return ok

    # --- GTFS ----------------------------------------------------------------
    async def refresh_gtfs(self, now: datetime, *, force: bool = False) -> None:
        def work() -> None:
            # connexion SQLite dédiée au thread d'import
            store = Store(self.store.path)
            try:
                path = static_gtfs.download(self.cfg, store)
                zip_path = self.cfg.data_dir / "gtfs.zip"
                if path is not None or force or (not store.gtfs_loaded() and zip_path.exists()):
                    static_gtfs.import_zip(zip_path, store, self.codes)
            finally:
                store.close()

        try:
            await asyncio.to_thread(work)
        except Exception as exc:  # le service continue avec le GTFS précédent
            log.error("rafraîchissement gtfs en échec", extra={"error": str(exc)})
            self.health.last_error = f"gtfs: {exc}"
        self._names.clear()
        self.health.gtfs_imported_at = self.store.get_meta("gtfs_imported_at") or ""
        if self.store.gtfs_loaded():
            self.save_snapshots(now.date())
        self.store.purge(now.date())

    def save_snapshots(self, today: date) -> None:
        """Mémorise le plan des prochains jours : baseline pour détecter les suppressions à l'avance."""
        for offset in range(0, SNAPSHOT_DAYS_AHEAD):
            day = today + timedelta(days=offset)
            for w in self.cfg.windows:
                self.store.save_snapshot(
                    day, w.name, today, static_gtfs.plan_for(self.store, self.cfg, w, day)
                )

    def gtfs_due(self, now: datetime) -> bool:
        if not self.store.gtfs_loaded():
            return True
        imported = self.store.get_meta("gtfs_imported_at")
        if not imported or now - datetime.fromisoformat(imported) > timedelta(hours=26):
            return True
        for t in self.cfg.feeds.static_refresh_times:
            due = datetime.combine(now.date(), t, tzinfo=self.tz)
            if due <= now < due + timedelta(hours=2) and datetime.fromisoformat(imported) < due:
                return True
        return False

    # --- baseline ------------------------------------------------------------
    def removed_trains(self, w: Window, day: date, plan_nos: set[str]) -> list[PlannedTrain]:
        """Trains habituels (snapshot antérieur, sinon même jour S-1) absents du plan du jour."""
        base = self.store.snapshot(day, w.name, captured_before=day)
        if base is None:
            base = self.store.snapshot(
                day - timedelta(days=7), w.name, captured_before=day + timedelta(days=1), latest=True
            )
        if not base:
            return []
        all_nos = {
            r["train_no"]
            for r in self.store.od_rows(
                day, self.cfg.station_code(w.origin), self.cfg.station_code(w.destination)
            )
        } | plan_nos

        out = []
        for no, dep, arr in base:
            if no in all_nos:
                continue
            ref = dep.astimezone(self.tz).date()

            def on_day(t: datetime, ref: date = ref) -> datetime:
                """Reporte l'heure locale sur `day` (sûr vis-à-vis du changement d'heure)."""
                local = t.astimezone(self.tz)
                return datetime.combine(day + (local.date() - ref), local.time(), tzinfo=self.tz)

            out.append(PlannedTrain(day, no, "", on_day(dep), on_day(arr), Mode.TRAIN))
        return sorted(out, key=lambda t: t.dep)

    # --- temps réel ----------------------------------------------------------
    async def fetch_feeds(
        self, now: datetime, wanted_nos: set[str]
    ) -> tuple[FeedSnapshot | None, AlertSnapshot | None]:
        tu_res, sa_res = await asyncio.gather(
            fetch(self.client, self.cfg.feeds.trip_updates),
            fetch(self.client, self.cfg.feeds.service_alerts),
            return_exceptions=True,
        )
        feed: FeedSnapshot | None = None
        fh = self.feed_health
        if isinstance(tu_res, BaseException):
            fh.consecutive_errors += 1
            fh.last_error = str(tu_res)
            log.warning(
                "flux trip updates en erreur", extra={"error": str(tu_res), "count": fh.consecutive_errors}
            )
        else:
            try:
                feed = decode_trip_updates(tu_res, self.tz, codes=self.codes, train_nos=wanted_nos)
                fh.consecutive_errors = 0
                fh.feed_ts = feed.feed_ts
            except Exception as exc:  # protobuf invalide
                fh.consecutive_errors += 1
                fh.last_error = f"décodage: {exc}"
        alerts: AlertSnapshot | None = None
        if isinstance(sa_res, BaseException):
            log.warning("flux service alerts en erreur", extra={"error": str(sa_res)})
            if self.last_alerts and now - self.last_alerts.feed_ts < timedelta(minutes=10):
                alerts = self.last_alerts
        else:
            try:
                alerts = decode_alerts(sa_res, self.tz, train_nos=wanted_nos, codes=self.codes)
                self.last_alerts = alerts
            except Exception as exc:
                log.warning("décodage alertes en échec", extra={"error": str(exc)})

        age = fh.age_s(now)
        stale = feed is not None and age is not None and age > self.cfg.polling.stale_after_s
        if stale:
            fh.last_error = f"flux périmé ({age} s)"
        available = feed is not None and not stale
        if available:
            fh.last_ok = now
            fh.unavailable = False
        elif stale or fh.consecutive_errors >= self.cfg.polling.error_threshold:
            fh.unavailable = True
        return (feed if available else None), alerts

    async def handle_feed_status(self, now: datetime) -> None:
        fh = self.feed_health
        if fh.unavailable and not fh.notified_down:
            fh.notified_down = await self.notify(
                Notification(
                    title="📡 Données temps réel SNCF indisponibles",
                    message=f"Les alertes sont suspendues ({fh.last_error[:120]}). "
                    "Aucune suppression ne sera déduite d'une absence de données.",
                    priority=2,
                    tags=["train", "warning"],
                    click=self.cfg.ntfy.click_url,
                ),
                now,
            )
        elif (
            not fh.unavailable
            and fh.notified_down
            and await self.notify(
                Notification(
                    title="📡 Données temps réel SNCF rétablies",
                    message="La surveillance reprend normalement.",
                    priority=2,
                    tags=["train", "white_check_mark"],
                    click=self.cfg.ntfy.click_url,
                ),
                now,
            )
        ):
            fh.notified_down = False

    async def poll_cycle(
        self, now: datetime, *, feed_override: tuple[FeedSnapshot | None, AlertSnapshot | None] | None = None
    ) -> dict[str, Any]:
        active = self.active_windows(now)
        self.health.active_windows = [w.name for w, _ in active]
        if not active:
            return {"active": []}
        plans: dict[str, tuple[list[PlannedTrain], list[PlannedTrain], list[PlannedTrain]]] = {}
        wanted: set[str] = set()
        for w, day in active:
            plan = static_gtfs.plan_for(self.store, self.cfg, w, day)
            plan_ext = static_gtfs.plan_for(
                self.store, self.cfg, w, day, extend_min=self.cfg.alternatives_horizon_min
            )
            removed = self.removed_trains(w, day, {p.train_no for p in plan_ext})
            plans[w.name] = (plan, plan_ext, removed)
            wanted |= {p.train_no for p in plan_ext + removed}

        if feed_override is not None:
            feed, alerts = feed_override
            self.feed_health.feed_ts = feed.feed_ts if feed else None
        else:
            feed, alerts = await self.fetch_feeds(now, wanted)
        self.health.last_poll_at = now.isoformat()
        if feed is not None:
            self.health.last_poll_ok_at = now.isoformat()
        self.health.feed_age_s = self.feed_health.age_s(now)
        self.health.feed_unavailable = self.feed_health.unavailable
        await self.handle_feed_status(now)

        summary: dict[str, Any] = {
            "active": [w.name for w, _ in active],
            "feed_entities": feed.entity_count if feed else None,
            "feed_age_s": self.health.feed_age_s,
            "alerts": len(alerts.alerts) if alerts else None,
            "tracked": 0,
            "events": 0,
        }
        for w, day in active:
            plan, plan_ext, removed = plans[w.name]
            origin, dest = self.cfg.station_code(w.origin), self.cfg.station_code(w.destination)
            added: list[PlannedTrain] = []
            if feed is not None:
                lo = datetime.combine(day, w.start, tzinfo=self.tz)
                hi = datetime.combine(day, w.end, tzinfo=self.tz)
                known = {p.train_no for p in plan_ext + removed} | {
                    r["train_no"] for r in self.store.od_rows(day, origin, dest)
                }
                added = find_added(
                    feed,
                    known,
                    day,
                    origin,
                    dest,
                    lo,
                    hi,
                    include_tgv=self.cfg.filters.include_tgv,
                    include_bus=self.cfg.filters.include_bus_replacement,
                )
            prev = self.store.load_states(day, w.name)
            res = evaluate_window(
                self.cfg,
                w,
                day,
                now,
                plan=plan,
                plan_ext=plan_ext,
                removed=removed,
                added=added,
                feed=feed,
                alerts=alerts,
                prev_states=prev,
                alert_seen=self.store.alert_seen,
                names=self.names(w),
            )
            ok = True
            if res.notification is not None:
                ok = await self.notify(res.notification, now)
            elif (
                self.cfg.send_all_clear
                and feed is not None
                and not self.store.was_sent(f"allclear:{day}:{w.name}")
            ):
                o, d = self.names(w)
                ok = await self.notify(
                    Notification(
                        title=f"✅ {w.name} {o}→{d} : tout est normal",
                        message=f"{len(plan)} trains prévus, aucune perturbation annoncée.",
                        priority=2,
                        tags=["train", "white_check_mark"],
                        click=self.cfg.ntfy.click_url,
                        window=w.name,
                    ),
                    now,
                )
                if ok:
                    self.store.mark_sent(f"allclear:{day}:{w.name}", now)
            if ok:  # en cas d'échec d'envoi, on rejouera les transitions au cycle suivant
                self.store.save_states(res.states, now)
                self.store.mark_alerts_seen(res.seen_alert_keys, now)
            summary["tracked"] += res.tracked
            summary["events"] += len(res.events)
        log.info("poll", extra=summary)
        return summary

    # --- résumé de la veille -------------------------------------------------
    def digest_due(self, now: datetime) -> date | None:
        if self.cfg.evening_digest is None:
            return None
        due = datetime.combine(now.date(), self.cfg.evening_digest, tzinfo=self.tz)
        tomorrow = now.date() + timedelta(days=1)
        if due <= now < datetime.combine(now.date(), time(23, 0), tzinfo=self.tz) and not self.store.was_sent(
            f"digest:{tomorrow}"
        ):
            return tomorrow
        return None

    async def send_digest(self, day: date, now: datetime) -> None:
        sections = []
        alerts = self.last_alerts
        if alerts is None or now - alerts.feed_ts > timedelta(minutes=15):
            try:
                raw = await fetch(self.client, self.cfg.feeds.service_alerts)
                alerts = decode_alerts(raw, self.tz)
            except (FeedError, Exception) as exc:  # le résumé part quand même, sans les alertes
                log.warning("alertes indisponibles pour le résumé", extra={"error": str(exc)})
                alerts = None
        for w in self.cfg.windows:
            if not is_active_day(self.cfg, w, day):
                continue
            plan = static_gtfs.plan_for(self.store, self.cfg, w, day)
            removed = self.removed_trains(w, day, {p.train_no for p in plan})
            day_alerts = []
            if alerts is not None:
                seen: set[str] = set()
                for p in plan:
                    for a in alerts.for_train(
                        p.train_no, p.dep - timedelta(hours=3), p.arr + timedelta(hours=1)
                    ):
                        if a.id not in seen and a.duration_h() <= DIGEST_ALERT_MAX_H:
                            seen.add(a.id)
                            day_alerts.append(a)
            sections.append((w, self.names(w), plan, removed, day_alerts))
        n = evening_digest(
            day, sections, send_all_clear=self.cfg.send_all_clear, click=self.cfg.ntfy.click_url
        )
        if n is None or await self.notify(n, now):
            self.store.mark_sent(f"digest:{day}", now)

    # --- boucle ---------------------------------------------------------------
    async def tick(self, now: datetime) -> float:
        """Une itération de la boucle ; renvoie le délai avant la prochaine (secondes)."""
        self.health.last_loop_at = now.isoformat()
        if self.gtfs_due(now):
            await self.refresh_gtfs(now)
        if not self.store.gtfs_loaded():
            return 600.0
        if (day := self.digest_due(now)) is not None:
            await self.send_digest(day, now)
        summary = await self.poll_cycle(now)
        if summary["active"]:
            errors = self.feed_health.consecutive_errors
            interval = float(self.cfg.polling.realtime_interval_s)
            if errors:
                return min(interval * 2.0 ** (errors - 1), float(self.cfg.polling.max_backoff_s))
            return interval
        nxt = self.next_window_start(now)
        until = (nxt - now).total_seconds() if nxt else 300.0
        return max(5.0, min(until, 300.0))

    async def run_forever(self) -> None:
        self.health.started_at = self.now().isoformat()
        while True:
            now = self.now()
            try:
                delay = await self.tick(now)
            except Exception as exc:
                log.exception("erreur dans la boucle", extra={"error": str(exc)})
                self.health.last_error = str(exc)
                delay = 60.0
            await asyncio.sleep(delay)


def data_path(cfg: Config) -> Path:
    return cfg.data_dir / "commute.db"
