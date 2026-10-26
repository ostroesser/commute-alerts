"""Normalisation des identifiants SNCF (trip_id, stop_id), communs au GTFS et au GTFS-RT.

Formes observées (octobre 2026) :
  trip_id : OCESN88716F1187_F:TER:FR:Line::…:20261211, OCESN88715F (court, alertes),
            OCESA…, OCEEA…, OCELO… (régions), suffixe R = trajet routier (car).
  stop_id : StopArea:OCE87191007, StopPoint:OCETrain TER-87191007, StopPoint:OCECar TER-…,
            StopPoint:OCETGV INOUI-…
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

_TRIP_RE = re.compile(r"^OCE[A-Z]{2}(\d+)([FR])")
_STOP_RE = re.compile(r"^(?:StopArea:OCE|StopPoint:OCE(?P<mode>.+?)-)(?P<code>\d{8})$")


class Mode(StrEnum):
    TRAIN = "train"
    HIGH_SPEED = "tgv"
    BUS = "car"
    UNKNOWN = "?"


_HIGH_SPEED = ("TGV", "OUIGO", "ICE", "LYRIA", "EUROSTAR", "THALYS")


def mode_from_label(label: str | None) -> Mode:
    """Mode déduit du libellé de StopPoint (« Train TER », « Car TER », « TGV INOUI »…)."""
    if not label:
        return Mode.UNKNOWN
    up = label.upper()
    if up.startswith("CAR") or "AUTOCAR" in up or up.startswith("BUS"):
        return Mode.BUS
    if any(k in up for k in _HIGH_SPEED):
        return Mode.HIGH_SPEED
    return Mode.TRAIN


@dataclass(frozen=True, slots=True)
class StopRef:
    code: str  # code gare à 8 chiffres
    mode: Mode


def parse_stop_id(stop_id: str) -> StopRef | None:
    m = _STOP_RE.match(stop_id)
    if not m:
        return None
    return StopRef(code=m.group("code"), mode=mode_from_label(m.group("mode")))


def train_number(trip_id: str) -> str | None:
    """Numéro commercial (clé de jointure entre flux)."""
    m = _TRIP_RE.match(trip_id)
    return m.group(1) if m else None


def is_road_trip(trip_id: str) -> bool:
    """Suffixe R après le numéro : trajet routier (car), cf. `_R:CTE`."""
    m = _TRIP_RE.match(trip_id)
    return bool(m and m.group(2) == "R")
