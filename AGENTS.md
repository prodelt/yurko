# Yurko — notes for coding agents

The MCP server gives a model **access to legal sources**: legislation, court
decisions and public registries. How the data is used in a task is decided by
the model, not by the server. New tools add access to source data only; the
lawyer's process (drafting, verification, court simulation) lives in the skill
under `skill/` (`/yurko`): prose without scripts or hooks, terms in `CONTEXT.md`.

## Gates

A change is ready when all four exit with code 0:

```bash
.venv/Scripts/python.exe -m pytest -q          # .venv/bin/python on Linux/macOS
.venv/Scripts/python.exe -m black --check .
uvx ruff@0.15.10 check .
.venv/Scripts/python.exe -m mypy server.py server_env.py core registries search storage sources
```

- For Cyrillic output on Windows set `PYTHONIOENCODING=utf-8`.
- Code lives in packages (`core/`, `registries/`, `search/`, `storage/`,
  `sources/`); a new package goes into `pyproject.toml` → `packages`, or the
  installed wheel fails on import.

## Language

Reply to the user in the user's language. Comments follow the language of the
surrounding code.

## Boundaries

- Case materials never go into the repository, a commit or the network: the
  server takes no files or paths, and only public identifiers (act numbers,
  case numbers, ECLI, CELEX) leave the machine.
