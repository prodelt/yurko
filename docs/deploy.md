# Deployment runbook — ukraine-laws-mcp v2

> ## This deployment is the legal environment (owner's decision, 2026-09-11)
>
> Until 2026-09-11 this runbook described an engineering bench: hosted
> embeddings and a hosted database meant the server **refused to start** under
> the default `YURKO_PROFILE=legal`, ran only as `engineering`, and
> `list_coverage` reported `legal_acceptance: false`. A lawyer connecting to it
> saw a product saying about itself that it had not passed legal acceptance.
>
> The owner decided the production deployment must carry the full MCP toolset so
> that international law can be worked properly. `render.yaml` now configures it
> accordingly, and the configuration is verified by a test rather than by
> reading: `tests/integration/test_production_profile.py`.
>
> What changed, and what each change costs:
>
> - `EMBEDDING_PROVIDER=null` — no request text reaches Google. The cost is that
>   the Ukrainian corpus is searched full-text only, without vectors.
>   International law is unaffected: EUR-Lex (Cellar), CURIA, HUDOC and the ICJ
>   are read by adapters directly, with no database in the path.
> - `YURKO_OWN_DATABASE=1` — the owner's declaration that the Postgres instance
>   is under their control. What lives in it is **public law**: 39 acts, 6487
>   articles. Case materials are not there and cannot be — the core never
>   receives them (principle VI: no tool takes a file, a path or an attachment).
> - `YURKO_HOSTED=1` — a hosted core has no output directory. The server's own
>   working directory is not the lawyer's storage, and writing there would be a
>   silent substitution of consent.
> - `YURKO_TOOL_GROUPS` — all six legal groups. Without it only `evidence` is
>   available: no document rendering, no task state, no moot court.
>
> **The boundary the profile does not check, stated plainly.** The server itself
> runs on Render. `yurko_profile` validates the database, the embeddings and
> hosted-model keys in the environment — it does not and cannot validate where
> the process runs. Render sees the traffic; the API key protects against
> strangers, not against the platform. A lawyer working through this deployment
> should know that, and a lawyer who cannot accept it runs the core locally over
> stdio, which remains fully supported (`README.md` → «Profiles»).
>
> Public packaging and an MCP-registry listing are deferred (T173) — they do not
> help read a norm before the deadline.

Stack (0 UAH budget):

- **Render** — hosts the HTTP MCP server (Docker), behind an API key.
- **Supabase** — managed Postgres + pgvector + Ukrainian FTS (the law corpus).
- **GitHub Actions** — loads the corpus and generates embeddings (`ingest.yml`).
- **Google Gemini** (free tier) — `gemini-embedding-001` @ 768 dims, used **only
  by the ingestion job**, never by the serving process. The distinction is the
  whole point: embedding a public act offline is not the same as sending a
  lawyer's query to a third party at request time, and only the second is what
  principle VIII forbids.

```
Claude / Cursor ──HTTP /mcp──▶ Render (server.py, YURKO_PROFILE=legal)
                                   │            EMBEDDING_PROVIDER=null
                                   ▼            → no query text leaves here
                            Supabase Postgres ◀── GitHub Actions (ingest.yml)
                            (pgvector + FTS)          │
                                                       └─▶ Gemini embeddings
                                                           (offline, public acts)
```

---

## 1. Supabase (database)

Create a Supabase project (region `eu-west-1` in production). The
schema (`laws`, `law_versions`, `articles`, `article_embeddings`) and extensions
(`vector` HNSW 768-dim, `unaccent`, `pg_trgm`, `ukrainian` FTS config) are applied
via `supabase/migrations/` and idempotently by `--ensure-schema`.

Connection string — use the **Transaction pooler (port 6543)** in production
(IPv4, free-tier connection-safe). URL-encode special characters in the password
(`,` → `%2C`, `@` → `%40`):

Take the "URI" connection string from Supabase → Project Settings → Database:
user `postgres.<ref>`, host `aws-0-eu-west-1.pooler.supabase.com`, port `6543`,
database `postgres`.

