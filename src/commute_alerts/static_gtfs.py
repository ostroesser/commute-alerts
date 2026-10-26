"""GTFS théorique SNCF : téléchargement conditionnel, import en streaming filtré, plan du jour."""

from __future__ import annotations

import csv
import io
import logging
import time as _time
import zipfile
from collections.abc import Iterator
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from .config import Config, Window
from .ids import Mode, is_road_trip, parse_stop_id, train_number
from .models import PlannedTrain
from .store import Store

log = logging.getLogger(__name__)

USER_AGENT = "commute-alerts/0.1 (usage personnel non commercial)"


# --- téléchargement ---------------------------------------------------------
def download(cfg: Config, store: Store, *, retries: int = 3) -> Path | None:
    """Télécharge le zip si modifié (ETag / Last-Modified). Renvoie le chemin si nouveau, sinon None."""
    target = cfg.data_dir / "gtfs.zip"
    headers = {"User-Agent": USER_AGENT}
    if target.exists():
        if etag := store.get_meta("gtfs_etag"):
            headers["If-None-Match"] = etag
        if lm := store.get_meta("gtfs_last_modified"):
            headers["If-Modified-Since"] = lm
    tmp = target.with_suffix(".zip.part")
    delay = 10.0
    for attempt in range(1, retries + 1):
        try:
            with httpx.stream(
                "GET", cfg.feeds.static_gtfs, headers=headers, timeout=120, follow_redirects=True
            ) as r:
                if r.status_code == 304:
                    log.info("gtfs inchangé (304)")
                    return None
                r.raise_for_status()
                target.parent.mkdir(parents=True, exist_ok=True)
                with tmp.open("wb") as f:
                    for chunk in r.iter_bytes(1 << 16):
                        f.write(chunk)
                zipfile.ZipFile(tmp).testzip()
                tmp.replace(target)
                store.set_meta("gtfs_etag", r.headers.get("etag", ""))
                store.set_meta("gtfs_last_modified", r.headers.get("last-modified", ""))
                log.info("gtfs téléchargé", extra={"bytes": target.stat().st_size})
                return target
        except (httpx.HTTPError, zipfile.BadZipFile, OSError) as exc:
            log.warning("échec téléchargement gtfs", extra={"attempt": attempt, "error": str(exc)})
            tmp.unlink(missing_ok=True)
            if attempt == retries:
                raise
            _time.sleep(delay)
            delay *= 3
    return None


# --- import en streaming ----------------------------------------------------
def _rows(zf: zipfile.ZipFile, name: str) -> Iterator[dict[str, str]]:
    with zf.open(name) as raw:
        yield from csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig", newline=""))


def _secs(hms: str) -> int | None:
    if not hms:
        return None
    h, m, s = hms.split(":")
    return int(h) * 3600 + int(m) * 60 + int(s)


