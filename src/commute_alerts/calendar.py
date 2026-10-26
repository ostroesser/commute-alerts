"""Calendrier : jours de surveillance (jours de la fenêtre, fériés FR/LU, pause congés)."""

from __future__ import annotations

from datetime import date
from functools import lru_cache

import holidays

from .config import WEEKDAYS, Config, Window


@lru_cache(maxsize=32)
def _holidays(spec: str, year: int) -> holidays.HolidayBase:
    """`spec` = code pays ISO (« FR », « LU ») ou pays-subdivision (« FR-57 » pour la Moselle)."""
    country, _, subdiv = spec.partition("-")
    return holidays.country_holidays(country, subdiv=subdiv or None, years=year)


def holiday_name(cfg: Config, day: date) -> str | None:
    for spec in cfg.skip_holidays:
        name = _holidays(spec, day.year).get(day)
        if name:
            return f"{name} ({spec})"
    return None


def is_paused(cfg: Config, day: date) -> bool:
    return cfg.pause_until is not None and day <= cfg.pause_until


def is_active_day(cfg: Config, window: Window, day: date) -> bool:
    if WEEKDAYS[day.weekday()] not in window.days:
        return False
    if is_paused(cfg, day):
        return False
    return holiday_name(cfg, day) is None