## 2. Load the corpus + embeddings (GitHub Actions)

Set repository secrets under **Settings → Secrets and variables → Actions**:

| Secret | Required | Purpose |
|---|---|---|
| `DATABASE_URL` | yes | Supabase Transaction pooler URI (above) |
| `GOOGLE_API_KEY` | **not on the server** | Gemini embeddings. Under `YURKO_PROFILE=legal` its presence stops the server from starting. Ingestion (`ingest.yml`) is a separate process and may hold it; the serving environment may not |

Run the **“Ingest laws into Supabase”** workflow (`workflow_dispatch`):

- `mode=full` (default) — fetch every law, chunk 0-article laws, split giant
  parser-artifact articles, then embed gaps.
- `mode=backfill` — no fetch; chunk 0-article laws + split giants + embed gaps
  from existing rows.
- `mode=embed` — embed remaining gaps only.

Embedding generation is **idempotent and resumable**: each run only fills the
remaining gap, so free-tier rate limits never corrupt state — just re-run
`mode=embed` until 0 remain. A **daily schedule** (`cron: 17 3 * * *`) self-heals
any leftover gaps automatically.

> **Gemini model & free tier.** Default is `gemini-embedding-001` (768-dim
> output): on the **free tier** it has the most usable daily embedding quota, so
> it completes the ~3.8k-article corpus at **0 UAH**. Note the free tier **meters
> embeddings to a daily quota (measured in production: ~100/day)**, so the corpus
> fills in **over ~a month** via the resumable daily cron rather than in a single run
> — a run that hits the quota simply stops writing (no error/corruption) and the
> next day's cron resumes. The newer `gemini-embedding-2` is higher quality but
> its free tier caps even harder — use it on a **paid tier** via
> `EMBEDDING_MODEL=gemini-embedding-2` (≈$0.20/1M tokens, ~$1.5 for the whole
> corpus, finishes in minutes). Meanwhile the server serves Ukrainian
> **full-text** results; hybrid ranking improves as vectors fill in.
>
> ⚠️ Switching models is **not** backward-compatible: each model has its own
> vector space. To change, set `EMBEDDING_MODEL` and **re-embed the whole corpus**
> (`DELETE FROM article_embeddings WHERE model <> '<new-model>';` then
> `--embed-missing`).

Confirm completion (Supabase SQL editor):

```sql
SELECT
  (SELECT count(*) FROM articles)            AS articles,
  (SELECT count(*) FROM article_embeddings)  AS embeddings,
  (SELECT count(*)
     FROM articles a
     JOIN law_versions v ON v.id = a.version_id
     LEFT JOIN article_embeddings e ON e.article_id = a.id
     WHERE v.status='current' AND e.article_id IS NULL) AS pending_embeddings;
```

`pending_embeddings = 0` ⇒ embeddings are complete.

## 3. Render (server)

`render.yaml` is a Blueprint — create the service from it. It pre-sets the legal
profile (`YURKO_PROFILE=legal`, `YURKO_HOSTED=1`, `YURKO_OWN_DATABASE=1`,
`EMBEDDING_PROVIDER=null`), the full tool catalogue (`YURKO_TOOL_GROUPS`),
`MCP_TRANSPORT=http`, `USE_PG_BACKEND=1`, an auto-generated
`UKRAINE_LAWS_API_KEY`, and declares `DATABASE_URL` as a dashboard-managed
secret.

Steps:

1. Render → **New → Blueprint** → pick this repo → apply `render.yaml`.
2. In the service’s **Environment**, set `DATABASE_URL` = the Transaction pooler
   URI from step 1. **Do not add `GOOGLE_API_KEY`**: under the legal profile a
   hosted-model key in the server's environment is a violation on its own, even
   if nothing calls it, and the server will refuse to start. The blueprint
   declares it with an empty value on purpose, so applying the blueprint
   overwrites whatever the dashboard held from the engineering-bench days — a
   variable the blueprint does not name keeps its old value, and that old value
   would stop the service from starting.
