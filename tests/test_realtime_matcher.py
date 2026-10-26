from __future__ import annotations

from conftest import BET, FIXTURES, HET, LUX, MONDAY, THI, TZ, S, T, at, sa_feed, tu_feed

from commute_alerts.config import Config
from commute_alerts.ids import Mode
from commute_alerts.matcher import find_added, match_window
from commute_alerts.models import Status
from commute_alerts.realtime import clean_text, decode_alerts, decode_trip_updates
from commute_alerts.static_gtfs import plan_for
from commute_alerts.store import Store

REAL = FIXTURES / "rt_20261026_morning"


def test_clean_text() -> None:
    raw = (
        "<p>Le train Fluo\xa088715\xa0origine Luxembourg</p><p>Merci d'emprunter le train"
        " Fluo\xa088719\xa0départ à 08h15.\xa0 \xa0</p>"
    )
    assert clean_text(raw) == (
        "Le train Fluo 88715 origine Luxembourg Merci d'emprunter le train Fluo 88719 départ à 08h15."
    )
    assert clean_text("&lt;b&gt; a &amp; b") == "<b> a & b"


def test_decode_real_fixture() -> None:
    feed = decode_trip_updates((REAL / "tu_20261026T082952.pb").read_bytes(), TZ)
    assert feed.feed_ts == at("08:29").replace(second=34)
    t = feed.by_key[("88716", MONDAY)]
    s = t.stop(THI)
    assert s is not None and s.dep == at("08:28") and s.dep_delay_s == 600
    assert feed.by_key[("88721", MONDAY)].canceled
    road = feed.by_key[("434341", MONDAY)]
    assert road.road and road.stops[0].mode is Mode.BUS
    skipped = feed.by_key[("88715", MONDAY)]
    origin = skipped.stop(LUX)
    assert origin is not None and origin.skipped


def test_decode_real_alerts() -> None:
    alerts = decode_alerts((REAL / "sa_20261026T082952.pb").read_bytes(), TZ, train_nos={"88715", "88721"})
    texts = {a.summary for a in alerts.alerts}
    assert any("supprimé de Luxembourg à Thionville" in t for t in texts)
    assert all("<p>" not in t for t in texts)
    a = alerts.for_train("88721", at("08:00"), at("09:10"))
    assert len(a) == 2


def test_match_real_fixture_by_number(cfg: Config, store: Store) -> None:
    """Les trip_id du mini-GTFS diffèrent du flux réel : appariement par (n°, date)."""
    feed = decode_trip_updates((REAL / "tu_20261026T082952.pb").read_bytes(), TZ)
    alerts = decode_alerts((REAL / "sa_20261026T082952.pb").read_bytes(), TZ)
    plan = plan_for(store, cfg, cfg.windows[1], MONDAY)  # Retour LUX→THI 07:30-08:45
    obs = match_window(plan, feed, alerts, LUX, THI, cfg.thresholds_min)
    assert obs["88715"].status is Status.SKIPPED_ORIGIN
    assert obs["88721"].status is Status.CANCELED
    assert obs["88719"].status is Status.SCHEDULED and obs["88719"].in_feed
    assert obs["88713"].status is Status.SCHEDULED
    assert obs["88721"].alerts
    morning = match_window(plan_for(store, cfg, cfg.windows[0], MONDAY), feed, alerts, THI, LUX, [5, 10])
    assert morning["88716"].status is Status.DELAYED and morning["88716"].delay_min == 10
    assert morning["88716"].est_arr == at("08:55")
    assert not morning["88501"].in_feed  # déjà passé : absent du flux, statut inconnu


def test_observe_statuses(cfg: Config, store: Store) -> None:
    plan = plan_for(store, cfg, cfg.windows[0], MONDAY)
    ts = at("07:00")
    feed = decode_trip_updates(
        tu_feed(
            ts,
            [
                T("88712", [S(THI, "07:18"), S(HET, "07:25", skipped=True), S(LUX, "07:48", skipped=True)]),
                T("88501", [S(THI, "07:28", label="Car TER"), S(LUX, "08:00", 6, label="Car TER")]),
                T("88804", [S(BET, "07:58", 5)]),  # origine absente : retard propagé ? non (après)
                T("88714", [S(THI, "07:51", 3), S(LUX, "08:22", 7)]),
            ],
        ),
        TZ,
    )
    obs = match_window(plan, feed, None, THI, LUX, [5, 10, 20, 30])
    assert obs["88712"].status is Status.SKIPPED_DEST
    assert obs["88501"].status is Status.BUS_REPLACEMENT and obs["88501"].mode is Mode.BUS
    assert obs["88714"].status is Status.DELAYED and obs["88714"].delay_min == 7  # max(dép, arr)
    assert obs["88804"].in_feed and obs["88804"].est_dep is None  # pas de propagation vers l'amont
    assert not obs["88708"].in_feed


def test_delay_propagation(cfg: Config, store: Store) -> None:
    plan = plan_for(store, cfg, cfg.windows[0], MONDAY)
    # seul un arrêt amont (Metz, hors périmètre) est publié : le retard se propage à Thionville
    feed = decode_trip_updates(tu_feed(at("06:00"), [T("88708", [S("87192039", "06:27", 12)])]), TZ)
    o = match_window(plan, feed, None, THI, LUX, [5, 10])["88708"]
    assert o.delay_min == 12 and o.est_dep == at("06:48") and o.est_arr == at("07:15")


def test_find_added(cfg: Config, store: Store) -> None:
    plan = plan_for(store, cfg, cfg.windows[0], MONDAY)
    feed = decode_trip_updates(
        tu_feed(
            at("07:00"),
            [
                T("99001", [S(THI, "07:40"), S(LUX, "08:05")], added=True),
                T("99002", [S(LUX, "07:40"), S(THI, "08:05")], added=True),  # mauvais sens
                T("99003", [S(THI, "07:45", label="Car TER"), S(LUX, "08:30", label="Car TER")], road=True),
                T("99004", [S(THI, "07:50", label="TGV INOUI"), S(LUX, "08:15", label="TGV INOUI")]),
                T("99005", [S(THI, "09:40"), S(LUX, "10:05")], added=True),  # hors fenêtre
            ],
        ),
        TZ,
    )
    added = find_added(
        feed,
        {p.train_no for p in plan},
        MONDAY,
        THI,
        LUX,
        at("06:30"),
        at("08:30"),
        include_tgv=False,
        include_bus=True,
    )
    assert [(a.train_no, a.mode) for a in added] == [("99001", Mode.TRAIN), ("99003", Mode.BUS)]


def test_alert_periods() -> None:
    raw = sa_feed(at("07:00"), [("a1", ["88501"], "Panne de signalisation", at("06:00"), at("09:00"))])
    snap = decode_alerts(raw, TZ)
    assert snap.for_train("88501", at("07:00"), at("08:00"))
    assert not snap.for_train("88501", at("10:00"), at("11:00"))
    assert snap.alerts[0].summary == "Panne de signalisation"
