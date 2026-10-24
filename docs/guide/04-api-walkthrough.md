# 4. API walkthrough

This chapter builds one small project through the API: a project, two
rules, four analyses with four different outcomes, then the history. Every
response below was copied from a real run (IDs and timestamps will differ on
your machine; long responses are shortened with `…`).

Start the database and the API as in [chapter 3](03-docker-postgres.md),
with an API key. Then, in the terminal you send requests from:

```bash
export INVARIANT_API_KEY=...            # the same key the API was started with
B=http://127.0.0.1:8000
AUTH="Authorization: Bearer $INVARIANT_API_KEY"
J="Content-Type: application/json"
```

Everything below can also be done in the browser at `$B/docs`.

## 0. The key

```bash
curl -i $B/projects
```

```text
HTTP/1.1 401 Unauthorized
www-authenticate: Bearer
x-request-id: 627653515c724546a47204e172ee4627

{"error":{"code":"unauthorized","message":"An API key is required: Authorization: Bearer <key>","request_id":"627653515c724546a47204e172ee4627"}}
```

Two things to notice, because every response has them:

- the **error shape** `{"error": {"code", "message", "request_id"}}`. Programs
  compare `code`; `message` is for people;
- the **`X-Request-ID`** header. The server's log line for this request
  carries the same ID, so when something goes wrong you search the logs for
  it.

`/health`, `/ready` and `/docs` need no key. The GitHub webhook does not use
the key either: GitHub signs it ([chapter 6](06-github-webhooks.md)).

## 1. Create a project

```bash
curl -s -H "$AUTH" -H "$J" -X POST $B/projects \
  -d '{"name":"shop","description":"Tutorial project"}'
```

```json
{"id":1,"name":"shop","description":"Tutorial project","github_owner":null,"github_repo":null,"source_roots":["."],"exclude_paths":[],"fail_on":"error","created_at":"2026-10-24T07:11:09.972139Z"}
```

The same name again is a conflict:

```json
{"error":{"code":"project_conflict","message":"A project with this name or GitHub repository already exists","request_id":"…"}}
```

Our code lives under `src/`, so `src/shop/domain/order.py` should be the
module `shop.domain.order`. Tell the project:

```bash
curl -s -H "$AUTH" -H "$J" -X PATCH $B/projects/1 -d '{"source_roots":["src"]}'
```