3. Deploy. The Dockerfile installs the Postgres extras (`psycopg`, `pgvector`).
4. Verify the profile took effect, from the outside rather than from the logs:
   `list_coverage` must report `legal_acceptance: true` and `profile: legal`. If
   it reports `false`, the service is running as an engineering bench and a
   lawyer must not be pointed at it.

## 4. Verify the deployment

```bash
# Liveness
curl -s https://<app>.onrender.com/health        # -> OK

# Backend confirmation — MUST read "postgres"
curl -s https://<app>.onrender.com/metrics | jq '{backend, pg_backend, pg_error, cache: .cache.backend, pg: .cache.pg}'
# {
#   "backend": "postgres", "pg_backend": "active", "pg_error": null,
#   "cache": "postgres",
#   "pg": { "laws": 38, "articles": 6244, "embeddings": 478, "pending_embeddings": 5596 }
# }
```

### Troubleshooting `backend: file`

The server **degrades gracefully** to the file cache rather than crash, so
`backend: file` means a config problem, not an outage. The `pg_backend` field
pinpoints it (no secrets are exposed):

| `pg_backend` | Cause | Fix |
|---|---|---|
| `disabled` | `USE_PG_BACKEND` is not `1` | Set `USE_PG_BACKEND=1` in the Render env, then redeploy. **An existing Render service does not auto-adopt new `render.yaml` env vars** — add it manually (or re-sync the Blueprint). |
| `no_database_url` | `USE_PG_BACKEND=1` but `DATABASE_URL` empty | Set `DATABASE_URL` (Transaction pooler, port 6543). |
| `init_failed` | DB unreachable / bad DSN | Use the **pooler** host (port 6543), **URL-encode** the password, then redeploy. `pg_error` shows the exception type. |

> Auto-deploy ships new **code** from `master`, but **env vars are separate** —
> after a code deploy you may still need to set `USE_PG_BACKEND=1` +
> `DATABASE_URL` once and redeploy for the backend to flip to `postgres`.

MCP endpoint: `https://<app>.onrender.com/mcp` (send the API key as the
`x-api-key` header or `?api_key=` query param for claude.ai web connectors).

## 5. Verify the registry adapters (v2.3.0)

The court/state-registry adapters call **live government endpoints** whose API
shapes can drift, and the dev sandbox has no egress to `*.gov.ua` — so after
each deploy, smoke-test them once from the deployed server (any MCP client):

| Tool call | Expected |
|---|---|
| `list_registries()` | 5 sources, all `ok: true` |
| `search_court_decisions(query="стягнення заборгованості")` | non-empty `results` with `decision_id`s |
| `get_court_decision(decision_id=<id from search>)` | decision text, `char_count > 200` |
| `search_debtors(name="<any common surname>")` | results list (possibly empty, but no error) |
| `search_tenders(query="медичне обладнання")` | tenders with `UA-…` ids |
| `get_tender(tender_id=<internal_id from search>)` | tender summary, `status` populated |
| `discover_registries(query="реєстр боржників")` | data.gov.ua datasets |

If an upstream changed its endpoint or payload shape, the tool returns a typed
`source_unavailable` error (never crashes the server). **Repoint without a code
release** via env overrides, then redeploy:

| Env var | Default |
|---|---|
| `COURT_REGISTRY_URL` | `https://reyestr.court.gov.ua` |
| `ERB_API_URL` | `https://erb.minjust.gov.ua/api/erbexternal/searchdebtorsexternal` |
| `PROZORRO_API_URL` | `https://public-api.prozorro.gov.ua/api/2.5` |
| `PROZORRO_SEARCH_URL` | `https://prozorro.gov.ua/api/search/tenders` |
| `DATA_GOV_UA_API_URL` | `https://data.gov.ua/api/3/action/package_search` |

`/metrics` → `sources` shows each adapter's health, base URL, and backoff state
for diagnosis without server logs.
