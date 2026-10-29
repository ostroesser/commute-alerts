#!/bin/sh
# Mise à jour automatique sur l'hôte (lancé par un timer systemd) : avance sur origin/main,
# reconstruit l'image, redéploie, et revient à l'image précédente si le healthcheck échoue.
#   tools/auto_update.sh [--force]   # --force : reconstruit même sans nouveau commit
# ALERT_TOPIC_FILE (optionnel) : fichier local contenant un topic ntfy.sh pour signaler les échecs.
set -eu
cd "$(dirname "$0")/.."
IMAGE=commute-alerts:local
CONTAINER=commute-alerts

notify() {
    [ -n "${ALERT_TOPIC_FILE:-}" ] && [ -r "$ALERT_TOPIC_FILE" ] || return 0
    curl -s -o /dev/null -H "X-Title: commute-alerts — mise à jour" -H "X-Tags: warning" \
        --data-binary "$1" "https://ntfy.sh/$(cat "$ALERT_TOPIC_FILE")" || true
}
fail() {
    echo "$1" >&2
    notify "$1"
    exit 1
}

git fetch -q origin main || fail "git fetch a échoué"
before=$(git rev-parse HEAD)
git merge -q --ff-only origin/main || fail "fast-forward impossible (commits ou modifs locales ?)"
after=$(git rev-parse HEAD)
if [ "$before" = "$after" ] && [ "${1:-}" != "--force" ]; then
    echo "déjà à jour ($after)"
    exit 0
fi
echo "mise à jour $before → $after"

docker image tag "$IMAGE" commute-alerts:previous 2>/dev/null || true
# --network host : le DNS des conteneurs échoue sous la concurrence de uv sur cet hôte.
docker build -q --network host -t "$IMAGE" . || fail "build échoué sur $after"
docker compose up -d --no-build || fail "docker compose up a échoué sur $after"

# Healthcheck : start_period 180 s, intervalle 60 s.
i=0
while [ $i -lt 30 ]; do
    status=$(docker inspect -f '{{.State.Health.Status}}' "$CONTAINER" 2>/dev/null || echo absent)
    [ "$status" = healthy ] && { echo "déployé ($after)"; exit 0; }
    [ "$status" = unhealthy ] && break
    sleep 10
    i=$((i + 1))
done

docker image tag commute-alerts:previous "$IMAGE" && docker compose up -d --no-build
fail "déploiement de $after non sain ($status) : image précédente restaurée"
