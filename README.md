# commute-alerts

Alertes **ntfy** de perturbations TER par **ligne** et **fenêtre horaire**, à partir des données ouvertes SNCF (GTFS + GTFS-RT).

Au lieu de suivre un train précis, le service surveille **tous les trains** qui relient une gare d'origine à une gare de destination dans une fenêtre (par ex. Thionville → Luxembourg, départ entre 06:30 et 08:30). Il n'envoie une notification **que lorsque quelque chose change** : suppression, retard qui franchit un palier, remplacement par car, train absent du plan, retour à la normale… Pour chaque train touché, il propose aussi la meilleure alternative.

```
🚆 Matin Thionville→Luxembourg
07:28 (TER 88501) SUPPRIMÉ. Alternative : 07:36 (88804) annoncé +5 → arr. ~08:11. Cause : Panne de signalisation entre Thionville et Bettembourg.
```

> ⚠️ **Projet personnel non officiel**, sans lien avec SNCF Voyageurs, la CFL ou transport.data.gouv.fr. Les données peuvent être incomplètes ou fausses : en cas de doute, vérifiez auprès du transporteur.

## Fonctionnement

| Étape | Détail |
|---|---|
| Plan théorique | GTFS national SNCF téléchargé chaque jour (03:30 et 17:40, requête conditionnelle ETag/Last-Modified), parsé en streaming et filtré sur les gares configurées (~20 s, ~45 Mo sur un Raspberry Pi 4). |
| Sélection des trains | Trips dont la séquence d'arrêts contient l'origine **puis** la destination. Pas de filtre par `route_id`. Les TGV sont exclus par défaut, les cars inclus et marqués « car ». |
| Temps réel | Trip Updates + Service Alerts GTFS-RT, interrogés toutes les 90 s **uniquement** pendant les fenêtres actives (de début − 75 min à fin + 30 min), en jours ouvrés hors fériés FR/LU. |
| Appariement | Par `trip_id`, sinon par (numéro commercial, date de circulation). |
| Baseline | Le plan des 8 jours suivants est mémorisé à chaque import. Un train « habituel » qui disparaît du GTFS (suppression connue à l'avance : grève, travaux) est signalé la veille à 18:00, puis à l'ouverture de la fenêtre. À défaut, la comparaison se fait avec le même jour de la semaine précédente. |
| État | SQLite (WAL) par (date, fenêtre, train) : statut, palier de retard notifié, empreinte des alertes. Seules les **transitions** sont notifiées. |

### Événements notifiés

| Événement | Priorité ntfy | Tags |
|---|---|---|
| Suppression (trip `CANCELED`, arrêt `SKIPPED` à l'origine ou à la destination) | 4 | `x` |
| Train habituel absent du plan du jour | 4 | `x` |
| Remplacement par car (`StopPoint:OCECar…` ou trip routier `…R`) | 4 | `bus` |
| Retard ≥ 20 min / 5–19 min (paliers `[5, 10, 20, 30]`, notifié à chaque palier supérieur) | 4 / 3 | `hourglass` |
| Nouveau texte d'alerte sur un train surveillé | 3 | `warning` |
| Train ajouté | 2 | `heavy_plus_sign` |
| Retour à la normale (retard < 1er palier − marge, annulation levée, train finalement maintenu) | 2 | `white_check_mark` |
| Résumé de la veille (18:00) | 3 | `warning` |
| Données temps réel indisponibles / rétablies | 2 | `warning` / `white_check_mark` |

- Un seul message par fenêtre et par cycle. À partir de 3 trains touchés, le message devient une synthèse « ligne ».
- Un train déjà parti n'est plus notifié.
- **L'absence d'un train dans le flux temps réel n'est jamais interprétée comme une suppression** : le flux ne couvre que les trains en circulation ou proches du départ.
- Si le flux a plus de 6 min ou échoue 3 fois de suite, le service envoie **une** notification « données indisponibles » puis applique un backoff exponentiel (5 min max). Une notification « rétablies » suit au retour des données.

### Exemples

```
⚠️ Matin Thionville→Luxembourg : perturbations (3 trains)
• 07:18 88712 supprimé
• 07:28 88501 supprimé
• 07:36 88804 +25 min
Meilleure option : 07:48 (88714) annoncé +2 → arr. ~08:17.
```

```
🚆 Retour Luxembourg→Thionville
08:39 (TER 88721) SUPPRIMÉ. Alternative SNCF : 09:39 (88520). Cause : Une opération de maintenance imprévue sur le train empêche sa mise en circulation.
```

```
🚆 Demain 27/10 : perturbations prévues
Matin Thionville→Luxembourg (10 trains prévus) :
• 07:36 (88804) absent du plan (supprimé à l'avance)
```

## Sources de données et licences

| Données | URL | Licence |
|---|---|---|
| GTFS-RT Trip Updates | `https://proxy.transport.data.gouv.fr/resource/sncf-gtfs-rt-trip-updates` | ODbL |
| GTFS-RT Service Alerts | `https://proxy.transport.data.gouv.fr/resource/sncf-gtfs-rt-service-alerts` | ODbL |
| GTFS théorique national | `https://eu.ftp.opendatasoft.com/sncf/plandata/Export_OpenData_SNCF_GTFS_NewTripId.zip` | ODbL |

Données : **SNCF Voyageurs / transport.data.gouv.fr**, sous licence [ODbL](https://opendatacommons.org/licenses/odbl/). Fériés : bibliothèque [`holidays`](https://github.com/vacanza/holidays) (MIT).

Pour ménager le proxy, le service fait au plus une requête par flux toutes les 90 s, et seulement pendant les fenêtres actives. Il fait deux requêtes conditionnelles par jour pour le GTFS.

## Limites connues

- **Horizon du temps réel** : le flux Trip Updates ne contient que les trains en circulation ou qui partent dans l'heure (~80 min observées). Un retard n'est donc connu qu'environ une heure avant le départ.
- **Qualité du flux** : le validateur MobilityData signale des centaines d'erreurs, que le code contourne en partie :
  - `schedule_relationship` est souvent absent ;
  - les horaires ne sont pas toujours triés ;
  - certains `trip_id` sont absents du GTFS ;
  - les alertes ont toujours `effect = UNKNOWN_EFFECT` et leur texte est en HTML.
- **Périmètre non filtré** : le flux couvre tout le réseau, soit environ 1 800 trips et 1,2 Mo par appel. Le filtrage se fait côté client.
- **Trains CFL (885xx, 886xx, 887xx, 888xx)** : ils sont présents dans le GTFS et le GTFS-RT SNCF (vérifié en octobre 2026), mais leur couverture temps réel côté Luxembourg peut être incomplète.
- **Changement de service annuel** (mi-décembre) : rien n'est codé en dur, tout vient du GTFS quotidien. La baseline « S-1 » peut toutefois produire de faux « absents » la première semaine si aucun snapshot anticipé n'existe.
- **Le GTFS n'intègre les adaptations que la veille vers 17 h**, d'où le rafraîchissement de 17:40 avant le résumé de 18:00.
- Les fenêtres qui traversent minuit ne sont pas gérées.

## Installation (Raspberry Pi arm64, Docker Compose)

```sh
git clone <ce dépôt> commute-alerts && cd commute-alerts
cp .env.example .env               # NTFY_URL, NTFY_TOPIC, NTFY_TOKEN (optionnel)
cp config.example.yaml config.yaml # gares, fenêtres, seuils
docker build --network host -t commute-alerts:local .
docker compose up -d
docker compose logs -f
```

- `docker compose build` fonctionne aussi. Le `--network host` sert seulement à contourner un DNS instable dans les conteneurs pendant le build, un cas fréquent sur les hôtes dont le seul résolveur est en IPv6.
- Le conteneur tourne sans privilèges : utilisateur non-root, système de fichiers en lecture seule, `cap_drop: ALL`, `mem_limit: 300m`.
- Les données (SQLite + `gtfs.zip`) sont dans le volume `commute-data` (`/data`).
- Au premier démarrage, le service télécharge et importe le GTFS (environ 30 s).

Commandes utiles :

```sh
# Notification de test
docker compose run --rm commute-alerts --test-notification
# Plan d'une date (après import) : trains par fenêtre, absents vs baseline, jour surveillé ou non
docker compose run --rm commute-alerts --plan 2026-10-27
# Une itération en dry-run (notifications sur stdout)
docker compose run --rm commute-alerts --once --dry-run
# État de santé
docker compose exec commute-alerts python -m commute_alerts.health --check && echo OK
docker compose exec commute-alerts python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8080/healthz').read().decode())"
```

`/healthz` renvoie 200 si la boucle tourne (moins de 12 min) et que le GTFS est importé, 503 sinon. Il expose aussi l'âge du flux, le dernier poll réussi, les fenêtres actives et la dernière erreur. Le port n'est pas publié par défaut.

## Configuration

- `config.yaml` : voir [`config.example.yaml`](config.example.yaml), commenté. Les `${VAR}` sont remplacées par les variables d'environnement.
- `.env` : voir [`.env.example`](.env.example). Contient `NTFY_URL`, `NTFY_TOPIC`, `NTFY_TOKEN` et `NTFY_CLICK_URL`, qui ont priorité sur le YAML.
- Les **codes gare** sont les 8 chiffres des `stop_id` SNCF (`StopArea:OCE87191007`) :

  | Gare | Code |
  |---|---|
  | Thionville | 87191007 |
  | Hettange-Grande | 87191163 |
  | Bettembourg | 82006030 |
  | Howald | 82002501 |
  | Luxembourg (gare centrale) | 82001000 (≠ « Luxembourg » RER Paris 87758615) |

- `skip_holidays: [FR, LU]` : la syntaxe `FR-57` ajoute les fériés d'Alsace-Moselle (Vendredi saint, 26 décembre).
- `pause_until: 2026-12-31` suspend la surveillance pendant les congés.
- `send_all_clear: true` envoie un « ✅ tout est normal » à l'ouverture de chaque fenêtre.
- Publication ntfy : `POST ${NTFY_URL}` en JSON (`{topic, title, message, priority, tags, click}`), avec `Authorization: Bearer ${NTFY_TOKEN}` si défini.

## Développement

```sh
uv sync                       # Python 3.12, dépendances épinglées (uv.lock)
git config core.hooksPath .githooks   # pre-commit : scan des secrets
uv run pytest && uv run ruff check . && uv run mypy
```

Les tests utilisent :
- un mini-GTFS synthétique (`tests/fixtures/gtfs_mini/`) ;
- des flux GTFS-RT générés (`tests/conftest.py`) ;
- une capture réelle filtrée (`tests/fixtures/rt_20261026_morning/`) contenant un retard, une suppression, une suppression partielle, un car et des alertes.

### Enregistrer des fixtures

```sh
# 1 capture filtrée sur les gares de l'axe (quelques Ko)
uv run python tools/record_fixture.py tests/fixtures/rt_YYYYMMDD_soir --filter-stations 87191007,87191163,82006030,82002501,82001000
# une soirée complète, toutes les 90 s
uv run python tools/record_fixture.py /tmp/soir --interval 90 --until 19:45 --filter-stations 87191007,82001000
# rejouer (notifications en dry-run)
uv run python -m commute_alerts --config config.yaml --replay /tmp/soir --gtfs data/gtfs.zip
```

`tools/explore_feeds.py` affiche un diagnostic complet des flux live : fraîcheur, formes d'identifiants, trains sur l'axe, alertes.

### Hygiène du dépôt

`tools/secret_scan.py` (test, hook pre-commit et CI) échoue si l'un de ces éléments apparaît dans un fichier versionné :
- un hôte `*.ts.net` ;
- une IP `100.x.y.z` ou `192.168.x.y` ;
- un jeton `tk_…` ;
- une valeur sensible du `.env` local (topic, token, hôte privé).

### Vulnérabilités : détection et correction automatiques

| Couche | Outil |
|---|---|
| Dépendances Python (`uv.lock`) | Dependabot (alertes + PR de sécurité immédiates, mises à jour hebdomadaires) ; `pip-audit` en CI |
| Images de base (`python:3.12-slim`, `uv`) | épinglées par digest, Dependabot ouvre une PR à chaque nouveau digest ; scan Trivy de l'image en CI |
| GitHub Actions | épinglées par SHA, mises à jour par Dependabot |
| Code | règles bandit de ruff (`S`), CodeQL (configuration par défaut de GitHub) |

La CI tourne aussi chaque lundi, pour détecter les CVE publiées depuis le dernier commit. Les PR Dependabot non majeures sont fusionnées automatiquement quand `test`, `audit` et `docker` passent ; les montées de version majeures attendent une relecture.

Déploiement continu sur l'hôte : `tools/auto_update.sh`, lancé chaque jour par un timer systemd, avance sur `origin/main`, reconstruit l'image et redéploie. Si le conteneur n'est pas `healthy` dans les 5 minutes, il restaure l'image précédente et notifie (topic ntfy lu dans le fichier `ALERT_TOPIC_FILE`).

```ini
# /etc/systemd/system/commute-alerts-update.service
[Service]
Type=oneshot
User=<utilisateur du groupe docker>
Environment=ALERT_TOPIC_FILE=/chemin/vers/topic.txt
ExecStart=/chemin/vers/sncf-notif/tools/auto_update.sh

# /etc/systemd/system/commute-alerts-update.timer
[Timer]
OnCalendar=*-*-* 11:00
RandomizedDelaySec=30m
Persistent=true
[Install]
WantedBy=timers.target
```

## Dépannage

| Symptôme | Piste |
|---|---|
| « Données temps réel indisponibles » | Le proxy renvoie une erreur ou un flux périmé (`header.timestamp` de plus de 6 min). Vérifier `/healthz` (`feed_age_s`, `last_error`) et les logs JSON (`"msg": "poll"`). Le service reprend seul. |
| Aucun train dans `--plan` | GTFS non importé (`--import-gtfs`) ; code gare erroné (log `gares absentes du GTFS`) ; jour férié, week-end ou `pause_until`. |
| Un train attendu manque | TGV exclus (`filters.include_tgv`) ; départ hors fenêtre ; ordre origine → destination inversé. |
| Codes gare | `unzip -p data/gtfs.zip stops.txt \| grep -i thionville`, ou `tools/explore_feeds.py` pour le temps réel. |
| Trop de notifications | Augmenter `thresholds_min` ou `recovery_margin_min`. Les alertes déjà notifiées (même identifiant et même texte) ne sont jamais renvoyées. |

## Licence

[MIT](LICENSE). Les données restent soumises à leurs licences respectives (ODbL).
