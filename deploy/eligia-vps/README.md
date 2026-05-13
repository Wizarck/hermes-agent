# `deploy/eligia-vps/` — ELIGIA stack deployment for Hermes

Source-of-truth for the production Hermes deployment on the eligia VPS
(`178.104.140.21`). Until this directory existed (2026-05-13), all three files
below lived only on the VPS at `/opt/hermes/*` and would have been lost in a
disaster recovery. Now they're versioned in this fork.

## Contents

| File | Lives on VPS at | Purpose |
|---|---|---|
| `Dockerfile.eligia-overlay` | `/opt/hermes/wamba_build/Dockerfile.eligia-overlay` (legacy) | Custom overlay on `nousresearch/hermes-agent` upstream image |
| `docker-compose.yml` | `/opt/hermes/docker-compose.yml` | Compose definition: image + env + volumes + healthcheck |
| `config.yaml` | `/opt/hermes/data/config.yaml` (mounted into container at `/opt/data/config.yaml`) | Hermes-agent runtime config: model defaults, plugins enabled, agent params, personalities, etc. |
| `hermes.service` | `/etc/systemd/system/hermes.service` | Systemd unit. Decrypts SOPS env via `sops exec-env` and runs `docker compose up -d --force-recreate`. |
| `migrate-from-wamba-build.sh` | (run-once on VPS) | One-shot migration script: clone fork → symlink compose/config → install systemd unit → rebuild image → restart. |

## How prod is wired

```
systemd unit: hermes.service
    │
    ▼
sops exec-env /opt/eligia/eligia-core/secrets/secrets.env
    │      ↑ decrypts the SOPS-encrypted secrets file from the
    │        eligia-core repo and injects all vars into the env
    ▼
docker compose up -d --force-recreate
    │      ↑ reads /opt/hermes/docker-compose.yml (sibling of this README)
    │        which references the env vars (`${ANTHROPIC_API_KEY_HERMES}`,
    │        `${LANGFUSE_PUBLIC_KEY}`, ...) injected above
    ▼
Container `hermes` running image `eligia/hermes-agent:wamba`
    │      ↑ built once from this Dockerfile.eligia-overlay; rebuild
    │        whenever this directory changes
    ▼
Hermes loads /opt/data/config.yaml (mounted from /opt/hermes/data/config.yaml)
    └──► plugins.enabled: [observability/langfuse]
         └──► writes traces to Langfuse Cloud with
              metadata.application = "hermes-bot"
              metadata.consumer    = "HERMES"
```

## Cost-by-tag dashboard relationship

The `application` and `consumer` tags on Langfuse traces are read by the
eligia-core dashboard's `TopByApplicationCard` and `TopByConsumerCard`
widgets. See `Wizarck/eligia-core` →
`openspec/changes/add-litellm-enforcement/tasks.md` §T6 for the full Phase 1
closure record. The patched langfuse plugin lives at
`plugins/observability/langfuse/__init__.py` at the fork root (NOT under
`deploy/eligia-vps/`) — the Dockerfile here copies it into the image.

## Upstream sync routine (REMEMBER: bump the image pin)

This fork uses the overlay pattern: `Dockerfile.eligia-overlay` starts
`FROM nousresearch/hermes-agent@sha256:<digest>` and `COPY`s our custom
source files onto that pinned base image. **The pinned digest and the
fork's source tree must advance together** — otherwise the overlay
copies new source files (that may import new modules) on top of an older
base image that lacks them, and the container crashes at startup with
`ModuleNotFoundError: No module named 'agent.<something>'`.

Routine for a fresh upstream sync:

```bash
# 1. Sync the fork source tree.
cd Wizarck/hermes-agent
git fetch upstream
git checkout main
git merge upstream/main        # resolve conflicts in our patched files
                               # (gateway/*, plugins/observability/langfuse/*,
                               # tools/cronjob_tools.py, scripts/release.py, ...)
git push origin main           # OR open a chore PR for review

# 2. Find the upstream Docker image digest matching the new HEAD.
HEAD_SHA=$(git rev-parse --short=40 upstream/main)
TAG="sha-${HEAD_SHA}"  # NousResearch publishes one image per upstream commit
DIGEST=$(curl -s "https://hub.docker.com/v2/repositories/nousresearch/hermes-agent/tags/${TAG}" \
          | jq -r '.digest')
echo "${TAG} → ${DIGEST}"

# 3. Bump deploy/eligia-vps/Dockerfile.eligia-overlay (ARG UPSTREAM=...)
#    to that digest. Commit. PR. Merge.

# 4. Pull on VPS, rebuild, restart.
ssh root@178.104.140.21 \
  "cd /opt/hermes/source && git pull && \
   docker build -f deploy/eligia-vps/Dockerfile.eligia-overlay \
                -t eligia/hermes-agent:wamba . && \
   systemctl restart hermes"
```

