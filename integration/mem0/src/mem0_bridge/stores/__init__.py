"""The mem0 store abstraction: one protocol below BOTH retrieval surfaces.

``Mem0Backend`` (the arm) and ``Mem0Endpoint`` (the standardized contract)
both consume a ``Mem0Store``, so retrieval is implemented exactly twice over
one native call per mode and the two surfaces cannot drift. ``open_store``
dispatches on the configured mode with a lazy per-mode import — the default
shared env is mem0ai-free, so the library store's module (which needs the
SDK) is only ever imported in library mode.

Shared conventions every implementation honors:

- Writes are synchronous: ``add`` returns only after persistence.
- ``add`` fails CLOSED on a drifted success body (a 200 that is not the
  mode's ``results`` receipt list raises 502, never coerces to "stored
  nothing"): the backend clears its retained batch on any non-raising add,
  so a silent empty would lose messages. An empty list stays a legitimate
  no-op extraction.
- ``search`` fails closed the same way (a success body without the
  ``results`` list raises 502, never returns an empty answer): the recall
  path caches an empty search as authoritative until the next dirty tick,
  so a drifted envelope would silently blind recall with no counter moving.
- ``user_id`` is the sole retrieval-isolation boundary (the bridge passes
  ``user_id`` only, never ``agent_id`` — the platform's attribution splitting
  would stamp assistant-message facts with ``agent_id`` instead, and a
  ``user_id``-filtered search would miss them).
- ``threshold`` is always sent explicitly on search — every surface's
  implicit default drifts (platform v2 0.3 / v3 0.1; OSS 0.1). Its MEANING is
  per surface: platform 0.0 disables the cutoff; OSS 0.0 is a minimal gate on
  the raw semantic score before the hybrid combine (a floor, not a switch).
- ``get`` on a missing id raises ``Mem0ApiError(404, ...)`` — never a null
  hit — so the endpoint contract's 404 rule holds on every surface (the OSS
  server's ``GET /memories/{id}`` answers 200 ``null`` for unknown ids; the
  server store maps it). A drifted non-dict 200 raises 502, never an empty
  row: the endpoint's ownership check would misreport an empty row as that
  same 404.
- ``get_all`` fails closed on a drifted envelope the same way (the dump a
  coerced ``[]`` would silently truncate is diagnostic, so the error surfaces
  through the base's finalize containment as a counted backend error). It
  returns flat native rows up to ``limit``: the platform paginates its
  envelope to exhaustion behind the store, the library engine takes the limit
  as ``top_k`` directly, and the OSS server has NO pagination — it answers
  one clamped page (hard server-side cap of 1000), so a run root past 1000
  memories truncates the final dump there.
"""

from typing import Protocol, TypedDict


class Receipt(TypedDict):
    """One normalized add receipt (post client-side flattening)."""

    id: str | None
    memory: str | None
    event: str | None


class Mem0Store(Protocol):
    """The mem0 operations the bridge needs, one native call each per mode."""

    def health(self) -> dict:
        """Liveness/credential probe; raises when the store is unusable."""
        ...

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
        """Add messages synchronously. ``guidelines`` is the per-mode advisory
        extraction-instructions channel (platform ``custom_instructions``, OSS
        ``prompt``) — all verified to land in the same advisory slot."""
        ...

    def search(
        self,
        *,
        query: str,
        user_id: str,
        top_k: int,
        threshold: float,
        timeout: float | None = None,
    ) -> list[dict]:
        """The one native search both retrieval surfaces share. ``timeout``
        bounds the call only where the search is a network call; an in-process
        store ignores it (a local deadline cannot interrupt CPU work)."""
        ...

    def get(self, memory_id: str) -> dict:
        """One memory row; missing id raises ``Mem0ApiError(404, ...)``."""
        ...

    def get_all(self, *, user_id: str, limit: int) -> list[dict]:
        """Every row under ``user_id`` (capped at ``limit``); the platform
        paginates to exhaustion behind the store, the OSS server answers one
        clamped page (hard cap 1000). Fails closed on a drifted envelope."""
        ...

    def update(self, memory_id: str, *, text: str | None = None, metadata: dict | None = None) -> dict: ...

    def delete(self, memory_id: str) -> dict: ...

    def close(self) -> None: ...


def open_store(mode: str, settings: dict) -> Mem0Store:
    """Construct the mode's store from the backend's resolved settings.

    The per-mode imports stay inside the branches: the library store needs the
    ``mem0ai`` SDK, which only the opt-in ``mem0-library`` dependency group
    installs — importing it eagerly would break the default shared env.
    """
    if mode == "platform":
        from mem0_bridge.stores.platform import PlatformStore

        return PlatformStore(
            api_key=settings["api_key"],
            base_url=settings["base_url"],
            poll_budget=settings["poll_budget"],
            poll_interval=settings["poll_interval"],
        )
    if mode == "server":
        from mem0_bridge.stores.server import ServerStore

        return ServerStore(server_url=settings["server_url"], server_api_key=settings["server_api_key"])
    if mode == "library":
        from mem0_bridge.stores.library import LibraryStore

        return LibraryStore(settings)
    raise ValueError(f"unknown mem0 mode: {mode!r}")
