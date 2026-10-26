from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from conftest import LUX, MONDAY, THI, TZ, S, T, at, make_config, sa_feed, tu_feed

from commute_alerts.config import Config
from commute_alerts.engine import best_alternative, bucket, pick_cause, transition
from commute_alerts.ids import Mode
from commute_alerts.matcher import Observation
from commute_alerts.models import PlannedTrain, Status, TrainState
from commute_alerts.notifier import DryRunNotifier, Notification
from commute_alerts.realtime import decode_alerts, decode_trip_updates
from commute_alerts.service import Service
from commute_alerts.static_gtfs import plan_for
from commute_alerts.store import Store


class Harness:
    """Fait tourner le service sur des flux synthétiques, notifications capturées."""

    def __init__(self, cfg: Config, store: Store) -> None:
        self.notifier = DryRunNotifier()
        self.svc = Service(cfg, store, self.notifier)

    def cycle(self, now: datetime, trips: list[T] | None, alerts: list | None = None) -> list[Notification]:  # type: ignore[type-arg]
        before = len(self.notifier.sent)
        feed = decode_trip_updates(tu_feed(now, trips), TZ) if trips is not None else None
        sa = decode_alerts(sa_feed(now, alerts), TZ) if alerts is not None else None
        asyncio.run(self.svc.poll_cycle(now, feed_override=(feed, sa)))
        return self.notifier.sent[before:]


@pytest.fixture
def h(cfg: Config, store: Store) -> Harness:
    return Harness(cfg, store)


def morning(no: str, dep: str, arr: str, delay: int = 0, **kw: object) -> T:
    def plus(hhmm: str) -> str:
        return (at(hhmm) + timedelta(minutes=delay)).strftime("%H:%M")

    return T(no, [S(THI, plus(dep), delay), S(LUX, plus(arr), delay)], **kw)  # type: ignore[arg-type]


NORMAL = [
    morning("88712", "07:18", "07:48"),
    morning("88501", "07:28", "07:54"),
    morning("88804", "07:36", "08:06"),
]


def test_no_notification_when_normal(h: Harness) -> None:
    assert h.cycle(at("06:40"), NORMAL) == []
    assert h.cycle(at("06:42"), NORMAL) == []


def test_cancellation_notified_once_with_alternative_and_cause(h: Harness) -> None:
    trips = [
        morning("88712", "07:18", "07:48"),
        morning("88501", "07:28", "07:54", canceled=True),
        morning("88804", "07:36", "08:06", delay=5),
        morning("88714", "07:48", "08:15"),
    ]
    alerts = [
        ("a1", ["88501"], "Panne de signalisation entre Thionville et Bettembourg.", at("06:00"), at("09:00"))
    ]
    sent = h.cycle(at("06:50"), trips, alerts)
    assert len(sent) == 1
    n = sent[0]
    assert n.title == "🚆 Matin Thionville→Luxembourg"
    assert "TER 88501 (départ 07:28) SUPPRIMÉ." in n.message
    assert "Alternative : TER 88804 (départ 07:36) annoncé +5 → arr. ~08:11." in n.message
    assert "Cause : Panne de signalisation" in n.message
    assert n.priority == 4 and "x" in n.tags and "train" in n.tags
    # mêmes données au cycle suivant : rien
    assert h.cycle(at("06:52"), trips, alerts) == []
    # annulation levée
    sent = h.cycle(at("06:54"), [morning("88501", "07:28", "07:54")], alerts)
    assert len(sent) == 1 and "rétabli : annulation levée" in sent[0].message and sent[0].priority == 2


def test_delay_buckets_and_recovery(h: Harness) -> None:
    def run(now: str, delay: int) -> list[Notification]:
        return h.cycle(at(now), [morning("88804", "07:36", "08:06", delay=delay)])

    assert run("06:30", 3) == []
    sent = run("06:32", 6)
    assert len(sent) == 1 and "retard +6 min" in sent[0].message and sent[0].priority == 3
    assert "hourglass" in sent[0].tags
    assert run("06:34", 8) == []  # même palier
    assert run("06:36", 4) == []  # sous le 1er seuil mais dans la marge d'hystérésis
    sent = run("06:38", 12)
    assert len(sent) == 1 and "+12 min" in sent[0].message
    sent = run("06:40", 25)
    assert len(sent) == 1 and sent[0].priority == 4
    assert run("06:42", 15) == []  # baisse entre paliers : silencieux
    sent = run("06:44", 2)
    assert len(sent) == 1 and "retour à l'heure" in sent[0].message and "white_check_mark" in sent[0].tags
    assert run("06:46", 2) == []


def test_absence_is_not_cancellation(h: Harness) -> None:
    h.cycle(at("06:30"), [morning("88804", "07:36", "08:06", delay=7)])
    assert h.cycle(at("06:32"), []) == []  # le train disparaît du flux : aucune conclusion
    assert h.cycle(at("06:34"), None) == []  # flux indisponible : idem
    state = h.svc.store.load_states(MONDAY, "Matin")["88804"]
    assert state.status is Status.DELAYED and state.delay_bucket == 1


