"""Library mode: the in-process mem0ai engine (``from mem0 import Memory``).

The engine runs inside the bridge process, pointed at the provider upstream —
extraction traffic is never recorded through the trajectory proxy (the mem0
treatment in every mode). Store paths live under the run root (qdrant dir +
history.db), so a fresh run root is a fresh store and a resume reopens the
same one.

The ``mem0ai`` SDK is imported lazily: only the opt-in ``mem0-library``
dependency group installs it, and the default shared env must stay mem0ai-free
(the litellm conflict posture). Construction (``Memory.from_config``)
validates the wiring — the local qdrant store is built on the spot, so a bad
path or dims fails the backend start; credentials only build client objects,
so a bad key surfaces at the first add (contained, retried via the retained
batch).

Timeout semantics: the store accepts the protocol's ``timeout`` and IGNORES it
— the shared stance bounds only network calls (``shared_bridge/config.py``),
and mem0's llm/embedder configs carry no timeout field (the openai package
reads no ``OPENAI_TIMEOUT`` env either), so the effective network bound is the
openai SDK default. No future-wrap: it would leak a live thread per timeout
and cannot interrupt the local lemmatize/BM25 CPU work regardless.
"""

import os

from mem0_bridge.client import Mem0ApiError, _results_of, _shape_of
from mem0_bridge.stores import Receipt


def _openai_v1_root(url: str) -> str:
    """The OpenAI-compatible root form: mem0's openai LLM/embedder clients
    append /chat/completions or /embeddings to the configured base_url."""
    root = url.rstrip("/")
    return root if root.endswith("/v1") else f"{root}/v1"


