"""Machine à états par (date, fenêtre, train), règles de notification, alternatives, messages."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from enum import StrEnum

from .config import Config, Window
from .ids import Mode
from .matcher import Observation, observe, train_alerts
from .models import PlannedTrain, Status, TrainState
from .notifier import Notification
from .realtime import AlertSnapshot, FeedSnapshot, RtAlert, alerts_hash

DEPARTED_GRACE = timedelta(minutes=1)
CAUSE_MAX = 140
LINE_THRESHOLD = 3


class EventKind(StrEnum):
    CANCELED = "canceled"
    SKIPPED_ORIGIN = "skipped_origin"
    SKIPPED_DEST = "skipped_dest"
    REMOVED = "removed"
    BUS = "bus"
    DELAY = "delay"
    RECOVERED = "recovered"
    ADDED = "added"
    ALERT = "alert"


_STATUS_EVENT = {
    Status.CANCELED: EventKind.CANCELED,
    Status.SKIPPED_ORIGIN: EventKind.SKIPPED_ORIGIN,
    Status.SKIPPED_DEST: EventKind.SKIPPED_DEST,
    Status.REMOVED_FROM_PLAN: EventKind.REMOVED,
    Status.BUS_REPLACEMENT: EventKind.BUS,
    Status.ADDED: EventKind.ADDED,
}

IMPACT = {
    EventKind.CANCELED,
    EventKind.SKIPPED_ORIGIN,
    EventKind.SKIPPED_DEST,
    EventKind.REMOVED,
    EventKind.BUS,
    EventKind.DELAY,
}


@dataclass(slots=True)
class Event:
    kind: EventKind
    state: TrainState
    obs: Observation | None
    alerts: list[RtAlert] = field(default_factory=list)
    detail: str = ""  # ex. « annulation levée »


@dataclass(slots=True)
class Candidate:
    """Train envisageable comme alternative (plan étendu + ajouts) avec son état courant."""

    planned: PlannedTrain
    obs: Observation | None
    status: Status

    @property
    def est_dep(self) -> datetime:
        return (self.obs.est_dep if self.obs and self.obs.est_dep else None) or self.planned.dep

    @property
    def est_arr(self) -> datetime:
        return (self.obs.est_arr if self.obs and self.obs.est_arr else None) or self.planned.arr

    @property
    def realtime(self) -> bool:
        return bool(self.obs and self.obs.in_feed)


@dataclass(slots=True)
class WindowResult:
    states: list[TrainState]
    events: list[Event]
    notification: Notification | None
    seen_alert_keys: list[str]
    tracked: int


def bucket(delay_min: int, thresholds: list[int]) -> int:
    return sum(1 for t in thresholds if delay_min >= t)


def hhmm(dt: datetime) -> str:
    return dt.strftime("%H:%M")


def _initial_state(p: PlannedTrain, window: str) -> TrainState:
    return TrainState(
        service_date=p.service_date,
        window=window,
        train_no=p.train_no,
        planned_dep=p.dep,
        planned_arr=p.arr,
        mode=p.mode,
    )


# --- transitions --------------------------------------------------------------
def transition(
    prev: TrainState, obs: Observation, cfg: Config, *, removed_from_plan: bool, added: bool
) -> tuple[TrainState, list[Event]]:
    """Calcule le nouvel état et les événements (transitions uniquement)."""
    th = cfg.thresholds_min
    new = replace(prev, extra=dict(prev.extra))
    events: list[Event] = []

    if not obs.in_feed:
        if removed_from_plan:
            new.status = Status.REMOVED_FROM_PLAN
        elif added:
            new.status = Status.ADDED
        else:
            return new, events  # absence ≠ suppression : on ne touche à rien
    else:
        new.status = Status.ADDED if added and not obs.status.is_cancellation else obs.status
        new.delay_min = obs.delay_min
        new.est_dep, new.est_arr = obs.est_dep, obs.est_arr
        new.mode = obs.mode

    was_disrupted = prev.status.is_cancellation or prev.status is Status.BUS_REPLACEMENT
    if new.status != prev.status and new.status in _STATUS_EVENT:
        events.append(Event(_STATUS_EVENT[new.status], new, obs))
        if new.status.is_cancellation:
            new.delay_bucket = 0
    elif was_disrupted and new.status in (Status.SCHEDULED, Status.DELAYED):
        detail = (
            "finalement maintenu"
            if prev.status is Status.REMOVED_FROM_PLAN
            else (
                "circule à nouveau en train" if prev.status is Status.BUS_REPLACEMENT else "annulation levée"
            )
        )
        events.append(Event(EventKind.RECOVERED, new, obs, detail=detail))
        new.delay_bucket = 0

    if obs.in_feed and not new.status.is_cancellation:
        b = bucket(new.delay_min, th)
        if b > new.delay_bucket:
            new.delay_bucket = b
            events.append(Event(EventKind.DELAY, new, obs))
        elif new.delay_bucket > 0 and new.delay_min <= th[0] - cfg.recovery_margin_min:
            new.delay_bucket = 0
            events.append(Event(EventKind.RECOVERED, new, obs, detail="retour à l'heure"))
    return new, events


# --- alternatives -----------------------------------------------------------------
def best_alternative(impacted: TrainState, candidates: list[Candidate], now: datetime) -> Candidate | None:
    """Meilleur train non supprimé partant après le train impacté : arrivée estimée la plus tôt."""
    target_arr = impacted.est_arr if not impacted.status.is_cancellation and impacted.est_arr else None
    best: Candidate | None = None
    for c in candidates:
        if c.planned.train_no == impacted.train_no:
            continue
        if c.status.is_cancellation:
            continue
        if c.planned.dep <= impacted.planned_dep or c.est_dep < now:
            continue
        if target_arr is not None and c.est_arr >= target_arr:
            continue  # pour un retard, l'alternative doit arriver avant le train retardé
        if best is None or (c.est_arr, c.est_dep) < (best.est_arr, best.est_dep):
            best = c
    return best


def describe_candidate(c: Candidate) -> str:
    label = f"{hhmm(c.planned.dep)} ({c.planned.train_no}{', car' if c.planned.mode is Mode.BUS else ''})"
    if c.realtime and c.obs:
        state = f"annoncé +{c.obs.delay_min}" if c.obs.delay_min else "à l'heure"
        return f"{label} {state} → arr. ~{hhmm(c.est_arr)}"
    return f"{label} → arr. {hhmm(c.est_arr)} (théorique)"


# --- causes ---------------------------------------------------------------------
_RESTATEMENT = re.compile(r"\best (partiellement )?supprimé|^Le train .* est supprimé", re.I)
_OFFICIAL_ALT = re.compile(
    r"emprunter le (?:train|car)[^0-9]*(\d{4,6})[^0-9]*?(?:départ à )?(\d{1,2})h(\d{2})", re.I
)


def pick_cause(alerts: list[RtAlert]) -> str:
    texts = [a.summary for a in alerts if a.summary]
    if not texts:
        return next((a.cause for a in alerts if a.cause), "")
    explanatory = [t for t in texts if not _RESTATEMENT.search(t)]
    text = (explanatory or texts)[0]
    return text if len(text) <= CAUSE_MAX else text[: CAUSE_MAX - 1].rstrip() + "…"


def official_alternative(alerts: list[RtAlert]) -> str | None:
    for a in alerts:
        if m := _OFFICIAL_ALT.search(a.text):
            return f"{int(m.group(2)):02d}:{m.group(3)} ({m.group(1)})"
    return None


# --- formatage --------------------------------------------------------------------
_PRIORITY = {
    EventKind.CANCELED: 4,
    EventKind.SKIPPED_ORIGIN: 4,
    EventKind.SKIPPED_DEST: 4,
    EventKind.REMOVED: 4,
    EventKind.BUS: 4,
    EventKind.RECOVERED: 2,
    EventKind.ADDED: 2,
    EventKind.ALERT: 3,
}
_TAGS = {
    EventKind.CANCELED: "x",
    EventKind.SKIPPED_ORIGIN: "x",
    EventKind.SKIPPED_DEST: "x",
    EventKind.REMOVED: "x",
    EventKind.BUS: "bus",
    EventKind.DELAY: "hourglass",
    EventKind.RECOVERED: "white_check_mark",
    EventKind.ADDED: "heavy_plus_sign",
    EventKind.ALERT: "warning",
}


def priority(ev: Event) -> int:
    if ev.kind is EventKind.DELAY:
        return 4 if ev.state.delay_min >= 20 else 3
    return _PRIORITY[ev.kind]


def train_label(s: TrainState) -> str:
    kind = "car" if s.mode is Mode.BUS and s.status is not Status.BUS_REPLACEMENT else "TER"
    return f"{hhmm(s.planned_dep)} ({kind} {s.train_no})"


def _est_suffix(s: TrainState) -> str:
    parts = []
    if s.est_dep:
        parts.append(f"dép. ~{hhmm(s.est_dep)}")
    if s.est_arr:
        parts.append(f"arr. ~{hhmm(s.est_arr)}")
    return f" → {', '.join(parts)}" if parts else ""


def event_text(ev: Event, origin: str, destination: str) -> str:
    s = ev.state
    match ev.kind:
        case EventKind.CANCELED:
            return f"{train_label(s)} SUPPRIMÉ."
        case EventKind.SKIPPED_ORIGIN:
            return f"{train_label(s)} SUPPRIMÉ au départ de {origin}."
        case EventKind.SKIPPED_DEST:
            return f"{train_label(s)} ne dessert pas {destination} (suppression partielle)."
        case EventKind.REMOVED:
            return f"{train_label(s)} absent du plan de transport du jour (habituellement prévu)."
        case EventKind.BUS:
            return f"{train_label(s)} REMPLACÉ PAR UN CAR{_est_suffix(s)}."
        case EventKind.DELAY:
            return f"{train_label(s)} retard +{s.delay_min} min{_est_suffix(s)}."
        case EventKind.RECOVERED:
            extra = f" (+{s.delay_min})" if s.delay_min else ""
            return f"{train_label(s)} rétabli : {ev.detail}{extra}."
        case EventKind.ADDED:
            what = "Car" if s.mode is Mode.BUS else "Train"
            return f"{what} ajouté : {hhmm(s.planned_dep)} ({s.train_no}){_est_suffix(s)}."
        case EventKind.ALERT:
            return f"{train_label(s)} info :"
    return ""


def build_notification(
    window: Window,
    origin_name: str,
    dest_name: str,
    events: list[Event],
    candidates: list[Candidate],
    now: datetime,
    click: str | None,
) -> Notification | None:
    if not events:
        return None
    by_train: dict[str, list[Event]] = {}
    for ev in events:
        by_train.setdefault(ev.state.train_no, []).append(ev)
    impacted = [no for no, evs in by_train.items() if any(e.kind in IMPACT for e in evs)]
    prio = max(priority(e) for e in events)
    tags = ["train", *sorted({_TAGS[e.kind] for e in events})]
    head = f"{window.name} {origin_name}→{dest_name}"

    lines: list[str] = []
    if len(impacted) >= LINE_THRESHOLD:
        title = f"⚠️ {head} : perturbations ({len(impacted)} trains)"
        for evs in sorted(by_train.values(), key=lambda e: e[0].state.planned_dep):
            lines.append("• " + _short(_main_event(evs)))
        alt = best_alternative(
            TrainState(
                service_date=now.date(),
                window=window.name,
                train_no="",
                planned_dep=now,
                planned_arr=now,
                status=Status.CANCELED,
            ),
            candidates,
            now,
        )
        if alt:
            lines.append(f"Meilleure option : {describe_candidate(alt)}.")
        causes = {pick_cause(e.alerts or (e.obs.alerts if e.obs else [])) for e in events} - {""}
        if causes:
            lines.append(f"Cause : {sorted(causes)[0]}")
        if "warning" not in tags:
            tags.append("warning")
    else:
        title = f"🚆 {head}"
        # Une même alerte « info » sur plusieurs trains (travaux, affluence…) : une seule ligne
        info_groups: dict[str, list[TrainState]] = {}
        ordered = sorted(by_train.values(), key=lambda e: e[0].state.planned_dep)
        for evs in ordered:
            main = _main_event(evs)
            if main.kind is EventKind.ALERT:
                info_groups.setdefault(pick_cause(main.alerts), []).append(main.state)
        for evs in ordered:
            main = _main_event(evs)
            group = info_groups.get(pick_cause(main.alerts)) if main.kind is EventKind.ALERT else None
            if group is not None and len(group) > 1:
                if group[0] is main.state:
                    trains = ", ".join(f"{hhmm(st.planned_dep)} ({st.train_no})" for st in group)
                    lines.append(f"Info {trains} : {pick_cause(main.alerts)}")
                continue
            lines.append(_train_paragraph(evs, candidates, now, origin_name, dest_name))
    return Notification(
        title=title, message="\n".join(lines), priority=prio, tags=tags, click=click, window=window.name
    )


_ORDER = [
    EventKind.CANCELED,
    EventKind.SKIPPED_ORIGIN,
    EventKind.SKIPPED_DEST,
    EventKind.REMOVED,
    EventKind.BUS,
    EventKind.DELAY,
    EventKind.ADDED,
    EventKind.RECOVERED,
    EventKind.ALERT,
]


def _main_event(evs: list[Event]) -> Event:
    return min(evs, key=lambda e: _ORDER.index(e.kind))


def _short(ev: Event) -> str:
    s = ev.state
    label = f"{hhmm(s.planned_dep)} {s.train_no}"
    return {
        EventKind.CANCELED: f"{label} supprimé",
        EventKind.SKIPPED_ORIGIN: f"{label} supprimé au départ",
        EventKind.SKIPPED_DEST: f"{label} terminus avancé",
        EventKind.REMOVED: f"{label} absent du plan",
        EventKind.BUS: f"{label} remplacé par car",
        EventKind.DELAY: f"{label} +{s.delay_min} min",
        EventKind.ADDED: f"{label} ajouté",
        EventKind.RECOVERED: f"{label} rétabli",
        EventKind.ALERT: f"{label} info",
    }[ev.kind]


def _train_paragraph(
    evs: list[Event], candidates: list[Candidate], now: datetime, origin: str, destination: str
) -> str:
    main = _main_event(evs)
    alerts = [a for e in evs for a in e.alerts] or (main.obs.alerts if main.obs else [])
    text = event_text(main, origin, destination)
    wants_alt = (main.kind is EventKind.DELAY and main.state.delay_bucket >= 2) or (
        main.kind in IMPACT and main.kind is not EventKind.DELAY
    )
    if wants_alt:
        alt = best_alternative(main.state, candidates, now)
        if alt:
            text += f" Alternative : {describe_candidate(alt)}."
        elif main.kind is not EventKind.DELAY and (off := official_alternative(alerts)):
            text += f" Alternative SNCF : {off}."
    cause = pick_cause(alerts)
    if main.kind is EventKind.ALERT:
        text += f" {cause}" if cause else ""
    elif cause and main.kind is not EventKind.RECOVERED:
        text += f" Cause : {cause}"
    return text


# --- évaluation d'une fenêtre -----------------------------------------------------
def evaluate_window(
    cfg: Config,
    window: Window,
    day: date,
    now: datetime,
    *,
    plan: list[PlannedTrain],
    plan_ext: list[PlannedTrain],
    removed: list[PlannedTrain],
    added: list[PlannedTrain],
    feed: FeedSnapshot | None,
    alerts: AlertSnapshot | None,
    prev_states: dict[str, TrainState],
    alert_seen: Callable[[str], bool],
    names: tuple[str, str],
) -> WindowResult:
    origin = cfg.station_code(window.origin)
    dest = cfg.station_code(window.destination)
    th = cfg.thresholds_min

    def observe_one(p: PlannedTrain) -> Observation:
        trip = None
        if feed is not None:
            trip = feed.find(p.trip_id, p.train_no, day) if p.trip_id else feed.by_key.get((p.train_no, day))
        return observe(p, trip, origin, dest, th, train_alerts(alerts, p))

    tracked: list[tuple[PlannedTrain, bool, bool]] = (
        [(p, False, False) for p in plan]
        + [(p, True, False) for p in removed]
        + [(p, False, True) for p in added]
    )
    states: list[TrainState] = []
    events: list[Event] = []
    obs_by_no: dict[str, Observation] = {}
    seen_keys: list[str] = []
    new_states: dict[str, TrainState] = {}

    for p, is_removed, is_added in tracked:
        obs = observe_one(p)
        obs_by_no[p.train_no] = obs
        prev = prev_states.get(p.train_no) or _initial_state(p, window.name)
        ref_dep = obs.est_dep or prev.est_dep or p.dep
        if ref_dep < now - DEPARTED_GRACE:
            new_states[p.train_no] = prev  # déjà parti : figé, plus aucune notification
            continue
        new, evs = transition(prev, obs, cfg, removed_from_plan=is_removed, added=is_added)

        # Alertes : nouveau texte (hash) contenant au moins une alerte jamais notifiée
        h = alerts_hash(obs.alerts)
        fresh = [a for a in obs.alerts if not alert_seen(a.key)]
        if h != prev.alert_hash:
            new.alert_hash = h
            if fresh:
                if evs:
                    evs[0].alerts = fresh
                else:
                    evs.append(Event(EventKind.ALERT, new, obs, alerts=fresh))
        seen_keys.extend(a.key for a in obs.alerts)
        if evs:
            new.last_notified_at = now
        new_states[p.train_no] = new
        events.extend(evs)
    states = list(new_states.values())

    # Candidats pour les alternatives : plan étendu + ajouts, avec statut à jour
    candidates: list[Candidate] = []
    for p in plan_ext + removed + added:
        st = new_states.get(p.train_no)
        obs = obs_by_no.get(p.train_no) or observe_one(p)
        status = st.status if st else (obs.status if obs.in_feed else Status.SCHEDULED)
        candidates.append(Candidate(p, obs, status))

    notif = build_notification(window, names[0], names[1], events, candidates, now, cfg.ntfy.click_url)
    return WindowResult(states, events, notif, seen_keys, len(tracked))


# --- résumé de la veille ---------------------------------------------------------
def evening_digest(
    day: date,
    sections: list[tuple[Window, tuple[str, str], list[PlannedTrain], list[PlannedTrain], list[RtAlert]]],
    *,
    send_all_clear: bool,
    click: str | None,
) -> Notification | None:
    """`sections` = (fenêtre, noms, plan de demain, trains habituels absents, alertes de demain)."""
    lines: list[str] = []
    for window, (o, d), plan, removed, alerts in sections:
        sub: list[str] = []
        for p in removed:
            sub.append(f"• {hhmm(p.dep)} ({p.train_no}) absent du plan (supprimé à l'avance)")
        by_no = {p.train_no: p for p in plan}
        for a in alerts:
            nos = sorted(n for n in a.train_nos if n in by_no)
            if nos and a.summary:
                trains = ", ".join(f"{hhmm(by_no[n].dep)} ({n})" for n in nos[:4])
                sub.append(f"• {trains} : {a.summary[:CAUSE_MAX]}")
        if sub:
            lines.append(f"{window.name} {o}→{d} ({len(plan)} trains prévus) :")
            lines.extend(sub)
    label = day.strftime("%d/%m")
    if not lines:
        if not send_all_clear or not sections:
            return None
        return Notification(
            title=f"🚆 Demain {label} : rien à signaler",
            message="Plan de transport habituel.",
            priority=2,
            tags=["train", "white_check_mark"],
            click=click,
        )
    return Notification(
        title=f"🚆 Demain {label} : perturbations prévues",
        message="\n".join(lines),
        priority=3,
        tags=["train", "warning"],
        click=click,
    )