def test_skipped_and_bus(h: Harness) -> None:
    trips = [
        T("88712", [S(THI, "07:18", skipped=True), S(LUX, "07:48")]),
        T("88501", [S(THI, "07:28", label="Car TER"), S(LUX, "08:05", label="Car TER")]),
    ]
    sent = h.cycle(at("06:40"), trips)
    assert len(sent) == 1
    msg = sent[0].message
    assert "TER 88712 (départ 07:18) SUPPRIMÉ au départ de Thionville." in msg
    assert "TER 88501 (départ 07:28) REMPLACÉ PAR UN CAR" in msg
    assert {"x", "bus"} <= set(sent[0].tags)


def test_line_summary_when_three_trains(h: Harness) -> None:
    trips = [
        morning("88712", "07:18", "07:48", canceled=True),
        morning("88501", "07:28", "07:54", canceled=True),
        morning("88804", "07:36", "08:06", delay=25),
        morning("88714", "07:48", "08:15", delay=2),
    ]
    sent = h.cycle(at("06:45"), trips)
    assert len(sent) == 1
    n = sent[0]
    assert n.title == "⚠️ Matin Thionville→Luxembourg : perturbations (3 trains)"
    assert n.message.splitlines()[:3] == [
        "• 88712 (07:18) supprimé",
        "• 88501 (07:28) supprimé",
        "• 88804 (07:36) +25 min",
    ]
    assert "Meilleure option : TER 88714 (départ 07:48) annoncé +2 → arr. ~08:17." in n.message
    assert "warning" in n.tags and n.priority == 4


def test_new_alert_text_on_monitored_train(h: Harness) -> None:
    alerts = [("a1", ["88804"], "Forte affluence attendue.", None, None)]
    sent = h.cycle(at("06:40"), NORMAL, alerts)
    assert len(sent) == 1 and sent[0].priority == 2
    assert "TER 88804 (départ 07:36) — info SNCF : Forte affluence attendue." in sent[0].message
    assert h.cycle(at("06:42"), NORMAL, alerts) == []
    alerts2 = [*alerts, ("a2", ["88804"], "Arrêt supplémentaire à Hettange-Grande.", None, None)]
    sent = h.cycle(at("06:44"), NORMAL, alerts2)
    assert len(sent) == 1 and "Arrêt supplémentaire" in sent[0].message
    # alerte retirée puis republiée à l'identique : déjà notifiée
    h.cycle(at("06:46"), NORMAL, [])
    assert h.cycle(at("06:48"), NORMAL, alerts2) == []


def test_added_train(h: Harness) -> None:
    trips = [*NORMAL, T("99001", [S(THI, "07:40"), S(LUX, "08:05")], added=True)]
    sent = h.cycle(at("06:40"), trips)
    assert len(sent) == 1 and "Train ajouté : 99001 (départ 07:40)" in sent[0].message
    assert h.cycle(at("06:42"), trips) == []


def test_departed_trains_are_frozen(h: Harness) -> None:
    # 88712 (07:18, annoncé 07:21) est parti à 07:25 : ses changements ne sont plus notifiés
    assert h.cycle(at("07:25"), [morning("88712", "07:18", "07:48", delay=3)]) == []
    assert h.cycle(at("07:27"), [morning("88712", "07:18", "07:48", delay=3, canceled=True)]) == []
    # mais un train annoncé plus tard que l'heure courante n'est pas parti
    sent = h.cycle(at("07:29"), [morning("88501", "07:28", "07:54", delay=6)])
    assert len(sent) == 1 and "+6 min" in sent[0].message


def test_removed_from_plan_vs_baseline(cfg: Config, store: Store) -> None:
    tue = date(2026, 10, 27)
    # baseline : snapshot du plan du 27 pris le 20 (comprenait 88804), plan actuel sans 88804
    full = plan_for(store, cfg, cfg.windows[0], MONDAY)
    store.save_snapshot(
        tue,
        "Matin",
        date(2026, 10, 20),
        [
            PlannedTrain(tue, p.train_no, "", p.dep + timedelta(days=1), p.arr + timedelta(days=1), p.mode)
            for p in full
        ],
    )
    h = Harness(cfg, store)
    sent = h.cycle(at("05:30", tue), None)  # ouverture de fenêtre, flux encore indisponible
    assert len(sent) == 1
    assert "TER 88804 (départ 07:36) absent du plan de transport du jour" in sent[0].message
    assert "Alternative : TER 88714 (départ 07:48) → arr. 08:15 (théorique)." in sent[0].message
    assert h.cycle(at("05:32", tue), None) == []
    # le train apparaît finalement dans le temps réel
    sent = h.cycle(at("06:30", tue), [T("88804", [S(THI, "07:36"), S(LUX, "08:06")], day=tue)])
    assert len(sent) == 1 and "finalement maintenu" in sent[0].message


