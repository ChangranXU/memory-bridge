# Vendoring: TencentDB-Agent-Memory

## Acquisition (always a fresh clone)

The upstream tree under `src/TencentDB-Agent-Memory/` is a gitignored,
never-committed clone of
[github.com/TencentCloud/TencentDB-Agent-Memory](https://github.com/TencentCloud/TencentDB-Agent-Memory),
pinned to the commit the design was verified against:

```bash
cd integration/tencentdb/src
git clone https://github.com/TencentCloud/TencentDB-Agent-Memory.git
git -C TencentDB-Agent-Memory checkout 97f9465
```

There is no parent-workspace copy of this checkout (unlike CURE) — the clone
is the only acquisition path.

## Purpose

1. **Development-time API reference**: every wire claim in the bridge (route
   paths, zod caps, envelope semantics, pipeline thresholds, config schema)
   was verified against this tree.
2. **Fallback build** — the reproducibility anchor: when the published image
   `agentmemory/memory-core:1.0.1-beta.1` cannot be pulled or has drifted
   from the verified commit, build it locally:

   ```bash
   docker build -f integration/tencentdb/src/TencentDB-Agent-Memory/MemoryCore/Dockerfile \
     -t agentmemory/memory-core:1.0.1-beta.1 \
     integration/tencentdb/src/TencentDB-Agent-Memory/MemoryCore
   ```

   The image tag and the clone commit are versioned independently upstream
   (the tree names no image tag for this release — the string
   `1.0.1-beta.1` appears only as SDK package versions, and the repo
   CHANGELOG tops out at `2.0.1-beta.1`), so this build is what makes the
   arm reproducible whenever image↔commit drift matters. `latest` is a
   moving alias and must not be used.

## Boundary

The bridge **never imports** the vendored tree: MemoryCore is TypeScript
plus a Node server; the Python bridge talks to its REST API over httpx (no
vendored Python SDK dependency — the tree does ship Python packages, which
is exactly why the integration's `pyproject.toml` pins discovery with
`[tool.setuptools.packages.find] where = ["src"], include =
["tencentdb_bridge*"]`). Nothing under `src/TencentDB-Agent-Memory/` is
ever committed to GitHub.