`PATCH` changes only the fields it sends. Why source roots matter is
explained in [configuration.md](../configuration.md#module-names-and-source-roots).

## 2. Add rules

A boundary rule, and a size rule that only warns:

```bash
curl -s -H "$AUTH" -H "$J" -X POST $B/projects/1/rules -d '{
  "key": "domain-isolation",
  "name": "Domain must not import infrastructure",
  "rule_type": "forbidden_import",
  "source_pattern": "shop.domain.*",
  "forbidden_imports": ["shop.infrastructure"]
}'

curl -s -H "$AUTH" -H "$J" -X POST $B/projects/1/rules \
  -d '{"key":"small-files","rule_type":"max_file_lines","max_lines":200,"severity":"warning"}'
```

```json
{"id":1,"project_id":1,"key":"domain-isolation","name":"Domain must not import infrastructure","rule_type":"forbidden_import","enabled":true,"severity":"error","source_pattern":"shop.domain.*","exclude_paths":[],"forbidden_imports":["shop.infrastructure"],"forbidden_calls":null,"max_lines":null,"created_at":"…"}
```

An invalid rule is refused with the field that is wrong, and without
echoing the value back:

```bash
curl -s -H "$AUTH" -H "$J" -X POST $B/projects/1/rules \
  -d '{"key":"x","rule_type":"max_file_lines","max_lines":0}'
```

```json
{"error":{"code":"validation_error","message":"The request is not valid","request_id":"…","fields":[{"location":"body.max_file_lines.max_lines","message":"Input should be greater than or equal to 1","type":"greater_than_equal"}]}}
```

Lists are paginated, and carry the total in a header:

```bash
curl -s -i -H "$AUTH" $B/projects/1/rules | grep -i x-total-count
# x-total-count: 2
```

## 3. Analyze code: four outcomes

Direct analysis sends file contents in the request and gets the stored run
back. The code is parsed, never run.

**Pass.** Nothing forbidden is imported:

```bash
curl -s -H "$AUTH" -H "$J" -X POST $B/projects/1/analyze -d '{
  "files": [{"path": "src/shop/domain/order.py", "content": "from dataclasses import dataclass\n"}]
}'
```

```text
id 1, status completed, outcome pass, passed true,
files {total 1, eligible 1, analyzed 1, excluded 0, skipped 0, ignored 0},
summary {errors 0, warnings 0, analysis_errors 0}
```

**Fail.** The domain imports infrastructure:

```bash
curl -s -H "$AUTH" -H "$J" -X POST $B/projects/1/analyze -d '{
  "files": [{"path": "src/shop/domain/order.py", "content": "from shop.infrastructure.db import session\n"}]
}'
```

```json
{
  "id": 2, "status": "completed", "outcome": "fail", "passed": false,
  "summary": {"errors": 1, "warnings": 0, "analysis_errors": 0},
  "violations": [{
    "kind": "violation", "code": "forbidden_import", "severity": "error",
    "rule_id": 1, "rule_key": "domain-isolation",
    "rule_name": "Domain must not import infrastructure",
    "path": "src/shop/domain/order.py", "line": 1, "column": 1,
    "imported_module": "shop.infrastructure.db",
    "message": "shop.domain.order must not import shop.infrastructure (matched shop.infrastructure.db)",
    "data": {"imported_module": "shop.infrastructure.db", "forbidden": "shop.infrastructure"}
  }],
  "…": "…"
}
```

**Incomplete.** A file that does not parse is not skipped quietly. It
becomes an `error` diagnostic, and without a blocking violation elsewhere
there is no verdict: `passed` is `null`.

```bash
curl -s -H "$AUTH" -H "$J" -X POST $B/projects/1/analyze \
  -d '{"files":[{"path":"src/shop/domain/broken.py","content":"def (:\n"}]}'
# outcome incomplete, passed null, violations [("error", "syntax_error")]
```

**Not applicable.** `README.py` is outside the source root `src`, so it is
excluded and nothing was checked. That is not a pass either:

```bash
curl -s -H "$AUTH" -H "$J" -X POST $B/projects/1/analyze \
  -d '{"files":[{"path":"README.py","content":"x=1\n"}]}'
# outcome not_applicable, passed null, files {"total":1,"eligible":0,"analyzed":0,"excluded":1,…}
```

Learn this table; the whole product is built around it:

| `outcome` | `passed` | Means |
| --- | --- | --- |
| `pass` | `true` | Every applicable check ran, no violation reached `fail_on` |
| `fail` | `false` | A violation reached `fail_on` |
| `incomplete` | `null` | Some file could not be analyzed |
| `not_applicable` | `null` | Nothing was checked |

Paths are checked before anything runs:

```bash
curl -s -H "$AUTH" -H "$J" -X POST $B/projects/1/analyze \
  -d '{"files":[{"path":"../etc/x.py","content":""}]}'
```

```json
{"error":{"code":"validation_error","message":"The request is not valid","request_id":"…","fields":[{"location":"body.files.0.path","message":"Value error, path must not contain '..'","type":"value_error"}]}}
```

## 4. History

```bash
curl -s -H "$AUTH" "$B/projects/1/analysis-runs?outcome=fail"     # newest first, filtered
curl -s -H "$AUTH" $B/analysis-runs/2                              # one run, with every diagnostic
curl -s -H "$AUTH" "$B/analysis-runs/2/diagnostics?kind=violation" # diagnostics, paginated
curl -s -H "$AUTH" $B/projects/1/stats
```

The stats after the four analyses above:

```json
{"project_id": 1, "runs": 4,
 "by_status": {"queued": 0, "running": 0, "completed": 4, "failed": 0},
 "by_outcome": {"pass": 1, "fail": 1, "incomplete": 1, "not_applicable": 1},
 "pass_rate": {"passed": 1, "decided": 2, "without_verdict": 2, "rate": 0.5},
 "diagnostics": {"errors": 1, "warnings": 0, "analysis_errors": 1},
 "duration": {"runs": 4, "average_ms": 1.75, "max_ms": 3},
 "latest": ["…"]}
```

The rate is 1 of 2 decided runs, not 1 of 4: runs without a verdict are
counted apart instead of lowering it.

`GET /analysis-runs/2/result` returns the run in the engine's own result
format, the same document that `invariant check --format json` prints
(keys `schema_version`, `engine_version`, `outcome`, `passed`, `fail_on`,
`files`, `rules`, `summary`, `diagnostics`, `source`, `run`).

## 5. Configuration as a file

Export the project as an `invariant.toml`:

```bash
curl -s -H "$AUTH" $B/projects/1/config -o invariant.toml
cat invariant.toml
```

```toml
version = 1

[project]
source_roots = ["src"]
exclude_paths = []
fail_on = "error"

[[rules]]
key = "domain-isolation"
name = "Domain must not import infrastructure"
rule_type = "forbidden_import"
enabled = true
severity = "error"
source_pattern = "shop.domain.*"
exclude_paths = []
forbidden_imports = ["shop.infrastructure"]

[[rules]]
key = "small-files"
name = "small-files"
rule_type = "max_file_lines"
enabled = true
severity = "warning"
source_pattern = "*"
exclude_paths = []
max_lines = 200
```

The same file works with the CLI ([chapter 5](05-cli-walkthrough.md)). Edit
it, then import it with `dry_run` to see the plan without changing anything:

```bash
sed -i 's/max_lines = 200/max_lines = 300/' invariant.toml
curl -s -H "$AUTH" -H "$J" -X POST $B/projects/1/config/import \
  -d "$(python3 -c 'import json; print(json.dumps({"content": open("invariant.toml").read(), "dry_run": True}))')"
```

```json
{"mode":"merge","dry_run":true,"applied":false,"project_changes":[],"created":[],"updated":["small-files"],"deleted":[],"unchanged":["domain-isolation"]}
```

Send it again without `dry_run` to apply it, in one transaction. With
`"mode": "replace"`, rules missing from the file are deleted.

## 6. A few more errors

```bash
curl -s -H "$AUTH" $B/projects/999
# 404 {"error":{"code":"project_not_found","message":"Project not found",…}}

curl -s -H "$AUTH" -H "$J" -X POST $B/projects/1/analyze/github -d '{"pull_number":1}'
# 409 {"error":{"code":"github_not_configured","message":"Project has no GitHub owner/repository configuration",…}}

curl -s -H "Authorization: Bearer wrong" $B/projects
# 401 {"error":{"code":"unauthorized","message":"The API key is not valid",…}}
```

The complete list of codes and limits (`413`, `429`, …) is in the README:
[Errors](../../README.md#errors) and [Limits](../../README.md#limits).

## Clean up

`DELETE /projects/1` returns `204` and deletes the project **with its rules
and its whole history**.

Next: [CLI walkthrough](05-cli-walkthrough.md).