def test_baseline_fallback_same_weekday(cfg: Config, store: Store) -> None:
    """Sans snapshot anticipé, on compare au même jour de la semaine précédente (S-1)."""
    tue = date(2026, 10, 27)
    prev = tue - timedelta(days=7)
    plan = plan_for(store, cfg, cfg.windows[0], MONDAY)
    store.save_snapshot(
        prev,
        "Matin",
        prev,
        [
            PlannedTrain(prev, p.train_no, "", p.dep - timedelta(days=6), p.arr - timedelta(days=6), p.mode)
            for p in plan
        ],
    )
    removed = Harness(cfg, store).svc.removed_trains(cfg.windows[0], tue, set())
    assert [(r.train_no, r.dep) for r in removed] == [("88804", at("07:36", tue))]


def test_quiet_outside_windows_and_holidays(h: Harness) -> None:
    assert h.cycle(at("12:00"), [morning("88501", "07:28", "07:54", canceled=True)]) == []
    assert h.svc.active_windows(at("07:00", date(2026, 11, 11))) == []  # férié
    assert h.svc.active_windows(at("05:14")) == []
    assert [w.name for w, _ in h.svc.active_windows(at("05:15"))] == ["Matin"]


def test_all_clear_option(tmp_path: Path, store: Store) -> None:
    h = Harness(make_config(tmp_path, send_all_clear=True), store)
    sent = h.cycle(at("05:20"), NORMAL)
    assert len(sent) == 1 and sent[0].title.startswith("✅ Matin") and sent[0].priority == 2
    assert h.cycle(at("05:22"), NORMAL) == []


# --- unitaires -----------------------------------------------------------------------
def _state(no: str, dep: str, arr: str, status: Status = Status.SCHEDULED) -> TrainState:
    return TrainState(MONDAY, "Matin", no, at(dep), at(arr), status=status)


def test_bucket() -> None:
    assert [bucket(d, [5, 10, 20, 30]) for d in (0, 4, 5, 9, 10, 29, 30, 90)] == [0, 0, 1, 1, 2, 3, 4, 4]


def test_transition_ignores_absent(cfg: Config) -> None:
    prev = _state("1", "07:00", "07:30", Status.CANCELED)
    p = PlannedTrain(MONDAY, "1", "", at("07:00"), at("07:30"), Mode.TRAIN)
    new, evs = transition(
        prev, Observation("1", p, Status.SCHEDULED, in_feed=False), cfg, removed_from_plan=False, added=False
    )
    assert evs == [] and new.status is Status.CANCELED


def test_best_alternative_prefers_earliest_arrival() -> None:
    from commute_alerts.engine import Candidate

    def cand(no: str, dep: str, arr: str, delay: int = 0, status: Status = Status.SCHEDULED) -> Candidate:
        p = PlannedTrain(MONDAY, no, "", at(dep), at(arr), Mode.TRAIN)
        o = Observation(
            no, p, status, True, delay, at(dep) + timedelta(minutes=delay), at(arr) + timedelta(minutes=delay)
        )
        return Candidate(p, o, status)

    impacted = _state("88501", "07:28", "07:54", Status.CANCELED)
    cands = [
        cand("88712", "07:18", "07:48"),  # avant : exclu
        cand("88804", "07:36", "08:06", delay=15),  # arr 08:21
        cand("88714", "07:48", "08:15"),  # arr 08:15 : meilleur
        cand("88503", "07:57", "08:24", status=Status.CANCELED),
    ]
    best = best_alternative(impacted, cands, at("07:00"))
    assert best is not None and best.planned.train_no == "88714"
    # pour un retard, l'alternative doit arriver avant le train retardé
    late = _state("88804", "07:36", "08:06", Status.DELAYED)
    late.est_arr = at("08:10")
    assert best_alternative(late, cands, at("07:00")) is None


def test_pick_cause_prefers_explanation() -> None:
    from commute_alerts.realtime import RtAlert

    def alert(text: str) -> RtAlert:
        return RtAlert("x" + text[:3], frozenset({"1"}), frozenset(), (), "Plus d'information :", text, "")

    alerts = [
        alert("Le train Fluo 88721 origine Luxembourg est supprimé."),
        alert("Une opération de maintenance imprévue."),
    ]
    assert pick_cause(alerts) == "Une opération de maintenance imprévue."
    assert len(pick_cause([alert("x" * 300)])) == 140


def test_same_alert_on_many_trains_grouped(h: Harness) -> None:
    alerts = [
        ("w1", ["88712", "88501", "88804"], "Travaux : temps de parcours allongé de 5 min.", None, None)
    ]
    sent = h.cycle(at("06:40"), NORMAL, alerts)
    assert len(sent) == 1
    assert sent[0].message == (
        "Info SNCF pour 88712 (07:18), 88501 (07:28), 88804 (07:36) : "
        "Travaux : temps de parcours allongé de 5 min."
    )
