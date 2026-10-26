from __future__ import annotations

from datetime import date, time
from pathlib import Path

import pytest
from conftest import make_config
from pydantic import ValidationError

from commute_alerts.calendar import holiday_name, is_active_day
from commute_alerts.config import load_config
from commute_alerts.ids import Mode, is_road_trip, parse_stop_id, train_number

ROOT = Path(__file__).resolve().parents[1]


# --- identifiants -------------------------------------------------------------------
@pytest.mark.parametrize(
    ("stop_id", "code", "mode"),
    [
        ("StopArea:OCE87191007", "87191007", Mode.UNKNOWN),
        ("StopPoint:OCETrain TER-87191007", "87191007", Mode.TRAIN),
        ("StopPoint:OCECar TER-82001000", "82001000", Mode.BUS),
        ("StopPoint:OCETGV INOUI-87191007", "87191007", Mode.HIGH_SPEED),
        ("StopPoint:OCEOUIGO-87547000", "87547000", Mode.HIGH_SPEED),
        ("StopPoint:OCEINTERCITES de nuit-87763607", "87763607", Mode.TRAIN),
        ("StopPoint:OCETramTrain-87590372", "87590372", Mode.TRAIN),
    ],
)
def test_parse_stop_id(stop_id: str, code: str, mode: Mode) -> None:
    ref = parse_stop_id(stop_id)
    assert ref is not None
    assert (ref.code, ref.mode) == (code, mode)


@pytest.mark.parametrize("bad", ["", "87191007", "StopArea:OCE8719100", "StopPoint:OCETrain TER-ABC"])
def test_parse_stop_id_rejects(bad: str) -> None:
    assert parse_stop_id(bad) is None


@pytest.mark.parametrize(
    ("trip_id", "no", "road"),
    [
        ("OCESN5741F", "5741", False),
        ("OCESN835755F1187_F:TER:FR:Line::abc:20261211", "835755", False),
        ("OCESN88716F1187_F:TER:FR:Line::5728B123:87191007:82001000:6:830:20261211", "88716", False),
        ("OCESA881201F5111_F:TER:FR:Line::96809d68::87755447:87756486:29:827:20261106", "881201", False),
        ("OCEEA848610F5235_F:TER:FR:Line::8440e055", "848610", False),
        ("OCELO859603F5270_F:TT:FR:Line::c1be0e08", "859603", False),
        ("OCESN434341R1187_R:CTE:FR:Line::E0A47A78", "434341", True),
        ("OCESN56255R", "56255", True),
    ],
)
def test_train_number(trip_id: str, no: str, road: bool) -> None:
    assert train_number(trip_id) == no
    assert is_road_trip(trip_id) is road


def test_train_number_unknown() -> None:
    assert train_number("FOO123") is None


# --- calendrier ----------------------------------------------------------------------
def test_calendar_weekdays_and_holidays(tmp_path: Path) -> None:
    cfg = make_config(tmp_path)
    w = cfg.windows[0]
    assert is_active_day(cfg, w, date(2026, 10, 26))  # lundi
    assert not is_active_day(cfg, w, date(2026, 10, 31))  # samedi
    assert not is_active_day(cfg, w, date(2026, 11, 11))  # Armistice (FR)
    assert holiday_name(cfg, date(2026, 11, 11)) == "Armistice (FR)"
    assert not is_active_day(cfg, w, date(2026, 12, 25))  # Noël (FR + LU)
    assert not is_active_day(cfg, w, date(2028, 5, 9))  # Journée de l'Europe (LU uniquement, mardi)
    assert holiday_name(cfg, date(2028, 5, 9)) is not None


def test_calendar_skip_only_lu(tmp_path: Path) -> None:
    cfg = make_config(tmp_path, skip_holidays=["LU"])
    assert is_active_day(cfg, cfg.windows[0], date(2026, 11, 11))  # Armistice : pas férié au LU


def test_calendar_moselle_subdivision(tmp_path: Path) -> None:
    cfg = make_config(tmp_path, skip_holidays=["FR-57"])
    assert holiday_name(cfg, date(2027, 3, 26)) is not None  # Vendredi saint en Moselle
    assert holiday_name(make_config(tmp_path, skip_holidays=["FR"]), date(2027, 3, 26)) is None


def test_calendar_pause(tmp_path: Path) -> None:
    cfg = make_config(tmp_path, pause_until=date(2026, 10, 30))
    assert not is_active_day(cfg, cfg.windows[0], date(2026, 10, 27))
    assert is_active_day(cfg, cfg.windows[0], date(2026, 11, 2))


# --- configuration -------------------------------------------------------------------
def test_example_config_loads_with_env() -> None:
    env = {"NTFY_URL": "https://ntfy.example.org", "NTFY_TOPIC": "placeholder"}
    cfg = load_config(ROOT / "config.example.yaml", env=env)
    assert cfg.ntfy.url == "https://ntfy.example.org"
    assert cfg.ntfy.topic == "placeholder"
    assert cfg.ntfy.token is None  # ${NTFY_TOKEN} non défini → None
    assert cfg.windows[0].start == time(6, 30)
    assert cfg.stations["luxembourg"] == "82001000"
    assert cfg.feeds.static_refresh_times == [time(3, 30), time(17, 40)]


def test_example_config_without_env_is_unconfigured() -> None:
    cfg = load_config(ROOT / "config.example.yaml", env={})
    assert not cfg.ntfy.configured


def test_env_overrides(tmp_path: Path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text(
        "stations: {a: '87191007', b: '82001000'}\n"
        "windows: [{name: M, origin: a, destination: b, start: 06:30, end: '08:30'}]\n"
        "ntfy: {url: https://x.example.org, topic: t1}\n",
        encoding="utf-8",
    )
    cfg = load_config(p, env={"NTFY_TOPIC": "t2", "DATA_DIR": str(tmp_path)})
    assert cfg.ntfy.topic == "t2"
    assert cfg.data_dir == tmp_path
    assert cfg.windows[0].start == time(6, 30)  # 06:30 non quoté (entier sexagésimal YAML 1.1)


def test_config_validation(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="gare inconnue"):
        make_config(
            tmp_path,
            windows=[
                {
                    "name": "X",
                    "origin": "nowhere",
                    "destination": "luxembourg",
                    "start": "06:00",
                    "end": "07:00",
                }
            ],
        )
    with pytest.raises(ValidationError, match="8 chiffres"):
        make_config(tmp_path, stations={"thionville": "8719100", "luxembourg": "82001000"}, windows=[])
    with pytest.raises(ValidationError):
        make_config(tmp_path, polling={"realtime_interval_s": 10})
