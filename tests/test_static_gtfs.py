from __future__ import annotations

from datetime import date, datetime, time
from pathlib import Path

from conftest import MONDAY, TZ, at, make_config

from commute_alerts.config import Config
from commute_alerts.ids import Mode
from commute_alerts.static_gtfs import import_zip, plan_for, service_time
from commute_alerts.store import Store


def test_import_keeps_only_our_trips(cfg: Config, store: Store) -> None:
    st = store.stats()
    assert st["gtfs_trips"] == 15
    assert store.stop_name("82001000") == "Luxembourg"
    assert store.stop_name("87192039") is None  # Metz : hors périmètre, non importé


def test_plan_morning(cfg: Config, store: Store) -> None:
    plan = plan_for(store, cfg, cfg.windows[0], MONDAY)
    assert [(p.dep.strftime("%H:%M"), p.train_no) for p in plan] == [
        ("06:36", "88708"),
        ("07:18", "88712"),
        ("07:28", "88501"),
        ("07:36", "88804"),
        ("07:48", "88714"),
        ("08:06", "88806"),
        ("08:18", "88716"),
        ("08:27", "88505"),
    ]  # 2801 (TGV) exclu, 88713 (sens inverse) exclu, 88718 hors fenêtre
    p = plan[0]
    assert p.arr == at("07:03")
    assert p.mode is Mode.TRAIN
    assert p.route_name == "C50"


def test_plan_extended_and_tgv(tmp_path: Path, gtfs_zip: Path) -> None:
    cfg = make_config(tmp_path, filters={"include_tgv": True})
    store = Store(":memory:")
    import_zip(gtfs_zip, store, set(cfg.stations.values()))
    nos = [p.train_no for p in plan_for(store, cfg, cfg.windows[0], MONDAY, extend_min=60)]
    assert "2801" in nos and "88718" in nos


def test_plan_calendar_exceptions_and_bus(cfg: Config, store: Store) -> None:
    tue = date(2026, 10, 27)
    plan = {p.train_no: p for p in plan_for(store, cfg, cfg.windows[0], tue)}
    assert "88804" not in plan  # service NO27 ne circule pas le 27
    assert plan["434341"].mode is Mode.BUS  # car planifié (suffixe R)
    assert plan_for(store, cfg, cfg.windows[0], date(2026, 10, 31)) == []  # samedi : aucun service


def test_plan_bus_excluded(tmp_path: Path, gtfs_zip: Path) -> None:
    cfg = make_config(tmp_path, filters={"include_bus_replacement": False})
    store = Store(":memory:")
    import_zip(gtfs_zip, store, set(cfg.stations.values()))
    assert "434341" not in {p.train_no for p in plan_for(store, cfg, cfg.windows[0], date(2026, 10, 27))}


def test_service_time_dst() -> None:
    # Jour du passage à l'heure d'hiver : l'origine GTFS est « midi − 12 h » = 01:00 locale (CEST→CET).
    d = date(2026, 10, 25)
    assert service_time(d, 12 * 3600, TZ) == datetime.combine(d, time(12), tzinfo=TZ)
    assert service_time(MONDAY, 7 * 3600 + 28 * 60, TZ) == at("07:28")
    assert service_time(MONDAY, 25 * 3600, TZ) == at("01:00", date(2026, 10, 27))


def test_snapshots(cfg: Config, store: Store) -> None:
    plan = plan_for(store, cfg, cfg.windows[0], MONDAY)
    store.save_snapshot(MONDAY, "Matin", date(2026, 10, 20), plan)
    store.save_snapshot(MONDAY, "Matin", date(2026, 10, 24), plan[:2])
    snap = store.snapshot(MONDAY, "Matin", captured_before=MONDAY)
    assert snap is not None and len(snap) == len(plan)  # le plus ancien
    latest = store.snapshot(MONDAY, "Matin", captured_before=MONDAY, latest=True)
    assert latest is not None and len(latest) == 2
    assert store.snapshot(MONDAY, "Soir", captured_before=MONDAY) is None
