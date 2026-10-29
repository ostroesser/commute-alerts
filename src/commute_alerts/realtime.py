"""Flux GTFS-RT SNCF : téléchargement, décodage, normalisation (Trip Updates + Service Alerts)."""

from __future__ import annotations

import hashlib
import html
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import httpx
from google.transit import gtfs_realtime_pb2 as rt

from .ids import Mode, is_road_trip, parse_stop_id, train_number

_TU = rt.TripUpdate
_TD = rt.TripDescriptor


@dataclass(frozen=True, slots=True)
class RtStop:
    code: str
    mode: Mode
    arr: datetime | None
    dep: datetime | None
    arr_delay_s: int | None
    dep_delay_s: int | None
    skipped: bool

    @property
    def scheduled_ref(self) -> datetime | None:
        """Horaire théorique implicite (time − delay), utile pour propager un retard."""
        for t, d in ((self.dep, self.dep_delay_s), (self.arr, self.arr_delay_s)):
            if t is not None and d is not None:
                return datetime.fromtimestamp(t.timestamp() - d, t.tzinfo)
        return None


@dataclass(slots=True)
class RtTrip:
    trip_id: str
    train_no: str
    start_date: date | None
    canceled: bool
    added: bool
    road: bool
    stops: list[RtStop]

    def stop(self, code: str) -> RtStop | None:
        for s in self.stops:
            if s.code == code:
                return s
        return None

    def stop_index(self, code: str) -> int | None:
        for i, s in enumerate(self.stops):
            if s.code == code:
                return i
        return None

    def delay_s_before(self, when: datetime) -> int | None:
        """Retard de la dernière mise à jour dont l'horaire théorique précède `when` (propagation)."""
        best: tuple[datetime, int] | None = None
        for s in self.stops:
            ref = s.scheduled_ref
            d = s.dep_delay_s if s.dep_delay_s is not None else s.arr_delay_s
            if ref is None or d is None or ref > when:
                continue
            if best is None or ref >= best[0]:
                best = (ref, d)
        return best[1] if best else None


@dataclass(frozen=True, slots=True)
class RtAlert:
    id: str
    train_nos: frozenset[str]
    stop_codes: frozenset[str]
    periods: tuple[tuple[datetime | None, datetime | None], ...]
    header: str
    text: str
    cause: str

    @property
    def digest(self) -> str:
        return hashlib.sha1(f"{self.header}\n{self.text}".encode(), usedforsecurity=False).hexdigest()[:12]

    @property
    def key(self) -> str:
        return f"{self.id}#{self.digest}"

    @property
    def summary(self) -> str:
        """Texte le plus informatif : description, sinon titre."""
        generic = {"plus d'information :", "plus d'information", "information", ""}
        if self.text:
            return self.text
        return "" if self.header.lower() in generic else self.header

    def active_between(self, start: datetime, end: datetime) -> bool:
        if not self.periods:
            return True
        for p_start, p_end in self.periods:
            if (p_start is None or p_start <= end) and (p_end is None or p_end >= start):
                return True
        return False

    def duration_h(self) -> float:
        """Durée totale couverte (∞ si une borne est ouverte)."""
        total = 0.0
        for s, e in self.periods:
            if s is None or e is None:
                return float("inf")
            total += (e - s).total_seconds() / 3600
        return total


@dataclass(slots=True)
class FeedSnapshot:
    feed_ts: datetime
    trips: list[RtTrip] = field(default_factory=list)
    by_trip_id: dict[str, RtTrip] = field(default_factory=dict)
    by_key: dict[tuple[str, date | None], RtTrip] = field(default_factory=dict)
    entity_count: int = 0

    def find(self, trip_id: str, train_no: str, service_date: date) -> RtTrip | None:
        """trip_id exact, sinon (n° de train, date de circulation)."""
        t = self.by_trip_id.get(trip_id)
        if t is not None and (t.start_date in (None, service_date)):
            return t
        return self.by_key.get((train_no, service_date))


@dataclass(slots=True)
class AlertSnapshot:
    feed_ts: datetime
    alerts: list[RtAlert] = field(default_factory=list)
    entity_count: int = 0

    def for_train(self, train_no: str, start: datetime, end: datetime) -> list[RtAlert]:
        return [a for a in self.alerts if train_no in a.train_nos and a.active_between(start, end)]


# --- décodage ---------------------------------------------------------------
def _ts(v: int, tz: ZoneInfo) -> datetime | None:
    return datetime.fromtimestamp(v, UTC).astimezone(tz) if v else None


def _sdate(v: str) -> date | None:
    try:
        return datetime.strptime(v, "%Y%m%d").date() if v else None
    except ValueError:
        return None


def parse_message(raw: bytes) -> rt.FeedMessage:
    msg = rt.FeedMessage()
    msg.ParseFromString(raw)
    return msg


