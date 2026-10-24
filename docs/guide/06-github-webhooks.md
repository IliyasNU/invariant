# 6. GitHub webhooks

A webhook is an HTTP request that GitHub sends to your server when something
happens in a repository. Invariant listens for pull request events: when a
pull request is opened, gets new commits (`synchronize`) or is reopened, it
analyzes the changed Python files and, if you allow it, keeps one comment on
the pull request with the result.

This chapter runs the whole path on your machine, against a stand-in GitHub,
so you need no GitHub account, token or public URL. Then it explains how to
connect a real repository.

## What happens, in order

1. GitHub sends `POST /integrations/github/webhook` with the event as JSON
   and three headers: `X-GitHub-Event` (`pull_request`), `X-GitHub-Delivery`
   (a unique ID for this delivery) and `X-Hub-Signature-256`.
2. The API checks the signature **before reading the JSON**. The signature
   is `sha256=` + HMAC-SHA256 of the exact raw body with the shared secret
   `GITHUB_WEBHOOK_SECRET`, compared in constant time. Without a configured
   secret the endpoint answers `503`, with a wrong signature `401`.
3. Events other than `pull_request`, and other actions (`closed`, `edited`,
   …), get `202 {"status": "ignored"}`.
4. The repository's owner and name select the project with the same
   `github_owner`/`github_repo` (compared lower-case). None: `404
   github_project_not_found`.
5. The API inserts a run with status `queued` and the delivery ID, commits,
   and only then answers `202 {"status": "queued", "analysis_run_id": …}`.
   The delivery ID is unique in the database: GitHub delivering the same
   event twice, even at the same moment, gets `200 {"status": "duplicate"}`
   and the first run's ID.
6. The worker claims the run, downloads the pull request's changed Python
   files at its head commit, runs the engine and stores the result.
7. If comments are on, the worker creates the comment, or edits the one it
   wrote before.

The API answers quickly and never calls GitHub itself; GitHub stops waiting
for a webhook after 10 seconds.

## Try it locally

You need four terminals in the repository root, the database running and
migrated ([chapter 3](03-docker-postgres.md)).

**Terminal 1, the stand-in GitHub.** `scripts/github_stub.py` answers like
GitHub for pull request 7 of any repository: one changed file,
`app/domain/orders.py`, containing `import app.infrastructure.db`.

```bash
python3 scripts/github_stub.py 8080
```

**Terminal 2, the API**, pointed at the stub:

```bash
export INVARIANT_API_KEY=...                       # your key
export GITHUB_WEBHOOK_SECRET=tutorial-webhook-secret
export GITHUB_API_URL=http://127.0.0.1:8080
export GITHUB_TOKEN=fake-token GITHUB_POST_PR_COMMENT=true
export INVARIANT_PUBLIC_URL=http://127.0.0.1:8000 INVARIANT_LOG_FORMAT=text
uv run python -m app.server
```

**Terminal 3, the worker**, with the same variables (paste the same
`export` lines), then:

```bash
uv run python -m app.worker
```

**Terminal 4, you.** Create a project for the repository `example-org/orders`
with one rule:

```bash
B=http://127.0.0.1:8000; AUTH="Authorization: Bearer $INVARIANT_API_KEY"; J="Content-Type: application/json"
curl -s -H "$AUTH" -H "$J" -X POST $B/projects \
  -d '{"name":"demo-gh","github_owner":"Example-Org","github_repo":"orders"}'
# … "github_owner":"example-org","github_repo":"orders" … (stored lower-case)
curl -s -H "$AUTH" -H "$J" -X POST $B/projects/<id>/rules \
  -d '{"key":"domain-isolation","source_pattern":"app.domain.*","forbidden_imports":["app.infrastructure"]}'
```

Now play GitHub. Save this as `send_webhook.py` anywhere outside the
repository; it uses only the standard library:

```python
import hashlib
import hmac
import json
import sys
import urllib.error
import urllib.request
import uuid

secret = b"tutorial-webhook-secret"
body = json.dumps({
    "action": "opened",
    "repository": {"name": "orders", "owner": {"login": "example-org"}},
    "pull_request": {"number": 7},
}).encode()
signature = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
delivery = sys.argv[1] if len(sys.argv) > 1 else str(uuid.uuid4())
request = urllib.request.Request(
    "http://127.0.0.1:8000/integrations/github/webhook",
    data=body,
    method="POST",
    headers={
        "Content-Type": "application/json",
        "X-GitHub-Event": "pull_request",
        "X-GitHub-Delivery": delivery,
        "X-Hub-Signature-256": signature,
    },
)
try:
    with urllib.request.urlopen(request) as response:
        print(response.status, response.read().decode())
except urllib.error.HTTPError as error:
    print(error.code, error.read().decode())
```

Send the same delivery twice:

```bash
python3 send_webhook.py delivery-2
python3 send_webhook.py delivery-2
```

```text
202 {"status":"queued","analysis_run_id":6,"outcome":null,"detail":null}
200 {"status":"duplicate","analysis_run_id":6,"outcome":null,"detail":null}
```

The worker's log (terminal 3) follows the run by its ID:

