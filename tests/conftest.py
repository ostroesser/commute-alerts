from __future__ import annotations

import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from google.transit import gtfs_realtime_pb2 as rt

from commute_alerts.config import Config, Window
from commute_alerts.static_gtfs import import_zip
from commute_alerts.store import Store

FIXTURES = Path(__file__).parent / "fixtures"
TZ = ZoneInfo("Europe/Paris")
MONDAY = date(2026, 10, 26)

THI, HET, BET, HOW, LUX = "87191007", "87191163", "82006030", "82002501", "82001000"
STATIONS = {"thionville": THI, "hettange_grande": HET, "bettembourg": BET, "howald": HOW, "luxembourg": LUX}


def at(hhmm: str, day: date = MONDAY) -> datetime:
    h, m = hhmm.split(":")
    return datetime.combine(day, time(int(h), int(m)), tzinfo=TZ)


@pytest.fixture
def gtfs_zip(tmp_path: Path) -> Path:
    out = tmp_path / "gtfs.zip"
    with zipfile.ZipFile(out, "w") as zf:
        for f in (FIXTURES / "gtfs_mini").iterdir():
            zf.write(f, f.name)
    return out


def make_config(tmp_path: Path, **overrides: object) -> Config:
    data: dict[str, object] = {
        "data_dir": tmp_path,
        "stations": STATIONS,
        "windows": [
            Window(
                name="Matin",
                origin="thionville",
                destination="luxembourg",
                start=time(6, 30),
                end=time(8, 30),
            ),
            Window(
                name="Retour",
                origin="luxembourg",
                destination="thionville",
                start=time(7, 30),
                end=time(8, 45),
            ),
        ],
        "skip_holidays": ["FR", "LU"],
    }
    data.update(overrides)
    return Config.model_validate(data)


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return make_config(tmp_path)


@pytest.fixture
def store(cfg: Config, gtfs_zip: Path) -> Iterator[Store]:
    s = Store(":memory:")
    import_zip(gtfs_zip, s, set(cfg.stations.values()))
    yield s
    s.close()


# --- construction de flux GTFS-RT synthétiques ---------------------------------------
@dataclass
class S:
    """Arrêt RT : code gare, heure locale HH:MM (réelle), retard (min), SKIPPED, libellé de mode."""

    code: str
    hhmm: str
    delay_min: int = 0
    skipped: bool = False
    label: str = "Train TER"


@dataclass
class T:
    no: str
    stops: list[S] = field(default_factory=list)
    day: date = MONDAY
    canceled: bool = False
    added: bool = False
    road: bool = False
    trip_id: str | None = None


def tu_feed(ts: datetime, trips: list[T]) -> bytes:
    msg = rt.FeedMessage()
    msg.header.gtfs_realtime_version = "1.0"
    msg.header.timestamp = int(ts.timestamp())
    for t in trips:
        e = msg.entity.add()
        suffix = "R" if t.road else "F"
        e.id = t.trip_id or f"OCESN{t.no}{suffix}1187_{suffix}:TER:FR:Line::RT::x:20261211"
        tu = e.trip_update
        tu.trip.trip_id = e.id
        tu.trip.start_date = t.day.strftime("%Y%m%d")
        if t.canceled:
            tu.trip.schedule_relationship = rt.TripDescriptor.CANCELED
        elif t.added:
            tu.trip.schedule_relationship = rt.TripDescriptor.ADDED
        for s in t.stops:
            u = tu.stop_time_update.add()
            u.stop_id = f"StopPoint:OCE{s.label}-{s.code}"
            when = int(at(s.hhmm, t.day).timestamp())
            u.arrival.time = when
            u.arrival.delay = s.delay_min * 60
            u.departure.time = when
            u.departure.delay = s.delay_min * 60
            if s.skipped:
                u.schedule_relationship = rt.TripUpdate.StopTimeUpdate.SKIPPED
    return msg.SerializeToString()


def sa_feed(
    ts: datetime, alerts: list[tuple[str, list[str], str, datetime | None, datetime | None]]
) -> bytes:
    """alerts = [(id, [n° de train], texte FR, début, fin)]."""
    msg = rt.FeedMessage()
    msg.header.gtfs_realtime_version = "1.0"
    msg.header.timestamp = int(ts.timestamp())
    for aid, nos, text, start, end in alerts:
        e = msg.entity.add()
        e.id = aid
        a = e.alert
        if start or end:
            p = a.active_period.add()
            if start:
                p.start = int(start.timestamp())
            if end:
                p.end = int(end.timestamp())
        for no in nos:
            a.informed_entity.add().trip.trip_id = f"OCESN{no}F"
        tr = a.header_text.translation.add()
        tr.language, tr.text = "fr", "Plus d'information :"
        tr = a.description_text.translation.add()
        tr.language, tr.text = "fr", f"<p>{text}</p>"
        tr = a.description_text.translation.add()
        tr.language, tr.text = "en", "english text"
    return msg.SerializeToString()


def utc(ts: datetime) -> datetime:
    return ts.astimezone(UTC)