def decode_trip_updates(
    raw: bytes, tz: ZoneInfo, *, codes: set[str] | None = None, train_nos: set[str] | None = None
) -> FeedSnapshot:
    """Décode le flux TU. Si `codes`/`train_nos` sont fournis, ne garde que les trips concernés."""
    msg = parse_message(raw)
    snap = FeedSnapshot(
        feed_ts=_ts(msg.header.timestamp, tz) or datetime.fromtimestamp(0, tz), entity_count=len(msg.entity)
    )
    for ent in msg.entity:
        if not ent.HasField("trip_update"):
            continue
        tu = ent.trip_update
        trip_id = tu.trip.trip_id
        no = train_number(trip_id)
        if no is None:
            continue
        stops: list[RtStop] = []
        for s in tu.stop_time_update:
            ref = parse_stop_id(s.stop_id)
            if ref is None:
                continue
            stops.append(
                RtStop(
                    code=ref.code,
                    mode=ref.mode,
                    arr=_ts(s.arrival.time, tz) if s.HasField("arrival") else None,
                    dep=_ts(s.departure.time, tz) if s.HasField("departure") else None,
                    arr_delay_s=s.arrival.delay
                    if s.HasField("arrival") and s.arrival.HasField("delay")
                    else None,
                    dep_delay_s=s.departure.delay
                    if s.HasField("departure") and s.departure.HasField("delay")
                    else None,
                    skipped=s.HasField("schedule_relationship")
                    and s.schedule_relationship == _TU.StopTimeUpdate.SKIPPED,
                )
            )
        if (codes is not None or train_nos is not None) and not (
            (codes and any(s.code in codes for s in stops)) or (train_nos and no in train_nos)
        ):
            continue
        # Les temps ne sont pas toujours triés : on trie par horaire disponible.
        stops.sort(key=lambda s: s.dep or s.arr or datetime.max.replace(tzinfo=tz))
        sr = tu.trip.schedule_relationship if tu.trip.HasField("schedule_relationship") else _TD.SCHEDULED
        trip = RtTrip(
            trip_id=trip_id,
            train_no=no,
            start_date=_sdate(tu.trip.start_date),
            canceled=sr == _TD.CANCELED,
            added=sr == _TD.ADDED,
            road=is_road_trip(trip_id),
            stops=stops,
        )
        snap.trips.append(trip)
        snap.by_trip_id[trip_id] = trip
        snap.by_key.setdefault((no, trip.start_date), trip)
    return snap


_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_RE = re.compile(r"</p>|<br\s*/?>|</li>", re.I)
_WS_RE = re.compile(r"[ \t  ]+")


def clean_text(s: str) -> str:
    """HTML SNCF → texte brut sur une ligne."""
    s = _BLOCK_RE.sub(" ", s)
    s = html.unescape(_TAG_RE.sub("", s))
    return _WS_RE.sub(" ", s).strip()


def _fr(ts: rt.TranslatedString) -> str:
    chosen = ""
    for t in ts.translation:
        if t.language.lower().startswith("fr"):
            return clean_text(t.text)
        if not chosen:
            chosen = t.text
    return clean_text(chosen)


_CAUSES_FR = {
    "TECHNICAL_PROBLEM": "problème technique",
    "STRIKE": "grève",
    "DEMONSTRATION": "manifestation",
    "ACCIDENT": "accident",
    "HOLIDAY": "jour férié",
    "WEATHER": "météo",
    "MAINTENANCE": "travaux",
    "CONSTRUCTION": "travaux",
    "POLICE_ACTIVITY": "intervention des forces de l'ordre",
    "MEDICAL_EMERGENCY": "urgence médicale",
}


def decode_alerts(
    raw: bytes, tz: ZoneInfo, *, train_nos: set[str] | None = None, codes: set[str] | None = None
) -> AlertSnapshot:
    msg = parse_message(raw)
    snap = AlertSnapshot(
        feed_ts=_ts(msg.header.timestamp, tz) or datetime.fromtimestamp(0, tz), entity_count=len(msg.entity)
    )
    for ent in msg.entity:
        if not ent.HasField("alert"):
            continue
        a = ent.alert
        nos: set[str] = set()
        stops: set[str] = set()
        for ie in a.informed_entity:
            if ie.HasField("trip") and (n := train_number(ie.trip.trip_id)):
                nos.add(n)
            if ie.stop_id and (ref := parse_stop_id(ie.stop_id)):
                stops.add(ref.code)
        if train_nos is not None and not (nos & train_nos) and not (codes and stops & codes and not nos):
            continue
        snap.alerts.append(
            RtAlert(
                id=ent.id,
                train_nos=frozenset(nos),
                stop_codes=frozenset(stops),
                periods=tuple((_ts(p.start, tz), _ts(p.end, tz)) for p in a.active_period),
                header=_fr(a.header_text),
                text=_fr(a.description_text),
                cause=_CAUSES_FR.get(rt.Alert.Cause.Name(a.cause), ""),
            )
        )
    return snap


# --- réseau -----------------------------------------------------------------
class FeedError(Exception):
    pass


async def fetch(client: httpx.AsyncClient, url: str) -> bytes:
    try:
        r = await client.get(url)
        r.raise_for_status()
    except httpx.HTTPError as exc:
        raise FeedError(f"{url}: {exc}") from exc
    if not r.content:
        raise FeedError(f"{url}: réponse vide")
    return r.content


def alerts_hash(alerts: Iterable[RtAlert]) -> str:
    keys = sorted(a.key for a in alerts)
    return hashlib.sha1("|".join(keys).encode(), usedforsecurity=False).hexdigest()[:12] if keys else ""
