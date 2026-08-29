# Vendoring: mem0 (OSS server mode)

## Acquisition (always a fresh clone)

The upstream tree under `vendor/mem0/` is a gitignored, never-committed clone of
[github.com/mem0ai/mem0](https://github.com/mem0ai/mem0), pinned to the commit
the server-mode design was verified against:

```bash
cd integration/mem0/vendor
git clone https://github.com/mem0ai/mem0.git
git -C mem0 checkout fdfb763d6e5e5509bdb35d4ddc9ca8003f6af009
```

The pin is `main` @ `fdfb763` (2026-08-27), FIVE commits past the LIGHTWEIGHT
tag `v2.0.19` = `dc82354` (2026-08-24); both commits declare version 2.0.19.
Verify the pin with `git cat-file -t v2.0.19` (→ `commit`) / `git rev-parse`,
never `git describe` without `--match 'v*'` (the tag is lightweight, so plain
`git describe` ignores it). The SHA is the pin target because it pins the
ROUTES; upstream releases ~weekly, so pins age fast.

## Purpose

1. **Development-time API reference**: every server-mode wire claim in
   `mem0_bridge/stores/server.py` (unprefixed routes, slash strictness, sync
   add, the `prompt` guidelines field, null-GET missing-id semantics, the
   `/auth/setup-status` readiness probe) was verified against this tree.
2. **Docker build context for the OSS server** (server mode): there is no
   usable published image — Docker Hub's `mem0/mem0-api-server:latest` is
   stale (pushed 2025-09-10, predates the v2 auth server) AND its only
   runnable variant is `linux/arm64` (amd64 hosts cannot pull it at all). The
   driver builds from this tree's `server/Dockerfile`.

## The two pins are separate

The clone pins the ROUTES, not the engine that runs: `server/requirements.txt`
carries `mem0ai>=0.1.48` (an unpinned lower bound), so a naive build floats
with PyPI latest. The driver rewrites that line to `mem0ai==2.0.19` in a
patched build-context copy at build time (the clone itself stays pristine) and
tags the image `mem0-oss-server:2.0.19-<routes-pin>` (the clone's HEAD short
hash): the tag keys on BOTH pins, so a ROUTES re-pin invalidates it and
`build_mem0_server_image`'s `docker image inspect` short-circuit rebuilds
instead of silently reusing a stale image. Both pins are recorded together in
the run root's `memory-arm.log`. The same staging step switches
`psycopg>=3.2.8` to `psycopg[binary]>=3.2.8`: the Dockerfile's
`python:3.12-slim` base ships no libpq, so the pure wheel dies at the first
DB connect ("libpq library not found") — same version range, bundled driver
library (verified by probing the base image).

## Stale-docs warning

The checkout's own `server/AGENTS.md` and `server/CLAUDE.md` describe a Neo4j
graph-store container in the stack (ports 8474/8687, "Neo4j 5.x with APOC") —
unrefreshed after graph memory left OSS. The live compose/Dockerfile ship no
Neo4j service and `server/main.py`'s `DEFAULT_CONFIG` carries no
`graph_store` key. Trust the compose/Dockerfile over those files' stack
tables.

## Boundary

The clone lives under `vendor/`, NOT `src/`: at `src/mem0` the editable
install's `.pth` (which adds `src/` to `sys.path`) made the clone importable
as a namespace package named `mem0`, shadowing the real SDK's absence with a
confusing half-import — `vendor/` keeps it off every import path.

The bridge **never imports** the vendored tree, and the tree never enters the
dependency graph (the engine comes from PyPI via the build-time pin or the
opt-in `mem0-library` group). The integration's `pyproject.toml` still pins
discovery with `[tool.setuptools.packages.find] where = ["src"], include =
["mem0_bridge*"]` (only the bridge package is ours — the same guard the
tencentdb integration carries). Nothing under `vendor/mem0/` is ever
committed to GitHub.
