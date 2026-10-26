"""Appariement trains planifiés ↔ temps réel et calcul de l'observation courante de chaque train."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .ids import Mode
from .models import PlannedTrain, Status
from .realtime import AlertSnapshot, FeedSnapshot, RtAlert, RtStop, RtTrip

ALERT_LOOKBEHIND = timedelta(hours=3)
ALERT_LOOKAHEAD = timedelta(hours=1)


@dataclass(slots=True)
class Observation:
    """Ce que dit le temps réel d'un train à un instant donné."""

    train_no: str
    planned: PlannedTrain
    status: Status
    in_feed: bool
    delay_min: int = 0
    est_dep: datetime | None = None
    est_arr: datetime | None = None
    mode: Mode = Mode.TRAIN
    alerts: list[RtAlert] = field(default_factory=list)


def _event_time(s: RtStop, prefer_dep: bool) -> datetime | None:
    return (s.dep or s.arr) if prefer_dep else (s.arr or s.dep)


def _estimate(trip: RtTrip, code: str, planned: datetime, prefer_dep: bool) -> datetime | None:
    """Horaire estimé à une gare : temps absolu si présent, sinon retard propagé."""
    s = trip.stop(code)
    if s is not None and (t := _event_time(s, prefer_dep)) is not None:
        return t
    d = trip.delay_s_before(planned)
    return planned + timedelta(seconds=d) if d is not None else None


def _delay_min(est: datetime | None, planned: datetime) -> int:
    if est is None:
        return 0
    return max(0, int((est - planned).total_seconds() // 60))


def observe(
    planned: PlannedTrain,
    trip: RtTrip | None,
    origin: str,
    destination: str,
    thresholds: list[int],
    alerts: list[RtAlert],
) -> Observation:
    if trip is None:
        # Absence du flux ≠ suppression : statut inconnu, le moteur garde l'état précédent.
        return Observation(
            planned.train_no, planned, Status.SCHEDULED, in_feed=False, mode=planned.mode, alerts=alerts
        )
    o = trip.stop(origin)
    d = trip.stop(destination)
    est_dep = _estimate(trip, origin, planned.dep, prefer_dep=True)
    est_arr = _estimate(trip, destination, planned.arr, prefer_dep=False)
    delay = max(_delay_min(est_dep, planned.dep), _delay_min(est_arr, planned.arr))
    rt_bus = trip.road or any(s is not None and s.mode is Mode.BUS for s in (o, d))
    mode = Mode.BUS if rt_bus else planned.mode

    if trip.canceled:
        status = Status.CANCELED
    elif o is not None and o.skipped:
        status = Status.SKIPPED_ORIGIN
    elif d is not None and d.skipped:
        status = Status.SKIPPED_DEST
    elif rt_bus and planned.mode is not Mode.BUS:
        status = Status.BUS_REPLACEMENT
    elif delay >= thresholds[0]:
        status = Status.DELAYED
    else:
        status = Status.SCHEDULED
    return Observation(
        planned.train_no,
        planned,
        status,
        in_feed=True,
        delay_min=delay,
        est_dep=est_dep,
        est_arr=est_arr,
        mode=mode,
        alerts=alerts,
    )


def train_alerts(alerts: AlertSnapshot | None, p: PlannedTrain) -> list[RtAlert]:
    if alerts is None:
        return []
    return alerts.for_train(p.train_no, p.dep - ALERT_LOOKBEHIND, p.arr + ALERT_LOOKAHEAD)


def match_window(
    plan: list[PlannedTrain],
    feed: FeedSnapshot,
    alerts: AlertSnapshot | None,
    origin: str,
    destination: str,
    thresholds: list[int],
) -> dict[str, Observation]:
    out: dict[str, Observation] = {}
    for p in plan:
        trip = (
            feed.find(p.trip_id, p.train_no, p.service_date)
            if p.trip_id
            else feed.by_key.get((p.train_no, p.service_date))
        )
        out[p.train_no] = observe(p, trip, origin, destination, thresholds, train_alerts(alerts, p))
    return out


def find_added(
    feed: FeedSnapshot,
    known_nos: set[str],
    day: date,
    origin: str,
    destination: str,
    lo: datetime,
    hi: datetime,
    *,
    include_tgv: bool,
    include_bus: bool,
) -> list[PlannedTrain]:
    """Trajets temps réel O→D inconnus du plan (ADDED, ou absents du GTFS sans ADDED)."""
    found: list[PlannedTrain] = []
    for trip in feed.trips:
        if trip.train_no in known_nos or trip.canceled or trip.start_date not in (None, day):
            continue
        oi, di = trip.stop_index(origin), trip.stop_index(destination)
        if oi is None or di is None or oi >= di:
            continue
        o, d = trip.stops[oi], trip.stops[di]
        if o.skipped or d.skipped:
            continue
        mode = Mode.BUS if trip.road or o.mode is Mode.BUS else o.mode
        if (mode is Mode.HIGH_SPEED and not include_tgv) or (mode is Mode.BUS and not include_bus):
            continue
        dep = o.scheduled_ref or o.dep or o.arr
        arr = d.scheduled_ref or d.arr or d.dep
        if dep is None or arr is None or not lo <= dep <= hi:
            continue
        found.append(PlannedTrain(day, trip.train_no, trip.trip_id, dep, arr, mode, ""))
    return sorted(found, key=lambda t: t.dep)
