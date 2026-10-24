# 9. Production deployment

This chapter deploys Invariant on one Linux server with Docker Compose,
behind a reverse proxy that terminates HTTPS. It is the setup the repository
is built and tested for; CI's `docker` job checks the same Compose stack on
every change.

> **Verification:** the Compose stack, health checks, backup and restore
> are tested in CI. The reverse proxy configuration, firewall and log
> rotation below are an example, not tested for this guide; adapt them to
> your infrastructure and test on a staging host first.
>
> **Pending v1.0 (stage 5e):** deploy a release tag (`v1.0.0`) once it
> exists, and follow its upgrade guide. Until then, deploy a reviewed
> commit, never a moving branch.

## What you are deploying

```text
Internet ──HTTPS 443──▶ reverse proxy (host) ──HTTP──▶ 127.0.0.1:8000 app
                                                         worker ──HTTPS──▶ api.github.com
                                                         db (127.0.0.1:5433, never public)
```

- One API key protects the whole API. There are no users or roles: anyone
  with the key can read and delete everything. Treat it like a database
  password.
- The API serves plain HTTP and must not be reachable from outside except
  through the proxy. `compose.yaml` already binds both published ports to
  `127.0.0.1`; keep it that way.
- The worker needs outbound HTTPS to GitHub only.

## 1. The server

- A Linux host with Docker Engine and the Compose plugin, a DNS name
  (`invariant.example.com` below), and a firewall that allows only SSH and
  443 (and 80 if the proxy uses it for certificates). With `ufw`:

  ```bash
  sudo ufw default deny incoming
  sudo ufw allow OpenSSH
  sudo ufw allow 80,443/tcp
  sudo ufw enable
  ```

  Docker's own port publishing bypasses `ufw` rules, which is one more
  reason the Compose ports must stay on `127.0.0.1`.
- A dedicated, non-root deploy user in the `docker` group. Membership of
  that group is root-equivalent on the host; give it to nobody else.

## 2. The code

```bash
git clone https://github.com/IliyasNU/invariant.git /opt/invariant
cd /opt/invariant
git checkout <reviewed commit or release tag>
```

## 3. Secrets: `.env`

Create `.env` from `.env.example` and fill in **every** secret. Generate
each value separately:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

```dotenv
POSTGRES_PASSWORD=<random>               # not the example "invariant"
INVARIANT_API_KEY=<random, at least 32 characters>
GITHUB_WEBHOOK_SECRET=<random>           # the same value in the GitHub webhook
GITHUB_TOKEN=<fine-grained token>        # see below
GITHUB_POST_PR_COMMENT=true              # only if you want PR comments
INVARIANT_PUBLIC_URL=https://invariant.example.com
INVARIANT_LOG_FORMAT=json
# INVARIANT_RETENTION_DAYS=180           # optional; default keeps all history
```

Then:

```bash
chmod 600 .env
```

- Do **not** set `INVARIANT_LOCAL_ONLY` in production. (With a key set it
  changes nothing, but it documents the wrong intent.)
- Leave `INVARIANT_CORS_ORIGINS` empty unless a browser application on
  another origin calls the API; then list its exact origins. `*` is refused.
- Set `POSTGRES_PASSWORD` **before the first start**. The PostgreSQL image
  applies it only when it creates the data volume; changing `.env` later
  does not change the password inside an existing database (use
  `ALTER USER invariant PASSWORD '…'` in `psql`, then update `.env`).
- **GitHub token:** a fine-grained token limited to the analyzed
  repositories, with *Contents: read* and *Pull requests: read*, plus *Pull
  requests: write* only for comments. Set an expiry and a reminder to rotate
  it. The token is sent only to `GITHUB_API_URL`.
- The secrets are never stored in the database, returned by the API, or
  written to logs: the logger redacts them, and anything shaped like a
  token or a password in a URL.

## 4. Start

```bash
docker compose up -d --build
docker compose ps
```

`migrate` runs `alembic upgrade head` once and exits `0`; `app` and `worker`
start after it and become `(healthy)`. Check locally on the server:

```bash
curl -s http://127.0.0.1:8000/ready                   # {"status":"ready"}
curl -s -H "Authorization: Bearer $KEY" http://127.0.0.1:8000/projects
```

## 5. HTTPS: the reverse proxy

