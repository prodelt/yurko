# Yurko

Yurko is an MCP server that gives a model access to legal sources, plus a Claude Code skill that drafts a legal position on top of it. I built it so that a model stops quoting the law from memory. Every legal statement in a draft sits under a verbatim quote from the source, with the article and the version it came from.

## The MCP server

The server reads Ukrainian legislation from the Verkhovna Rada site, EU law through Cellar, ELI and CELEX, case law of the European Court of Human Rights and the Court of Justice of the EU, case cards of the International Court of Justice, and public Ukrainian registries: court decisions, debtors and Prozorro tenders. The legal order is a parameter (`legal_order`), so adding a jurisdiction adds a value, not a new set of tools.

`verify_quote` checks a quote against the official text and answers with one of three results: a verbatim match, a match after normalisation, or a mismatch with the differences marked. A match proves the words are there; it does not prove the quote supports the argument. `list_coverage` says which operations have been tested for each source.

This public edition exposes 19 tools; the private build I run has 23.

| Group | Tools |
|---|---|
| Legislation | `resolve_law_id`, `query_law`, `get_article`, `get_multiple_articles`, `get_law_metadata`, `list_laws`, `discover_laws`, `search_across_laws`, `search_articles` |
| Case law | `search_decisions`, `get_decision`, `get_case` |
| Registries | `search_debtors`, `search_tenders`, `get_tender`, `list_registries`, `discover_registries` |
| Checks | `verify_quote`, `list_coverage` |

## The `legal` profile

The default profile is built for work on real matters:

- free text from the user is never sent to the sources;
- the server refuses to start with someone else's database or a model API key;
- the server process sees only environment variables from an allow-list (`server_env.py`);
- no tool accepts a file or a path, so case materials cannot reach the server.

## The `/yurko` skill

An orchestrator is the only author of documents. Sub-agents read, search, argue the other side and check the draft. Risks are given as categories, never as a percentage chance, and procedural deadlines are left to a lawyer. The run keeps its state in files, so it can stop and resume, and it records its own cost.

A separate court cycle runs an adversarial simulation with information barriers between the roles and a blind panel of three models. It is back-tested on cases whose outcome is already known.

## In production

The server runs on Render with a Postgres corpus. In production the Ukrainian corpus is searched with full-text search only, so query text never goes to an embedding provider. Hybrid search (full text plus vectors, merged with reciprocal rank fusion) is in the code but switched off.

## Run it locally

Python 3.12 or newer. Dependency versions are pinned: `uv.lock` is the single source, and `requirements.txt` (stdio mode) and `requirements-hosted.txt` (Docker, with Postgres and Playwright) are exported from it with hashes.

With uv, without installing anything into the code folder:

```bash
uv run --quiet --no-project --python 3.12 --with-requirements requirements.txt python server_env.py
```

With your own virtual environment:

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt          # Windows: .venv\Scripts\pip
.venv/bin/python server_env.py                     # stdio; the entry point builds an allow-listed environment
```

Connect it to Claude Code (absolute paths):

```bash
claude mcp add yurko -s user -- uv run --quiet --no-project --python 3.12 --with-requirements /abs/path/requirements.txt python /abs/path/server_env.py
```

HTTP mode for a hosted server: `MCP_TRANSPORT=http`, `PORT` and `UKRAINE_LAWS_API_KEY`. The image and the deployment are in `Dockerfile` and `render.yaml`.

## Settings

| Variable | Default | What it does |
|---|---|---|
| `YURKO_PROFILE` | `legal` | `legal` keeps free text away from the sources and will not start with a foreign database or a model API key; `engineering` is for development |
| `YURKO_CACHE_DIR` | the OS cache directory | where the server writes its cache; it never writes into the code folder |
| `USE_PG_BACKEND`, `DATABASE_URL` | `0` | use Postgres instead of the file cache |
| `RADA_LIVE_CHANNEL` | `1` | `0` turns off live reads from the Rada site |
| `COURT_REGISTRY_URL`, `ERB_API_URL`, `PROZORRO_API_URL`, `PROZORRO_SEARCH_URL`, `DATA_GOV_UA_API_URL` | official endpoints | override a registry endpoint |

## Development

Tests never touch the network or the code folder. `tests/conftest.py` fails any test that tries to go online, points `YURKO_CACHE_DIR` at a temporary folder and checks that `cache/` in the code folder stays unchanged. Tests that really need the network are marked `live` and run only with `YURKO_LIVE=1`.

```bash
uv sync --frozen --extra dev
.venv/bin/python -m pytest -q          # Windows: .venv\Scripts\python
```

Known quirks of the Rada site, such as TLS resets on large handshakes and ignored search filters, are written up in [docs/](docs/).

## License

MIT, see [LICENSE](LICENSE). The skill under `skill/` adapts parts of other open-source projects; see `skill/NOTICE.md`.
