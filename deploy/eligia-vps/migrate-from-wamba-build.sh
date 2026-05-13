#!/usr/bin/env bash
# migrate-from-wamba-build.sh — one-shot VPS migration from the legacy
# /opt/hermes/wamba_build/ snapshot to a git checkout of the fork.
#
# Idempotent: safe to re-run; checks each step before applying.
#
# After this script runs, the live deployment is sourced from
# /opt/hermes/source/ (a checkout of Wizarck/hermes-agent main). Builds
# pull the latest by running `git -C /opt/hermes/source pull` + a docker
# build inside the script in the runbook.
#
# Run on the VPS as root:
#   bash deploy/eligia-vps/migrate-from-wamba-build.sh

set -euo pipefail

REPO_URL="https://github.com/Wizarck/hermes-agent.git"
SOURCE_DIR="/opt/hermes/source"
COMPOSE_LIVE="/opt/hermes/docker-compose.yml"
CONFIG_LIVE="/opt/hermes/data/config.yaml"
LEGACY_SNAPSHOT="/opt/hermes/wamba_build"
IMAGE_TAG="eligia/hermes-agent:wamba"
DOCKERFILE_REL="deploy/eligia-vps/Dockerfile.eligia-overlay"

echo "=== Step 1/6: clone fork into ${SOURCE_DIR} (if not already present) ==="
if [[ -d "${SOURCE_DIR}/.git" ]]; then
  echo "  ✓ ${SOURCE_DIR} is already a git checkout — pulling latest."
  git -C "${SOURCE_DIR}" fetch origin main
  git -C "${SOURCE_DIR}" reset --hard origin/main
else
  echo "  ➤ Cloning ${REPO_URL} → ${SOURCE_DIR}"
  git clone --depth 1 "${REPO_URL}" "${SOURCE_DIR}"
fi
git -C "${SOURCE_DIR}" rev-parse --short HEAD

echo
echo "=== Step 2/6: replace live compose.yaml + config.yaml with repo versions ==="
TS=$(date +%Y%m%d-%H%M%S)
if [[ -f "${COMPOSE_LIVE}" && ! -L "${COMPOSE_LIVE}" ]]; then
  echo "  ➤ Backing up ${COMPOSE_LIVE} → ${COMPOSE_LIVE}.bak-${TS}"
  cp "${COMPOSE_LIVE}" "${COMPOSE_LIVE}.bak-${TS}"
fi
echo "  ➤ Symlinking ${COMPOSE_LIVE} → ${SOURCE_DIR}/deploy/eligia-vps/docker-compose.yml"
ln -sf "${SOURCE_DIR}/deploy/eligia-vps/docker-compose.yml" "${COMPOSE_LIVE}"

if [[ -f "${CONFIG_LIVE}" && ! -L "${CONFIG_LIVE}" ]]; then
  echo "  ➤ Backing up ${CONFIG_LIVE} → ${CONFIG_LIVE}.bak-${TS}"
  cp "${CONFIG_LIVE}" "${CONFIG_LIVE}.bak-${TS}"
fi
echo "  ➤ Symlinking ${CONFIG_LIVE} → ${SOURCE_DIR}/deploy/eligia-vps/config.yaml"
ln -sf "${SOURCE_DIR}/deploy/eligia-vps/config.yaml" "${CONFIG_LIVE}"

echo
echo "=== Step 3/6: install systemd unit from repo (drop-in replacement) ==="
if ! cmp -s "${SOURCE_DIR}/deploy/eligia-vps/hermes.service" /etc/systemd/system/hermes.service; then
  cp /etc/systemd/system/hermes.service "/etc/systemd/system/hermes.service.bak-${TS}"
  cp "${SOURCE_DIR}/deploy/eligia-vps/hermes.service" /etc/systemd/system/hermes.service
  systemctl daemon-reload
  echo "  ✓ hermes.service updated + daemon-reloaded"
else
  echo "  ✓ hermes.service already matches repo"
fi

echo
echo "=== Step 4/6: rebuild image from fork checkout ==="
cd "${SOURCE_DIR}"
docker build -f "${DOCKERFILE_REL}" -t "${IMAGE_TAG}" .

echo
echo "=== Step 5/6: restart hermes ==="
systemctl restart hermes
echo "  ➤ Waiting for healthcheck to settle..."
for i in $(seq 1 12); do
  status=$(docker inspect hermes --format '{{.State.Health.Status}}' 2>/dev/null || echo "unknown")
  echo "    attempt ${i}/12 → ${status}"
  if [[ "${status}" == "healthy" ]]; then
    break
  fi
  sleep 10
done

echo
echo "=== Step 6/6: cleanup legacy snapshot (optional, asks first) ==="
if [[ -d "${LEGACY_SNAPSHOT}" ]]; then
  echo "  ⚠ ${LEGACY_SNAPSHOT} still exists (legacy build context, no longer used)."
  echo "    To remove: rm -rf ${LEGACY_SNAPSHOT}"
  echo "    (This script does not auto-delete it — verify the new flow works first.)"
else
  echo "  ✓ legacy snapshot already removed"
fi

echo
echo "=== Migration complete ==="
echo "Live deployment is now sourced from ${SOURCE_DIR}."
echo "To pull future updates:"
echo "  git -C ${SOURCE_DIR} pull && docker build -f ${SOURCE_DIR}/${DOCKERFILE_REL} -t ${IMAGE_TAG} ${SOURCE_DIR}"
echo "  systemctl restart hermes"