Any proxy that terminates TLS works (nginx, Caddy, Traefik, a cloud load
balancer). A minimal Caddy example, which obtains and renews the
certificate itself; `/etc/caddy/Caddyfile` on the host:

```caddyfile
invariant.example.com {
    reverse_proxy 127.0.0.1:8000
    request_body {
        max_size 16MB
    }
}
```

Then, from your own machine:

```bash
curl -s https://invariant.example.com/ready
```

Notes:

- Keep the proxy's body limit at or below `INVARIANT_MAX_BODY_BYTES`
  (16 MiB), so large requests are refused early.
- Invariant's rate limits count per client address. Behind a proxy every
  client has the proxy's address, so one abusive client can use up the
  limit for everyone. Add a per-client rate limit at the proxy if the API is
  used by many clients.
- `/docs` (the interactive documentation) and `/openapi.json` are public
  but show no data. Block them at the proxy if you prefer.
- Invariant warns at startup if `INVARIANT_PUBLIC_URL` is plain `http://`.

## 6. GitHub

Add the webhook as in [chapter 6](06-github-webhooks.md#connect-a-real-repository),
with the payload URL `https://invariant.example.com/integrations/github/webhook`.
GitHub's *Recent Deliveries* should show `202` responses.

## 7. Operations

**Health.** Point the load balancer or uptime monitor at `GET /ready`
(`503` when the database is down or not migrated); `GET /health` is only
liveness. `docker compose ps` shows the container health checks; the
worker's checks its heartbeat file.

**Logs.** One JSON object per line, on the containers' standard error.
Collect them with your log system, or at least rotate them: in
`/etc/docker/daemon.json`,
`{"log-driver": "json-file", "log-opts": {"max-size": "50m", "max-file": "5"}}`,
then restart Docker. Search by `request_id` (from the `X-Request-ID` header)
or `run_id`.

**Backups.** Everything to keep is in PostgreSQL. A nightly dump, kept off
the server:

```bash
cd /opt/invariant
docker compose exec -T db pg_dump --username invariant --format custom invariant \
  > /var/backups/invariant/invariant-$(date +%F).dump
```

A backup you have not restored is not a backup: restore one into a scratch
database regularly, as [chapter 3](03-docker-postgres.md#backup-and-restore)
shows. Back up `.env` separately and securely (a password manager or a
secret store); it is not in the dump.

**Retention.** `INVARIANT_RETENTION_DAYS=N` makes the worker delete finished
runs older than N days, hourly. Queued and running runs, and projects and
rules, are never deleted. Preview with
`docker compose exec worker python -m app.retention --days N --dry-run`.

**Scaling the worker.** Several workers are safe (each run is claimed with a
row lock): `docker compose up -d --scale worker=2`. Keep
`WORKER_LEASE_SECONDS` longer than your slowest analysis, or runs are taken
over and analyzed twice.

**Rotating the API key.** Change `INVARIANT_API_KEY` in `.env` and run
`docker compose up -d`; the new key works once the `app` container is
recreated, and the old one stops working. There is one key, so every client
must switch at the same time.

## 8. Upgrades

1. Read the changelog and the diff of `migrations/versions/`.
2. Back up the database (above).
3. `git fetch && git checkout <new tag>`
4. `docker compose up -d --build`: `migrate` applies the new migrations
   before the new `app` and `worker` start. If it fails, they do not start;
   `docker compose logs migrate` says why.
5. Check `/ready` and a request with the key.

Rolling back code after a migration needs the matching
`alembic downgrade`, and some downgrades refuse to run rather than lose
data. The safe rollback is: the old code **and** the backup taken in step 2.

## Production checklist

- [ ] Only SSH and 443 (and 80) reachable from outside; ports 8000 and 5433 bound to `127.0.0.1`.
- [ ] HTTPS in front of the API; `INVARIANT_PUBLIC_URL` is `https://`.
- [ ] `INVARIANT_API_KEY`, `POSTGRES_PASSWORD`, `GITHUB_WEBHOOK_SECRET` random and distinct; `.env` mode `600`, not in Git.
- [ ] `INVARIANT_LOCAL_ONLY` not set; `INVARIANT_CORS_ORIGINS` empty or exact.
- [ ] GitHub token fine-grained, least privilege, with an expiry.
- [ ] Nightly `pg_dump` stored off the host, a restore tested.
- [ ] `/ready` monitored; logs collected or rotated.
- [ ] A reviewed commit or release tag deployed, not a branch.
