"""Point d'entrée : service (boucle asyncio), outils de diagnostic et rejeu de fixtures."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import health, logs, static_gtfs
from .calendar import holiday_name, is_active_day
from .config import Config, load_config
from .notifier import DryRunNotifier, Notification, Notifier, NtfyNotifier
from .realtime import AlertSnapshot, decode_alerts, decode_trip_updates
from .service import Service, data_path
from .store import Store

log = logging.getLogger("commute_alerts")

_STAMP = re.compile(r"(\d{8}T\d{6})")


def _config_path(arg: str | None) -> Path:
    for candidate in (arg, os.environ.get("COMMUTE_CONFIG"), "/config/config.yaml", "config.yaml"):
        if candidate and Path(candidate).exists():
            return Path(candidate)
    sys.exit("config introuvable : --config, $COMMUTE_CONFIG, /config/config.yaml ou ./config.yaml")


def make_notifier(cfg: Config, dry_run: bool) -> Notifier:
    if dry_run:
        return DryRunNotifier()
    if not cfg.ntfy.configured:
        log.warning("ntfy non configuré : notifications en dry-run (stdout)")
        return DryRunNotifier()
    return NtfyNotifier(cfg.ntfy)


async def run_service(cfg: Config, notifier: Notifier, once: bool) -> None:
    store = Store(data_path(cfg))
    svc = Service(cfg, store, notifier)
    if once:
        delay = await svc.tick(svc.now())
        log.info("tick unique terminé", extra={"next_in_s": round(delay)})
        return
    health.serve(svc.health, cfg.health_port)
    log.info(
        "démarrage",
        extra={"windows": [w.name for w in cfg.windows], "dry_run": isinstance(notifier, DryRunNotifier)},
    )
    await svc.run_forever()


def _stamp(p: Path) -> str:
    m = _STAMP.search(p.name)
    return m.group(1) if m else p.name


def _closest_before(candidates: list[Path], ref: Path, tolerance_s: int = 60) -> Path | None:
    """Dernière capture prise au plus `tolerance_s` après `ref` (les deux flux sont lus à ~1 s d'écart)."""
    try:
        limit = datetime.strptime(_stamp(ref), "%Y%m%dT%H%M%S") + timedelta(seconds=tolerance_s)
    except ValueError:
        return None
    best = None
    for c in candidates:
        try:
            if datetime.strptime(_stamp(c), "%Y%m%dT%H%M%S") <= limit:
                best = c
        except ValueError:
            continue
    return best


async def replay(
    cfg: Config, directory: Path, gtfs_zip: Path, notifier: Notifier, db: str = ":memory:"
) -> Service:
    """Rejoue une série de captures TU/SA horodatées (tu_YYYYmmddTHHMMSS.pb / sa_…pb)."""
    tz = ZoneInfo(cfg.timezone)
    store = Store(db)
    static_gtfs.import_zip(gtfs_zip, store, set(cfg.stations.values()))
    svc = Service(cfg, store, notifier)
    tus = sorted(
        (p for p in directory.iterdir() if re.match(r"(tu|trip_updates)_.*\.pb$", p.name)), key=_stamp
    )
    sas = sorted(
        (p for p in directory.iterdir() if re.match(r"(sa|service_alerts)_.*\.pb$", p.name)), key=_stamp
    )
    if not tus:
        raise SystemExit(f"aucune capture tu_*.pb dans {directory}")
    for tu_path in tus:
        feed = decode_trip_updates(tu_path.read_bytes(), tz, codes=svc.codes)
        sa_path = _closest_before(sas, tu_path)
        alerts: AlertSnapshot | None = decode_alerts(sa_path.read_bytes(), tz) if sa_path else None
        await svc.poll_cycle(feed.feed_ts, feed_override=(feed, alerts))
    return svc


def print_plan(cfg: Config, day: date) -> None:
    store = Store(data_path(cfg))
    if not store.gtfs_loaded():
        sys.exit("GTFS non importé : lancer d'abord --import-gtfs")
    svc = Service(cfg, store, DryRunNotifier())
    hol = holiday_name(cfg, day)
    for w in cfg.windows:
        o, d = svc.names(w)
        active = is_active_day(cfg, w, day)
        print(
            f"\n{w.name} {o}→{d} {w.start:%H:%M}-{w.end:%H:%M} — {day} "
            f"{'surveillé' if active else 'NON surveillé'}{f' ({hol})' if hol else ''}"
        )
        for p in static_gtfs.plan_for(store, cfg, w, day):
            print(f"  {p.dep:%H:%M} {p.train_no:>6} → {p.arr:%H:%M}  {p.mode.value:5} {p.route_name}")
        for p in svc.removed_trains(w, day, set()):
            print(f"  {p.dep:%H:%M} {p.train_no:>6}  ABSENT du plan (baseline)")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="commute-alerts", description=__doc__)
    ap.add_argument("--config")
    ap.add_argument("--dry-run", action="store_true", help="notifications sur stdout au lieu de ntfy")
    ap.add_argument("--once", action="store_true", help="une seule itération puis sortie")
    ap.add_argument("--import-gtfs", action="store_true", help="télécharger/importer le GTFS puis sortir")
    ap.add_argument("--plan", metavar="YYYY-MM-DD", help="afficher le plan d'une date puis sortir")
    ap.add_argument("--replay", metavar="DIR", type=Path, help="rejouer des captures TU/SA")
    ap.add_argument(
        "--gtfs", metavar="ZIP", type=Path, help="zip GTFS pour --replay (défaut : DATA_DIR/gtfs.zip)"
    )
    ap.add_argument("--test-notification", action="store_true", help="envoyer une notification de test")
    args = ap.parse_args(argv)

    logs.setup()
    cfg = load_config(_config_path(args.config))
    notifier = make_notifier(cfg, args.dry_run or bool(args.replay))

    if args.import_gtfs:
        store = Store(data_path(cfg))
        svc = Service(cfg, store, notifier)
        asyncio.run(svc.refresh_gtfs(svc.now(), force=True))
        print(store.stats())
        return
    if args.plan:
        print_plan(cfg, date.fromisoformat(args.plan))
        return
    if args.replay:
        asyncio.run(replay(cfg, args.replay, args.gtfs or cfg.data_dir / "gtfs.zip", notifier))
        return
    if args.test_notification:
        ok = asyncio.run(
            notifier.send(
                Notification(
                    title="🚆 commute-alerts : test",
                    message=f"Notification de test ({datetime.now():%d/%m %H:%M}).",
                    priority=2,
                    tags=["train", "white_check_mark"],
                    click=cfg.ntfy.click_url,
                )
            )
        )
        sys.exit(0 if ok else 1)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run_service(cfg, notifier, args.once))


if __name__ == "__main__":
    main()
