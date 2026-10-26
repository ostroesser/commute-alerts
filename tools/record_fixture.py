"""Enregistre des captures GTFS-RT (Trip Updates + Service Alerts) horodatées pour les tests / --replay.

Usage :
    python tools/record_fixture.py OUT_DIR [--interval 90] [--until HH:MM] [--count N]
                                   [--filter-stations 87191007,82001000,…]

Avec --filter-stations, seules les entités touchant ces gares (TU) ou les trains concernés (SA) sont
conservées : fichiers de quelques Ko, adaptés à tests/fixtures/. Intervalle minimum : 30 s par flux.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from google.transit import gtfs_realtime_pb2 as rt

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from commute_alerts.config import DEFAULT_SERVICE_ALERTS, DEFAULT_TRIP_UPDATES
from commute_alerts.ids import parse_stop_id, train_number

TZ = ZoneInfo("Europe/Paris")


def filter_tu(raw: bytes, codes: set[str]) -> tuple[bytes, set[str]]:
    msg = rt.FeedMessage()
    msg.ParseFromString(raw)
    keep = [
        e
        for e in msg.entity
        if any(
            (ref := parse_stop_id(s.stop_id)) and ref.code in codes for s in e.trip_update.stop_time_update
        )
    ]
    nos = {n for e in keep if (n := train_number(e.trip_update.trip.trip_id))}
    del msg.entity[:]
    msg.entity.extend(keep)
    return msg.SerializeToString(), nos


def filter_sa(raw: bytes, nos: set[str], codes: set[str]) -> bytes:
    msg = rt.FeedMessage()
    msg.ParseFromString(raw)
    keep = []
    for e in msg.entity:
        ies = e.alert.informed_entity
        if any(train_number(ie.trip.trip_id) in nos for ie in ies if ie.HasField("trip")) or any(
            (ref := parse_stop_id(ie.stop_id)) and ref.code in codes for ie in ies if ie.stop_id
        ):
            keep.append(e)
    del msg.entity[:]
    msg.entity.extend(keep)
    return msg.SerializeToString()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("out", type=Path)
    ap.add_argument("--interval", type=int, default=90)
    ap.add_argument("--until", help="HH:MM (heure de Paris)")
    ap.add_argument("--count", type=int, default=1)
    ap.add_argument("--filter-stations", help="codes gare séparés par des virgules")
    ap.add_argument("--tu-url", default=DEFAULT_TRIP_UPDATES)
    ap.add_argument("--sa-url", default=DEFAULT_SERVICE_ALERTS)
    args = ap.parse_args()
    interval = max(30, args.interval)
    codes = set(args.filter_stations.split(",")) if args.filter_stations else None
    args.out.mkdir(parents=True, exist_ok=True)
    n = 0
    with httpx.Client(timeout=60, headers={"User-Agent": "commute-alerts-recorder/0.1"}) as client:
        while True:
            stamp = datetime.now(TZ).strftime("%Y%m%dT%H%M%S")
            try:
                tu = client.get(args.tu_url).raise_for_status().content
                sa = client.get(args.sa_url).raise_for_status().content
                if codes:
                    tu, nos = filter_tu(tu, codes)
                    sa = filter_sa(sa, nos, codes)
                (args.out / f"tu_{stamp}.pb").write_bytes(tu)
                (args.out / f"sa_{stamp}.pb").write_bytes(sa)
                print(f"{stamp} tu={len(tu)}o sa={len(sa)}o", flush=True)
            except httpx.HTTPError as exc:
                print(f"{stamp} erreur : {exc}", flush=True)
            n += 1
            if args.until:
                if datetime.now(TZ).strftime("%H:%M") >= args.until:
                    break
            elif n >= args.count:
                break
            time.sleep(interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