def import_zip(path: Path, store: Store, station_codes: set[str]) -> dict[str, float]:
    """Importe uniquement les trips qui desservent au moins deux de nos gares."""
    t0 = _time.monotonic()
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        # 1. stop_times (le gros fichier) : on ne garde que nos gares
        per_trip: dict[str, list[tuple[int, str, int, int, str]]] = {}
        for r in _rows(zf, "stop_times.txt"):
            ref = parse_stop_id(r["stop_id"])
            if ref is None or ref.code not in station_codes:
                continue
            arr = _secs(r["arrival_time"])
            dep = _secs(r["departure_time"])
            if arr is None and dep is None:
                continue
            per_trip.setdefault(r["trip_id"], []).append(
                (
                    int(r["stop_sequence"]),
                    ref.code,
                    arr if arr is not None else dep or 0,
                    dep if dep is not None else arr or 0,
                    ref.mode.value,
                )
            )
        per_trip = {k: v for k, v in per_trip.items() if len({s[1] for s in v}) >= 2}

        # 2. trips
        trips: list[tuple[str, str, str, str, int]] = []
        route_of: dict[str, str] = {}
        services: set[str] = set()
        for r in _rows(zf, "trips.txt"):
            tid = r["trip_id"]
            if tid not in per_trip:
                continue
            no = train_number(tid) or r.get("trip_headsign", "").strip()
            if not no:
                continue
            trips.append((tid, no, r["service_id"], "", int(is_road_trip(tid))))
            route_of[tid] = r["route_id"]
            services.add(r["service_id"])

        # 3. routes (nom court : C50, C40+…)
        route_names: dict[str, str] = {}
        if "routes.txt" in names:
            wanted = set(route_of.values())
            for r in _rows(zf, "routes.txt"):
                if r["route_id"] in wanted:
                    route_names[r["route_id"]] = r.get("route_short_name") or r.get("route_long_name", "")
        trips = [(t[0], t[1], t[2], route_names.get(route_of[t[0]], ""), t[4]) for t in trips]

        # 4. calendrier (calendar.txt optionnel + calendar_dates.txt)
        days: dict[str, set[str]] = {s: set() for s in services}
        if "calendar.txt" in names:
            wd = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
            for r in _rows(zf, "calendar.txt"):
                if r["service_id"] not in services:
                    continue
                d = datetime.strptime(r["start_date"], "%Y%m%d").date()
                end = datetime.strptime(r["end_date"], "%Y%m%d").date()
                while d <= end:
                    if r[wd[d.weekday()]] == "1":
                        days[r["service_id"]].add(d.strftime("%Y%m%d"))
                    d += timedelta(days=1)
        if "calendar_dates.txt" in names:
            for r in _rows(zf, "calendar_dates.txt"):
                sid = r["service_id"]
                if sid not in services:
                    continue
                if r["exception_type"] == "1":
                    days[sid].add(r["date"])
                elif r["exception_type"] == "2":
                    days[sid].discard(r["date"])

        # 5. noms de gares
        stops: dict[str, str] = {}
        for r in _rows(zf, "stops.txt"):
            ref = parse_stop_id(r["stop_id"])
            if ref and ref.code in station_codes and ref.code not in stops:
                stops[ref.code] = r["stop_name"]

        feed_version = ""
        if "feed_info.txt" in names:
            for r in _rows(zf, "feed_info.txt"):
                feed_version = r.get("feed_version", "")
                break

    stop_times = [
        (tid, seq, code, arr, dep, mode)
        for tid, sts in per_trip.items()
        for seq, code, arr, dep, mode in sts
        if tid in route_of
    ]
    service_dates = [(sid, d) for sid, ds in days.items() for d in ds]
    store.replace_gtfs(
        stops.items(),
        trips,
        stop_times,
        service_dates,
        meta={"gtfs_imported_at": datetime.now().astimezone().isoformat(), "gtfs_feed_version": feed_version},
    )
    stats = {
        "trips": len(trips),
        "stop_times": len(stop_times),
        "service_dates": len(service_dates),
        "stations_found": len(stops),
        "seconds": round(_time.monotonic() - t0, 1),
    }
    missing = station_codes - stops.keys()
    if missing:
        log.warning("gares absentes du GTFS", extra={"codes": sorted(missing)})
    log.info("gtfs importé", extra=stats)
    return stats


# --- plan du jour -----------------------------------------------------------
def service_time(day: date, secs: int, tz: ZoneInfo) -> datetime:
    """Heure GTFS (secondes depuis « midi − 12 h », éventuellement > 24 h) → datetime aware."""
    noon_utc = datetime.combine(day, time(12), tzinfo=tz).astimezone(UTC)
    return (noon_utc - timedelta(hours=12) + timedelta(seconds=secs)).astimezone(tz)


def _mode_ok(cfg: Config, mode: Mode) -> bool:
    if mode is Mode.HIGH_SPEED:
        return cfg.filters.include_tgv
    if mode is Mode.BUS:
        return cfg.filters.include_bus_replacement
    return True


def plan_for(
    store: Store, cfg: Config, window: Window, day: date, *, extend_min: int = 0
) -> list[PlannedTrain]:
    """Trains théoriques de la fenêtre (départ origine dans [start, end + extend_min])."""
    tz = ZoneInfo(cfg.timezone)
    lo = datetime.combine(day, window.start, tzinfo=tz)
    hi = datetime.combine(day, window.end, tzinfo=tz) + timedelta(minutes=extend_min)
    out: dict[str, PlannedTrain] = {}
    for r in store.od_rows(day, cfg.station_code(window.origin), cfg.station_code(window.destination)):
        mode = Mode.BUS if r["is_road"] else Mode(r["o_mode"])
        if not _mode_ok(cfg, mode):
            continue
        dep = service_time(day, r["dep_s"], tz)
        if not lo <= dep <= hi:
            continue
        arr = service_time(day, r["arr_s"], tz)
        no = r["train_no"]
        if no in out and out[no].dep <= dep:
            continue
        out[no] = PlannedTrain(day, no, r["trip_id"], dep, arr, mode, r["route_name"])
    return sorted(out.values(), key=lambda t: t.dep)
