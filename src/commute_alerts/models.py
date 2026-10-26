"""Types partagés entre plan théorique, temps réel et moteur."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum

from .ids import Mode


class Status(StrEnum):
    SCHEDULED = "SCHEDULED"
    DELAYED = "DELAYED"
    CANCELED = "CANCELED"
    SKIPPED_ORIGIN = "SKIPPED_ORIGIN"
    SKIPPED_DEST = "SKIPPED_DEST"
    REMOVED_FROM_PLAN = "REMOVED_FROM_PLAN"
    ADDED = "ADDED"
    BUS_REPLACEMENT = "BUS_REPLACEMENT"

    @property
    def is_cancellation(self) -> bool:
        return self in (Status.CANCELED, Status.SKIPPED_ORIGIN, Status.SKIPPED_DEST, Status.REMOVED_FROM_PLAN)


@dataclass(frozen=True, slots=True)
class PlannedTrain:
    """Un train du plan de transport desservant origine puis destination."""

    service_date: date
    train_no: str
    trip_id: str
    dep: datetime  # départ théorique à l'origine (aware)
    arr: datetime  # arrivée théorique à destination (aware)
    mode: Mode
    route_name: str = ""


@dataclass(slots=True)
class TrainState:
    """État persistant par (date, fenêtre, n° de train)."""

    service_date: date
    window: str
    train_no: str
    planned_dep: datetime
    planned_arr: datetime
    status: Status = Status.SCHEDULED
    delay_min: int = 0
    delay_bucket: int = 0  # dernier palier notifié (0 = à l'heure)
    est_dep: datetime | None = None
    est_arr: datetime | None = None
    alert_hash: str = ""
    mode: Mode = Mode.TRAIN
    last_notified_at: datetime | None = None
    extra: dict[str, str] = field(default_factory=dict)