Lesson learned the hard way on 2026-05-13 (see PR #6 in this fork).
Captured upstream in `Wizarck/ai-playbook` `specs/upstream-sync.md`
§"Containerised forks — base-image pin discipline".

## Build + deploy

From the fork root on any machine that has docker + ssh access to the VPS:

```bash
# 1. Sync this fork to the VPS (or git pull on the VPS itself).
ssh root@178.104.140.21 \
  'cd /opt/hermes/source && git pull --ff-only origin main'

# 2. Rebuild the image on the VPS.
ssh root@178.104.140.21 \
  'cd /opt/hermes/source && docker build \
     -f deploy/eligia-vps/Dockerfile.eligia-overlay \
     -t eligia/hermes-agent:wamba .'

# 3. Restart the container so the new image is picked up.
ssh root@178.104.140.21 'systemctl restart hermes'

# 4. Verify health.
ssh root@178.104.140.21 \
  'docker inspect hermes --format "{{.State.Health.Status}}"'
# expected: healthy (within ~45s of restart)
```

> **Migration script**: [`migrate-from-wamba-build.sh`](migrate-from-wamba-build.sh) automates the one-shot
> transition from the legacy `/opt/hermes/wamba_build/` snapshot to a git
> checkout at `/opt/hermes/source/`. After running it, the live deployment
> is sourced from this repo directly; future updates are just
> `git pull && docker build && systemctl restart hermes`.
>
> ```bash
> ssh root@178.104.140.21 \
>   'cd /tmp && curl -sL https://raw.githubusercontent.com/Wizarck/hermes-agent/main/deploy/eligia-vps/migrate-from-wamba-build.sh \
>     | bash'
> ```

## Verify Langfuse tracing is active

```bash
ssh root@178.104.140.21 \
  'docker exec hermes /opt/hermes/.venv/bin/python -c \
    "from plugins.observability.langfuse import _get_langfuse, _application_tag, _consumer_tag; \
     c = _get_langfuse(); \
     print(f\"langfuse_client: {type(c).__name__ if c else None}\"); \
     print(f\"application: {_application_tag()}\"); \
     print(f\"consumer: {_consumer_tag()}\")"'
```

Expected:

```
langfuse_client: Langfuse
application: hermes-bot
consumer: HERMES
```

If `langfuse_client: None` → credentials missing (check `LANGFUSE_PUBLIC_KEY`
and `LANGFUSE_SECRET_KEY` are exported from `secrets.env`) OR the SDK isn't
installed (check the build log for `uv pip install langfuse>=3.0`).

## Differences between this repo and the live VPS

The repo cannot — and should not — be byte-identical to the live filesystem.
This list enumerates the expected deltas so future-you can audit:

| Live (`/opt/hermes/...`) | Repo (`deploy/eligia-vps/...`) | Note |
|---|---|---|
| Decrypted env vars from `sops exec-env` | `${VAR}` placeholders | Secrets live in `Wizarck/eligia-core` `secrets/secrets.env`, NOT here. |
| `data/SOUL.md`, `data/memories/`, `data/agents/` | (not in repo) | Mounted volumes — runtime state, not config. |
| `skills/` directory | (not in repo) | User-modified skills tree; preserved as a mount. |
| `data/sessions/` | (not in repo) | Container-managed volume. |
| `.bak-<timestamp>` files alongside the live files | (not in repo) | Manual backups taken before each edit; safe to delete after a couple of weeks. |
| Cron state, log rotation, container start time | (not in repo) | Pure runtime, can't be captured in source. |

## Disaster recovery (fresh VPS from scratch)

If the VPS is rebuilt from a blank state, follow this sequence:

1. Install docker, sops, age, ssh keys.
2. Clone `Wizarck/eligia-core` to `/opt/eligia/eligia-core/` and decrypt
   `secrets/secrets.env`.
3. Clone this fork (`Wizarck/hermes-agent`) to `/opt/hermes/source/`.
4. Copy `deploy/eligia-vps/docker-compose.yml` → `/opt/hermes/docker-compose.yml`
   (or symlink). Same for `config.yaml` → `/opt/hermes/data/config.yaml`.
5. Restore mounted data dirs (`SOUL.md`, `memories/`, `agents/`, `skills/`)
   from your last backup.
6. Build the image: `cd /opt/hermes/source && docker build -f
   deploy/eligia-vps/Dockerfile.eligia-overlay -t eligia/hermes-agent:wamba .`
7. Install the `hermes.service` systemd unit (preserved at
   `/etc/systemd/system/hermes.service` — back this up separately, it's not
   in any repo today; consider adding it under `deploy/eligia-vps/` in a
   follow-up).
8. `systemctl enable --now hermes`.
9. Run the verification block above to confirm Langfuse is active.

Total time: ~30 min on a clean VPS, assuming backups of the mounted data
directories are intact.

## Out-of-scope (intentionally not here)

- Tunnel / DNS configuration (Cloudflared `eligia-hermes.palafitofood.com` →
  `127.0.0.1:8642`). Managed via the Cloudflare dashboard (`cloudflared`
  remote-managed tunnel; see eligia-core memory note `cloudflared-remote-managed.md`).
- The `eligia/hermes-agent:wamba` image itself — not pushed to a registry,
  rebuilt on the VPS from this source directory. Acceptable risk for a
  single-instance deployment.