```text
INFO app.worker Run 6 claimed, attempt 1 event=run_claimed attempt=1 worker=… run_id=6
INFO app.analysis_service Pull request 7 fetched at 111111111111 event=pull_request_fetched head_sha=1111… files=1 skipped=0 truncated=False … run_id=6
INFO app.analysis_service Analysis finished: fail event=analysis_finished outcome=fail diagnostics=1 … run_id=6
INFO app.worker Run 6 completed: fail event=run_completed outcome=fail … run_id=6
INFO app.feedback The comment for run 6 is posted event=feedback_posted comment_id=1 new_comment=True run_id=6
```

And the comment it wrote is in the stub, at `curl -s http://127.0.0.1:8080/_comments`:

```markdown
**Invariant: FAIL** for `111111111111`: 1 violation at or above error

- `app/domain/orders.py:1` error: `app.domain.orders must not import app.infrastructure (matched app.infrastructure.db)`

Checked 1 changed Python file against 1 rule; 0 excluded, 0 not analyzed. Invariant reads the code without running it, so dynamic imports and calls made through variables are not seen.
Details: [run 6](http://127.0.0.1:8000/analysis-runs/6) · Invariant 0.3.0.dev0
```

Send another delivery (no argument: a new ID) and the comment is **edited**,
not duplicated. The stub still holds one comment for pull request 7.

Try the failure cases too:

```bash
# a wrong signature: 401 invalid_github_signature
curl -s -X POST -H "X-GitHub-Event: pull_request" -H "X-Hub-Signature-256: sha256=00" \
  -d '{}' $B/integrations/github/webhook
# the API without GITHUB_WEBHOOK_SECRET: 503 github_webhook_not_configured
```

Stop the worker with Ctrl+C (or `kill -TERM`): it finishes the run in
progress and logs `Worker stopped after N runs`. Queued runs wait in the
database for the next worker.

## The same without a webhook

`POST /projects/{id}/analyze/github` with `{"pull_number": 7}` queues the
same job by hand and returns `202` with the queued run. Poll
`GET /analysis-runs/{id}` until `status` is `completed` or `failed`. Unlike
a webhook delivery, each request queues a new run.

## Retries, leases and stale results

These are the parts of `app/worker.py` and `app/feedback.py` that make the
GitHub path safe. Read them with the tests named in the
[study guide](08-study-guide.md).

- **Claiming.** A worker takes the oldest due `queued` run with
  `SELECT … FOR UPDATE SKIP LOCKED`, so two workers never take the same run,
  and holds it under a lease (`WORKER_LEASE_SECONDS`, 10 minutes).
- **A dead worker.** When a lease expires, another worker queues the run
  again (`worker_lost`), or fails it after `WORKER_MAX_ATTEMPTS`. A worker
  writes its result only if it still holds the run, so a slow worker cannot
  overwrite the result of the one that took over.
- **Transient GitHub errors** (network, rate limit, 5xx) queue the run again
  after `WORKER_RETRY_SECONDS` × 2^(attempt−1), at most an hour, and never
  before GitHub's `Retry-After` or rate-limit reset. **Other errors**
  (404, bad credentials, a malformed response) fail the run at once with a
  safe `error.code` such as `github_not_found`.
- **A moving pull request.** After listing the files, the worker checks
  that the head commit did not change meanwhile; if it did, the attempt is
  retried rather than mixing two commits.
- **Limits.** Each file at most 1 MB, 50 MB per pull request, within
  `WORKER_JOB_SECONDS`. Beyond them the run is `incomplete`, never a pass.
- **Stale comments.** Before writing the comment, the worker reads the pull
  request's current head. A result for an older commit, or older than the
  run the comment already shows, is marked `stale` and not posted, so a late
  run never shows an old PASS as current.
- **Comment failures never change the result.** Publishing has its own
  status (`run.feedback`: `pending`, `posted`, `stale`, `failed`) and its own
  retries.

## Connect a real repository

> Not tested end to end for this guide; follow it on a test repository first.

1. The API must be reachable from GitHub over **HTTPS**: deploy it
   ([chapter 9](09-production-deployment.md)) or, for a test, run a tunnel
   to `127.0.0.1:8000`.
2. Generate a secret, for example
   `python3 -c 'import secrets; print(secrets.token_hex(32))'`, and set it
   as `GITHUB_WEBHOOK_SECRET`.
3. In the repository: *Settings → Webhooks → Add webhook*. Payload URL
   `https://<your-host>/integrations/github/webhook`, content type
   `application/json`, the same secret, *Let me select individual events* →
   **Pull requests** only.
4. Create the Invariant project with that `github_owner` and `github_repo`,
   and its rules.
5. For private repositories or comments, create a **fine-grained** token for
   that repository only, with *Contents: read*, *Pull requests: read*, and
   *Pull requests: write* only if `GITHUB_POST_PR_COMMENT=true`. Set it as
   `GITHUB_TOKEN`.
6. Open a pull request. GitHub's *Recent Deliveries* tab shows each delivery,
   the response, and a **Redeliver** button: a redelivery has the same
   delivery ID, so Invariant answers `duplicate` instead of analyzing again.

Next: [common errors and debugging](07-troubleshooting.md).
