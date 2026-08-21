# Development

This repo is a `uv` workspace with two members: `shared` and `ingestion`.

## Setup

```bash
uv sync --all-packages
```

Plain `uv sync` only installs the root project — it will **not** install the
`shared` and `ingestion` workspace member packages into the venv, so their
imports (`ingestion.*`, `advisor_shared.*`) will fail. Always use
`--all-packages`.

## Corporate networks

If PyPI is blocked or slow on your network, set `UV_INDEX_URL` to point at
your corporate PyPI proxy before running `uv sync`.
