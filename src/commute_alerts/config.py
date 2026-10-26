"""Configuration : YAML (avec substitution ${VAR}) + overrides par variables d'environnement."""

from __future__ import annotations

import os
import re
from datetime import date, time
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
WEEKDAYS: tuple[Weekday, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

_ENV_RE = re.compile(r"\$\{(\w+)\}")

DEFAULT_TRIP_UPDATES = "https://proxy.transport.data.gouv.fr/resource/sncf-gtfs-rt-trip-updates"
DEFAULT_SERVICE_ALERTS = "https://proxy.transport.data.gouv.fr/resource/sncf-gtfs-rt-service-alerts"
DEFAULT_STATIC_GTFS = "https://eu.ftp.opendatasoft.com/sncf/plandata/Export_OpenData_SNCF_GTFS_NewTripId.zip"


def _parse_hhmm(v: Any) -> time:
    if isinstance(v, time):
        return v
    if isinstance(v, int):  # YAML 1.1 lit "06:30" non quoté comme un entier sexagésimal
        return time(v // 60, v % 60)
    h, m = str(v).split(":")
    return time(int(h), int(m))


class Window(BaseModel):
    name: str
    origin: str
    destination: str
    start: time
    end: time
    days: list[Weekday] = Field(default_factory=lambda: list(WEEKDAYS[:5]))

    _parse_times = field_validator("start", "end", mode="before")(_parse_hhmm)


class Filters(BaseModel):
    include_tgv: bool = False
    include_bus_replacement: bool = True


class Polling(BaseModel):
    realtime_interval_s: int = Field(default=90, ge=30)  # politesse : jamais < 30 s par flux
    pre_window_min: int = 75
    post_window_min: int = 30
    stale_after_s: int = 360
    max_backoff_s: int = 300
    error_threshold: int = 3
    http_timeout_s: float = 30.0


class Feeds(BaseModel):
    trip_updates: str = DEFAULT_TRIP_UPDATES
    service_alerts: str = DEFAULT_SERVICE_ALERTS
    static_gtfs: str = DEFAULT_STATIC_GTFS
    static_refresh_times: list[time] = Field(default_factory=lambda: [time(3, 30), time(17, 40)])

    _parse_times = field_validator("static_refresh_times", mode="before")(
        lambda v: [_parse_hhmm(x) for x in v]
    )


class Ntfy(BaseModel):
    url: str | None = None
    topic: str | None = None
    token: str | None = None
    click_url: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self.url and self.topic)


class Mobiliteit(BaseModel):
    enabled: bool = False
    api_key: str | None = None


class Config(BaseModel):
    timezone: str = "Europe/Paris"
    data_dir: Path = Path("/data")
    stations: dict[str, str]
    windows: list[Window]
    filters: Filters = Filters()
    thresholds_min: list[int] = Field(default_factory=lambda: [5, 10, 20, 30])
    recovery_margin_min: int = 2
    skip_holidays: list[str] = Field(default_factory=lambda: ["FR", "LU"])
    pause_until: date | None = None
    polling: Polling = Polling()
    feeds: Feeds = Feeds()
    ntfy: Ntfy = Ntfy()
    evening_digest: time | None = time(18, 0)
    send_all_clear: bool = False
    quiet_hours: bool = True
    alternatives_horizon_min: int = 120
    health_port: int = 8080
    mobiliteit: Mobiliteit = Mobiliteit()

    _parse_digest = field_validator("evening_digest", mode="before")(
        lambda v: None if v in (None, "", False) else _parse_hhmm(v)
    )

    @field_validator("stations")
    @classmethod
    def _codes(cls, v: dict[str, str]) -> dict[str, str]:
        for key, code in v.items():
            if not re.fullmatch(r"\d{8}", str(code)):
                raise ValueError(f"code gare invalide pour {key!r} : {code!r} (8 chiffres attendus)")
        return {k: str(c) for k, c in v.items()}

    @field_validator("thresholds_min")
    @classmethod
    def _sorted(cls, v: list[int]) -> list[int]:
        if not v or sorted(v) != v or v[0] <= 0:
            raise ValueError("thresholds_min doit être une liste croissante d'entiers > 0")
        return v

    @model_validator(mode="after")
    def _windows_ref(self) -> Config:
        for w in self.windows:
            for key in (w.origin, w.destination):
                if key not in self.stations:
                    raise ValueError(f"fenêtre {w.name!r} : gare inconnue {key!r}")
        names = [w.name for w in self.windows]
        if len(set(names)) != len(names):
            raise ValueError("noms de fenêtres en double")
        return self

    def station_code(self, key: str) -> str:
        return self.stations[key]


def _expand_env(node: Any, env: dict[str, str]) -> Any:
    """Remplace ${VAR} par sa valeur ; une chaîne qui devient vide vaut None."""
    if isinstance(node, dict):
        return {k: _expand_env(v, env) for k, v in node.items()}
    if isinstance(node, list):
        return [_expand_env(v, env) for v in node]
    if isinstance(node, str) and _ENV_RE.search(node):
        return _ENV_RE.sub(lambda m: env.get(m.group(1), ""), node) or None
    return node


# Variables d'environnement prioritaires sur le YAML (chemin dans l'arbre de config)
_ENV_OVERRIDES: dict[str, tuple[str, ...]] = {
    "NTFY_URL": ("ntfy", "url"),
    "NTFY_TOPIC": ("ntfy", "topic"),
    "NTFY_TOKEN": ("ntfy", "token"),
    "NTFY_CLICK_URL": ("ntfy", "click_url"),
    "MOBILITEIT_API_KEY": ("mobiliteit", "api_key"),
    "DATA_DIR": ("data_dir",),
    "TZ_COMMUTE": ("timezone",),
    "HEALTH_PORT": ("health_port",),
}


def load_config(path: Path | str, env: dict[str, str] | None = None) -> Config:
    env = dict(os.environ) if env is None else env
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    data: dict[str, Any] = _expand_env(raw, env)
    for var, keys in _ENV_OVERRIDES.items():
        if env.get(var):
            node = data
            for k in keys[:-1]:
                if not isinstance(node.get(k), dict):
                    node[k] = {}
                node = node[k]
            node[keys[-1]] = env[var]
    return Config.model_validate(data)
