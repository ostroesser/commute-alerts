"""Persistance SQLite (WAL) : plan GTFS filtré, snapshots de baseline, états des trains, dédup."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .ids import Mode
from .models import PlannedTrain, Status, TrainState

MIGRATIONS: list[str] = [
    # v1
    """
    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE gtfs_stops (code TEXT PRIMARY KEY, name TEXT NOT NULL);
    CREATE TABLE gtfs_trips (
        trip_id TEXT PRIMARY KEY, train_no TEXT NOT NULL, service_id TEXT NOT NULL,
        route_name TEXT NOT NULL DEFAULT '', is_road INTEGER NOT NULL DEFAULT 0);
    CREATE INDEX gtfs_trips_no ON gtfs_trips(train_no);
    CREATE TABLE gtfs_stop_times (
        trip_id TEXT NOT NULL, seq INTEGER NOT NULL, code TEXT NOT NULL,
        arr_s INTEGER NOT NULL, dep_s INTEGER NOT NULL, mode TEXT NOT NULL,
        PRIMARY KEY (trip_id, seq));
    CREATE INDEX gtfs_stop_times_code ON gtfs_stop_times(code);
    CREATE TABLE gtfs_service_dates (service_id TEXT NOT NULL, day TEXT NOT NULL,
        PRIMARY KEY (service_id, day));
    CREATE INDEX gtfs_service_dates_day ON gtfs_service_dates(day);
    CREATE TABLE plan_snapshots (
        service_date TEXT NOT NULL, window TEXT NOT NULL, captured_on TEXT NOT NULL,
        train_no TEXT NOT NULL, dep TEXT NOT NULL, arr TEXT NOT NULL,
        PRIMARY KEY (service_date, window, captured_on, train_no));
    CREATE TABLE train_state (
        service_date TEXT NOT NULL, window TEXT NOT NULL, train_no TEXT NOT NULL,
        planned_dep TEXT NOT NULL, planned_arr TEXT NOT NULL, status TEXT NOT NULL,
        delay_min INTEGER NOT NULL, delay_bucket INTEGER NOT NULL,
        est_dep TEXT, est_arr TEXT, alert_hash TEXT NOT NULL, mode TEXT NOT NULL,
        last_notified_at TEXT, extra TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL,
        PRIMARY KEY (service_date, window, train_no));
    CREATE TABLE seen_alerts (alert_key TEXT PRIMARY KEY, first_seen TEXT NOT NULL);
    CREATE TABLE sent_keys (key TEXT PRIMARY KEY, sent_at TEXT NOT NULL);
    CREATE TABLE notification_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, sent_at TEXT NOT NULL, window TEXT,
        title TEXT NOT NULL, message TEXT NOT NULL, priority INTEGER NOT NULL, tags TEXT NOT NULL);
    """,
]


def _dt(v: datetime | None) -> str | None:
    return v.isoformat() if v else None


def _pdt(v: str | None) -> datetime | None:
    return datetime.fromisoformat(v) if v else None


class Store:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def close(self) -> None:
        self.conn.close()

    def _migrate(self) -> None:
        version: int = self.conn.execute("PRAGMA user_version").fetchone()[0]
        for i, sql in enumerate(MIGRATIONS[version:], start=version + 1):
            with self.tx():
                for statement in _split(sql):
                    self.conn.execute(statement)
                self.conn.execute(f"PRAGMA user_version={i}")

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    # --- meta -------------------------------------------------------------
    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    # --- GTFS filtré ------------------------------------------------------
    def replace_gtfs(
        self,
        stops: Iterable[tuple[str, str]],
        trips: Iterable[tuple[str, str, str, str, int]],
        stop_times: Iterable[tuple[str, int, str, int, int, str]],
        service_dates: Iterable[tuple[str, str]],
        meta: dict[str, str],
    ) -> None:
        with self.tx() as c:
            for t in ("gtfs_stops", "gtfs_trips", "gtfs_stop_times", "gtfs_service_dates"):
                c.execute(f"DELETE FROM {t}")  # noqa: S608 (noms de tables constants)
            c.executemany("INSERT INTO gtfs_stops VALUES(?, ?)", stops)
            c.executemany("INSERT INTO gtfs_trips VALUES(?, ?, ?, ?, ?)", trips)
            c.executemany("INSERT OR REPLACE INTO gtfs_stop_times VALUES(?, ?, ?, ?, ?, ?)", stop_times)
            c.executemany("INSERT OR IGNORE INTO gtfs_service_dates VALUES(?, ?)", service_dates)
            for k, v in meta.items():
                self.set_meta(k, v)

    def gtfs_loaded(self) -> bool:
        return self.conn.execute("SELECT 1 FROM gtfs_trips LIMIT 1").fetchone() is not None

    def stop_name(self, code: str) -> str | None:
        row = self.conn.execute("SELECT name FROM gtfs_stops WHERE code=?", (code,)).fetchone()
        return row[0] if row else None

    def od_rows(self, day: date, origin: str, destination: str) -> list[sqlite3.Row]:
        """Trips circulant `day` qui desservent origine puis destination (ordre des séquences)."""
        return self.conn.execute(
            """
            SELECT t.trip_id, t.train_no, t.route_name, t.is_road,
                   o.dep_s AS dep_s, o.mode AS o_mode, d.arr_s AS arr_s, d.mode AS d_mode
            FROM gtfs_trips t
            JOIN gtfs_service_dates sd ON sd.service_id = t.service_id AND sd.day = ?
            JOIN gtfs_stop_times o ON o.trip_id = t.trip_id AND o.code = ?
            JOIN gtfs_stop_times d ON d.trip_id = t.trip_id AND d.code = ? AND d.seq > o.seq
            ORDER BY o.dep_s
            """,
            (day.strftime("%Y%m%d"), origin, destination),
        ).fetchall()

    # --- baseline ---------------------------------------------------------
    def save_snapshot(
        self, service_date: date, window: str, captured_on: date, trains: Iterable[PlannedTrain]
    ) -> None:
        with self.tx() as c:
            c.executemany(
                "INSERT OR IGNORE INTO plan_snapshots VALUES(?, ?, ?, ?, ?, ?)",
                [
                    (
                        service_date.isoformat(),
                        window,
                        captured_on.isoformat(),
                        t.train_no,
                        t.dep.isoformat(),
                        t.arr.isoformat(),
                    )
                    for t in trains
                ],
            )
            # marqueur « snapshot pris » même si le plan est vide
            c.execute(
                "INSERT OR IGNORE INTO plan_snapshots VALUES(?, ?, ?, '', '', '')",
                (service_date.isoformat(), window, captured_on.isoformat()),
            )

    def snapshot(
        self, service_date: date, window: str, captured_before: date, *, latest: bool = False
    ) -> list[tuple[str, datetime, datetime]] | None:
        """Plus ancien (ou plus récent) snapshot pris avant `captured_before` (None si aucun)."""
        agg = "MAX" if latest else "MIN"
        row = self.conn.execute(
            f"SELECT {agg}(captured_on) FROM plan_snapshots"  # noqa: S608 (MIN/MAX constant)
            " WHERE service_date=? AND window=? AND captured_on<?",
            (service_date.isoformat(), window, captured_before.isoformat()),
        ).fetchone()
        if not row or row[0] is None:
            return None
        rows = self.conn.execute(
            "SELECT train_no, dep, arr FROM plan_snapshots "
            "WHERE service_date=? AND window=? AND captured_on=? AND train_no<>''",
            (service_date.isoformat(), window, row[0]),
        ).fetchall()
        return [(r[0], datetime.fromisoformat(r[1]), datetime.fromisoformat(r[2])) for r in rows]

    # --- états ------------------------------------------------------------
    def load_states(self, service_date: date, window: str) -> dict[str, TrainState]:
        rows = self.conn.execute(
            "SELECT * FROM train_state WHERE service_date=? AND window=?", (service_date.isoformat(), window)
        ).fetchall()
        out: dict[str, TrainState] = {}
        for r in rows:
            out[r["train_no"]] = TrainState(
                service_date=date.fromisoformat(r["service_date"]),
                window=r["window"],
                train_no=r["train_no"],
                planned_dep=datetime.fromisoformat(r["planned_dep"]),
                planned_arr=datetime.fromisoformat(r["planned_arr"]),
                status=Status(r["status"]),
                delay_min=r["delay_min"],
                delay_bucket=r["delay_bucket"],
                est_dep=_pdt(r["est_dep"]),
                est_arr=_pdt(r["est_arr"]),
                alert_hash=r["alert_hash"],
                mode=Mode(r["mode"]),
                last_notified_at=_pdt(r["last_notified_at"]),
                extra=json.loads(r["extra"]),
            )
        return out

    def save_states(self, states: Iterable[TrainState], now: datetime) -> None:
        with self.tx() as c:
            c.executemany(
                "INSERT OR REPLACE INTO train_state VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        s.service_date.isoformat(),
                        s.window,
                        s.train_no,
                        s.planned_dep.isoformat(),
                        s.planned_arr.isoformat(),
                        s.status.value,
                        s.delay_min,
                        s.delay_bucket,
                        _dt(s.est_dep),
                        _dt(s.est_arr),
                        s.alert_hash,
                        s.mode.value,
                        _dt(s.last_notified_at),
                        json.dumps(s.extra, ensure_ascii=False),
                        now.isoformat(),
                    )
                    for s in states
                ],
            )

    # --- dédup ------------------------------------------------------------
    def alert_seen(self, key: str) -> bool:
        return self.conn.execute("SELECT 1 FROM seen_alerts WHERE alert_key=?", (key,)).fetchone() is not None

    def mark_alerts_seen(self, keys: Iterable[str], now: datetime) -> None:
        self.conn.executemany(
            "INSERT OR IGNORE INTO seen_alerts VALUES(?, ?)", [(k, now.isoformat()) for k in keys]
        )

    def was_sent(self, key: str) -> bool:
        return self.conn.execute("SELECT 1 FROM sent_keys WHERE key=?", (key,)).fetchone() is not None

    def mark_sent(self, key: str, now: datetime) -> None:
        self.conn.execute("INSERT OR IGNORE INTO sent_keys VALUES(?, ?)", (key, now.isoformat()))

    def log_notification(
        self, now: datetime, window: str | None, title: str, message: str, priority: int, tags: list[str]
    ) -> None:
        self.conn.execute(
            "INSERT INTO notification_log(sent_at, window, title, message, priority, tags)"
            " VALUES(?,?,?,?,?,?)",
            (now.isoformat(), window, title, message, priority, ",".join(tags)),
        )

    def purge(self, today: date) -> None:
        with self.tx() as c:
            c.execute(
                "DELETE FROM train_state WHERE service_date < ?", ((today - timedelta(days=14)).isoformat(),)
            )
            c.execute(
                "DELETE FROM plan_snapshots WHERE service_date < ?",
                ((today - timedelta(days=21)).isoformat(),),
            )
            c.execute(
                "DELETE FROM seen_alerts WHERE first_seen < ?", ((today - timedelta(days=60)).isoformat(),)
            )
            c.execute("DELETE FROM sent_keys WHERE sent_at < ?", ((today - timedelta(days=60)).isoformat(),))
            c.execute(
                "DELETE FROM notification_log WHERE sent_at < ?", ((today - timedelta(days=60)).isoformat(),)
            )

    def stats(self) -> dict[str, Any]:
        return {
            t: self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]  # noqa: S608 (tables constantes)
            for t in ("gtfs_trips", "gtfs_stop_times", "train_state", "plan_snapshots")
        }


def _split(script: str) -> list[str]:
    return [s.strip() for s in script.split(";") if s.strip()]