class LibraryStore:
    def __init__(self, settings: dict, *, memory=None):
        # ``memory`` is the test seam: a fake Memory object, so the offline
        # suite never imports mem0ai (the default env must stay SDK-free).
        os.environ.setdefault("MEM0_TELEMETRY", "false")  # posthog phone-home hygiene (the driver exports it too)
        self._memory = memory if memory is not None else self._make_memory(settings)

    @staticmethod
    def _make_memory(settings: dict):
        try:
            from mem0 import Memory
        except ImportError:
            # Fail closed with guidance: the default shared env is deliberately
            # mem0ai-free (the opt-in mem0-library group installs it), and a
            # bare `import mem0` can even resolve to a stray namespace package
            # (pytest path insertion) — both are the same missing-SDK state.
            raise ImportError(
                "mem0 library mode needs the mem0ai SDK: run via `uv run --group mem0-library`"
            ) from None
        return Memory.from_config(LibraryStore._config_dict(settings))

    @staticmethod
    def _config_dict(settings: dict) -> dict:
        store_dir = os.path.join(settings["run_root"], "mem0")
        # The bridge's own directory — create it explicitly (the server mode's
        # /app/history makedirs trap does not apply here, but a clear path is
        # cheaper than relying on engine internals).
        os.makedirs(store_dir, exist_ok=True)
        dims = settings["embedding_dimensions"]
        return {
            "version": "v1.1",
            "llm": {
                "provider": "openai",
                "config": {
                    "model": settings["llm_model"],
                    "api_key": settings["llm_api_key"],
                    "openai_base_url": _openai_v1_root(settings["llm_base_url"]),
                    # 32000, not the engine's 2000 default: reasoning-hybrid
                    # roster models (deepseek-v4-flash et al.) burn a small
                    # budget entirely on thinking — finish_reason=length with
                    # 0 output chars, and the extraction stores NOTHING while
                    # answering fine (the trap the tencentdb gateway yaml
                    # documents; 32k matches its shipped deepseek value).
                    "max_tokens": 32000,
                },
            },
            "embedder": {
                "provider": "openai",
                "config": {
                    "model": settings["embedding_model"],
                    "api_key": settings["embedding_api_key"],
                    "openai_base_url": _openai_v1_root(settings["embedding_base_url"]),
                    "embedding_dims": dims,
                },
            },
            # Local qdrant under the run root; the fixed collection name is
            # already isolated by the per-run path.
            "vector_store": {
                "provider": "qdrant",
                "config": {"collection_name": "mem0", "path": os.path.join(store_dir, "qdrant"), "embedding_model_dims": dims},
            },
            "history_db_path": os.path.join(store_dir, "history.db"),
        }

    def health(self) -> dict:
        # Construction already validated the config; report the engine version
        # for the record (best-effort — absent under the fake-Memory seam).
        try:
            from importlib.metadata import version as _pkg_version

            engine = _pkg_version("mem0ai")
        except Exception:
            engine = "unknown"
        return {"status": "ok", "engine": engine}

    def add(
        self,
        *,
        messages: list[dict],
        user_id: str,
        run_id: str | None = None,
        infer: bool = True,
        metadata: dict | None = None,
        guidelines: str | None = None,
    ) -> list[Receipt]:
        # ``prompt`` lands in the same advisory slot as config-level custom
        # instructions (custom_instr = prompt or self.custom_instructions) —
        # identical semantics to the platform's custom_instructions.
        response = self._memory.add(
            messages,
            user_id=user_id,
            run_id=run_id,
            infer=infer,
            metadata=metadata,
            prompt=guidelines.strip() if guidelines and guidelines.strip() else None,
        )
        # Every engine path answers {"results": [...]} (verified against the
        # pinned engine — no bare-list path exists). Fail closed on anything
        # else: coercing drift to [] would report "success, zero memories"
        # and the backend would clear the retained batch — silent message
        # loss. An empty results list stays a legitimate no-op add.
        if not isinstance(response, dict) or not isinstance(response.get("results"), list):
            raise Mem0ApiError(502, f"mem0 library add returned an unrecognizable response: {_shape_of(response)}")
        return _results_of(response)

    def search(
        self,
        *,
        query: str,
        user_id: str,
        top_k: int,
        threshold: float,
        timeout: float | None = None,
    ) -> list[dict]:
        # threshold is always sent explicitly (the OSS default 0.1 drifts); the
        # entity filter is hard-required by the engine. timeout is ignored by
        # design (module docstring). Fail closed on a shapeless response, same
        # as add: coercing drift to [] reads as "no memories", and the recall
        # path CACHES an empty answer as authoritative — silent blindness.
        response = self._memory.search(query=query, filters={"user_id": user_id}, top_k=top_k, threshold=threshold)
        if not isinstance(response, dict) or not isinstance(response.get("results"), list):
            raise Mem0ApiError(502, f"mem0 library search returned an unrecognizable response: {_shape_of(response)}")
        return [item for item in response["results"] if isinstance(item, dict)]

    def get(self, memory_id: str) -> dict:
        row = self._memory.get(memory_id)
        if row is None:
            raise Mem0ApiError(404, f"memory {memory_id} not found")
        if not isinstance(row, dict):
            # Drift must not masquerade as an empty row: the endpoint's
            # ownership check would misreport it as a plain 404.
            raise Mem0ApiError(502, f"mem0 library get returned an unrecognizable response: {_shape_of(row)}")
        return row

    def get_all(self, *, user_id: str, limit: int) -> list[dict]:
        # The entity filter is hard-required and the engine default top_k=20
        # would silently truncate the dump — always explicit.
        response = self._memory.get_all(filters={"user_id": user_id}, top_k=limit)
        results = response.get("results") if isinstance(response, dict) else None
        if not isinstance(results, list):
            # Fail closed like add/search: a drifted envelope coerced to []
            # would silently truncate the final dump with no counter moving.
            raise Mem0ApiError(502, f"mem0 library get-all returned an unrecognizable response: {_shape_of(response)}")
        return [item for item in results if isinstance(item, dict)][:limit]

    def update(self, memory_id: str, *, text: str | None = None, metadata: dict | None = None) -> dict:
        try:
            self._memory.update(memory_id, text=text, metadata=metadata)
        except ValueError as e:
            raise _maybe_not_found(e) from e
        # The engine answers a bare {"message": ...} — the echo the contract
        # needs is a follow-up read.
        return self.get(memory_id)

    def delete(self, memory_id: str) -> dict:
        try:
            return self._memory.delete(memory_id)
        except ValueError as e:
            raise _maybe_not_found(e) from e

    def close(self) -> None:
        # Deterministic release, not GC-timed: the engine has no close hook,
        # but the batch runner keeps every episode of an arm in ONE process —
        # qdrant-local holds an exclusive flock on its storage folder (a
        # later Memory over the same run root fails to construct while an
        # earlier client is still alive), and the sqlite history manager
        # keeps a persistent connection. The base nulls the store right
        # after this returns, so a closed engine is never used again.
        self._memory.vector_store.client.close()
        self._memory.db.connection.close()


def _maybe_not_found(error: ValueError) -> Mem0ApiError:
    """The engine signals a missing id by ValueError ("... not found"); map it
    to the protocol's 404 convention, keep any other ValueError a 400."""
    message = str(error)
    status = 404 if "not found" in message.lower() else 400
    return Mem0ApiError(status, message)
