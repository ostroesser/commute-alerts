"""Exploration live des flux GTFS-RT SNCF (étape 1, avant tout code applicatif).

Usage :
    python tools/explore_feeds.py [--save-dir DIR] [--from-dir DIR] [--siri]

- Télécharge Trip Updates + Service Alerts (une requête par flux),
  ou relit des .pb déjà sauvegardés avec --from-dir.
- Affiche fraîcheur, volumétrie, exemples, entités touchant nos gares,
  couverture de la regex n° de train, n° vus sur l'axe, en-têtes de cache.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from google.transit import gtfs_realtime_pb2

FEEDS = {
    "trip_updates": "https://proxy.transport.data.gouv.fr/resource/sncf-gtfs-rt-trip-updates",
    "service_alerts": "https://proxy.transport.data.gouv.fr/resource/sncf-gtfs-rt-service-alerts",
}
SIRI_SX = "https://proxy.transport.data.gouv.fr/resource/sncf-siri-lite-situation-exchange"

STATIONS = {
    "87191007": "Thionville",
    "87191163": "Hettange-Grande",
    "82006030": "Bettembourg",
    "82002501": "Howald",
    "82001000": "Luxembourg",
}
TZ = ZoneInfo("Europe/Paris")
TRAIN_RE = re.compile(r"^OCESN(\d+)F")
STOP_RE = re.compile(r"^(?:StopArea:OCE|StopPoint:OCE(?P<mode>.+?)-)(?P<code>\d{8})$")
CACHE_HEADERS = ("etag", "last-modified", "cache-control", "age", "expires", "content-length")


def fmt_ts(ts: int) -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d %H:%M:%S")


def norm_stop(stop_id: str) -> tuple[str | None, str]:
    m = STOP_RE.match(stop_id)
    if not m:
        return None, "?"
    return m.group("code"), (m.group("mode") or "StopArea")


def train_no(trip_id: str) -> str | None:
    m = TRAIN_RE.match(trip_id)
    return m.group(1) if m else None


def fetch(name: str, url: str, save_dir: Path) -> bytes:
    t0 = time.monotonic()
    r = httpx.get(url, timeout=60, follow_redirects=True)
    dt = time.monotonic() - t0
    print(f"\n### {name}: HTTP {r.status_code} en {dt:.1f}s, {len(r.content)} octets")
    for h in CACHE_HEADERS:
        print(f"    {h}: {r.headers.get(h, '<absent>')}")
    r.raise_for_status()
    stamp = datetime.now(TZ).strftime("%Y%m%dT%H%M%S")
    path = save_dir / f"{name}_{stamp}.pb"
    path.write_bytes(r.content)
    print(f"    sauvegardé : {path}")
    # Requête conditionnelle : le proxy honore-t-il If-Modified-Since / If-None-Match ?
    cond: dict[str, str] = {}
    if etag := r.headers.get("etag"):
        cond["If-None-Match"] = etag
    if lm := r.headers.get("last-modified"):
        cond["If-Modified-Since"] = lm
    if cond:
        print(f"    (requête conditionnelle possible : {cond} — non testée pour respecter la limite 30 s)")
    else:
        print("    pas de validateur de cache → requêtes conditionnelles impossibles")
    return r.content


def parse(raw: bytes) -> gtfs_realtime_pb2.FeedMessage:
    msg = gtfs_realtime_pb2.FeedMessage()
    msg.ParseFromString(raw)
    return msg


def header_info(name: str, msg: gtfs_realtime_pb2.FeedMessage) -> None:
    ts = msg.header.timestamp
    age = int(time.time()) - ts if ts else None
    print(f"\n=== {name}")
    print(
        f"  gtfs_realtime_version={msg.header.gtfs_realtime_version} "
        f"incrementality={msg.header.incrementality}"
    )
    print(f"  header.timestamp={ts} ({fmt_ts(ts)}), âge={age}s")
    print(f"  entités={len(msg.entity)}")


def explore_trip_updates(msg: gtfs_realtime_pb2.FeedMessage) -> set[str]:
    header_info("TRIP UPDATES", msg)
    tus = [e for e in msg.entity if e.HasField("trip_update")]
    print(f"  entités trip_update={len(tus)}")

    print("\n  --- 3 exemples bruts (tronqués)")
    for e in tus[:3]:
        txt = str(e)
        print("    " + "\n    ".join(txt.splitlines()[:25]))
        print("    ...")

    # Couverture regex
    no_match = [e.trip_update.trip.trip_id for e in tus if not train_no(e.trip_update.trip.trip_id)]
    print(f"\n  --- regex ^OCESN(\\d+)F : {len(tus) - len(no_match)}/{len(tus)} trip_id matchent")
    for t in no_match[:10]:
        print(f"    non matché : {t!r}")

    # Formes de trip_id
    shapes = Counter(re.sub(r"\d", "9", e.trip_update.trip.trip_id)[:40] for e in tus)
    print("\n  --- formes de trip_id (chiffres→9, 40 premiers car.)")
    for s, n in shapes.most_common(8):
        print(f"    {n:5d}  {s}")

    # stop_id : formes et modes
    unknown_stops: Counter[str] = Counter()
    modes: Counter[str] = Counter()
    sr_trip: Counter[str] = Counter()
    sr_stu: Counter[str] = Counter()
    has_delay = has_time = 0
    for e in tus:
        tu = e.trip_update
        sr_trip[
            gtfs_realtime_pb2.TripDescriptor.ScheduleRelationship.Name(tu.trip.schedule_relationship)
            if tu.trip.HasField("schedule_relationship")
            else "<absent>"
        ] += 1
        for s in tu.stop_time_update:
            code, mode = norm_stop(s.stop_id)
            if code is None:
                unknown_stops[re.sub(r"\d", "9", s.stop_id)] += 1
            modes[mode] += 1
            sr_stu[
                gtfs_realtime_pb2.TripUpdate.StopTimeUpdate.ScheduleRelationship.Name(s.schedule_relationship)
                if s.HasField("schedule_relationship")
                else "<absent>"
            ] += 1
            for ev in (s.arrival, s.departure):
                has_delay += ev.HasField("delay")
                has_time += ev.HasField("time")
    print(f"\n  --- trip.schedule_relationship : {dict(sr_trip)}")
    print(f"  --- stop_time_update.schedule_relationship : {dict(sr_stu)}")
    print(f"  --- StopTimeEvent avec time={has_time}, avec delay={has_delay}")
    print(f"  --- modes déduits des stop_id : {dict(modes.most_common())}")
    for s, n in unknown_stops.most_common(5):
        print(f"    stop_id non normalisé : {n:5d}  {s}")

    # Entités touchant nos gares
    print("\n  --- entités desservant nos gares")
    axis: set[str] = set()
    rows = []
    for e in tus:
        tu = e.trip_update
        ours = []
        for s in tu.stop_time_update:
            code, mode = norm_stop(s.stop_id)
            if code in STATIONS:
                t = s.departure.time or s.arrival.time
                d = (
                    s.departure.delay
                    if s.departure.HasField("delay")
                    else (s.arrival.delay if s.arrival.HasField("delay") else None)
                )
                sr = (
                    gtfs_realtime_pb2.TripUpdate.StopTimeUpdate.ScheduleRelationship.Name(
                        s.schedule_relationship
                    )
                    if s.HasField("schedule_relationship")
                    else ""
                )
                ours.append(
                    f"{STATIONS[code]}[{mode}] {fmt_ts(t)[11:16]}"
                    f"{'' if d is None else f' d={d // 60:+d}m'}{' ' + sr if sr else ''}"
                )
        if ours:
            no = train_no(tu.trip.trip_id) or "?"
            axis.add(no)
            tsr = (
                gtfs_realtime_pb2.TripDescriptor.ScheduleRelationship.Name(tu.trip.schedule_relationship)
                if tu.trip.HasField("schedule_relationship")
                else ""
            )
            first_t = min(
                (
                    s.departure.time or s.arrival.time
                    for s in tu.stop_time_update
                    if (s.departure.time or s.arrival.time)
                ),
                default=0,
            )
            rows.append((first_t, no, tu.trip.trip_id, tu.trip.start_date, tsr, ours))
    for _, no, tid, sd, tsr, ours in sorted(rows):
        print(f"    {no:>7} {sd} {tsr:9} {tid[:60]}")
        print(f"            {' | '.join(ours)}")
    print(f"  total : {len(rows)} trains touchant nos gares")

    by_prefix: dict[str, list[str]] = defaultdict(list)
    for no in sorted(axis):
        by_prefix[no[:3] + "xx" if len(no) == 5 else f"{len(no)}ch"].append(no)
    print("\n  --- n° vus sur l'axe, par préfixe")
    for p, nos in sorted(by_prefix.items()):
        print(f"    {p}: {', '.join(nos)}")
    return axis


def text_fr(ts: gtfs_realtime_pb2.TranslatedString) -> str:
    for t in ts.translation:
        if t.language.lower().startswith("fr"):
            return t.text
    return ts.translation[0].text if ts.translation else ""


def explore_alerts(msg: gtfs_realtime_pb2.FeedMessage, axis: set[str]) -> None:
    header_info("SERVICE ALERTS", msg)
    alerts = [e for e in msg.entity if e.HasField("alert")]
    print(f"  entités alert={len(alerts)}")
    print("\n  --- 3 exemples bruts (tronqués)")
    for e in alerts[:3]:
        print("    " + "\n    ".join(str(e).splitlines()[:30]))
        print("    ...")

    ie_kinds: Counter[str] = Counter()
    langs: Counter[str] = Counter()
    effects: Counter[str] = Counter()
    causes: Counter[str] = Counter()
    hits = []
    for e in alerts:
        a = e.alert
        effects[gtfs_realtime_pb2.Alert.Effect.Name(a.effect)] += 1
        causes[gtfs_realtime_pb2.Alert.Cause.Name(a.cause)] += 1
        for t in a.header_text.translation:
            langs[t.language or "<vide>"] += 1
        nos = set()
        stops = set()
        for ie in a.informed_entity:
            kinds = [f for f in ("trip", "route_id", "stop_id", "agency_id") if ie.HasField(f)]
            ie_kinds["+".join(kinds) or "<vide>"] += 1
            if ie.HasField("trip") and (n := train_no(ie.trip.trip_id)):
                nos.add(n)
            if ie.stop_id:
                code, _ = norm_stop(ie.stop_id)
                if code in STATIONS:
                    stops.add(STATIONS[code])
        if nos & axis or stops:
            hits.append((e, nos, stops))
    print(f"\n  --- informed_entity : {dict(ie_kinds)}")
    print(f"  --- langues header_text : {dict(langs)}")
    print(f"  --- effects : {dict(effects)}")
    print(f"  --- causes : {dict(causes)}")
    print(f"\n  --- alertes concernant l'axe ({len(hits)})")
    for e, nos, stops in hits[:15]:
        a = e.alert
        periods = ", ".join(f"{fmt_ts(p.start)[5:16]}→{fmt_ts(p.end)[5:16]}" for p in a.active_period)
        print(f"    id={e.id} trains={sorted(nos & axis) or sorted(nos)[:5]} gares={sorted(stops)}")
        print(f"      effect={gtfs_realtime_pb2.Alert.Effect.Name(a.effect)} périodes={periods}")
        print(f"      titre : {text_fr(a.header_text)[:160]!r}")
        print(f"      descr : {text_fr(a.description_text)[:300]!r}")


def explore_siri(save_dir: Path, axis: set[str]) -> None:
    r = httpx.get(SIRI_SX, timeout=60)
    print(f"\n### SIRI SX: HTTP {r.status_code}, {len(r.content)} octets")
    (save_dir / "siri_sx.xml").write_bytes(r.content)
    refs = re.findall(r"<(?:\w+:)?Vehic\w*JourneyRef>([^<]+)<", r.text)
    print(f"  balises *JourneyRef : {len(refs)}, exemples : {refs[:5]}")
    tags = Counter(re.findall(r"<(?:\w+:)?(Vehic\w*JourneyRef)>", r.text))
    print(f"  noms de balise : {dict(tags)}")
    on_axis = sorted({x for x in refs if any(n in x for n in axis)})
    print(f"  refs concernant les trains de l'axe : {on_axis[:20]}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--save-dir", type=Path, default=Path("/tmp/commute-explore"))
    ap.add_argument("--from-dir", type=Path, help="relire trip_updates_*.pb / service_alerts_*.pb")
    ap.add_argument("--siri", action="store_true")
    args = ap.parse_args()

    if args.from_dir:
        tu_raw = sorted(args.from_dir.glob("trip_updates_*.pb"))[-1].read_bytes()
        sa_raw = sorted(args.from_dir.glob("service_alerts_*.pb"))[-1].read_bytes()
    else:
        args.save_dir.mkdir(parents=True, exist_ok=True)
        tu_raw = fetch("trip_updates", FEEDS["trip_updates"], args.save_dir)
        sa_raw = fetch("service_alerts", FEEDS["service_alerts"], args.save_dir)

    axis = explore_trip_updates(parse(tu_raw))
    explore_alerts(parse(sa_raw), axis)
    if args.siri:
        explore_siri(args.save_dir, axis)
    return 0


if __name__ == "__main__":
    sys.exit(main())
