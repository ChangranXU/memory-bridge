"""Platform mode: the hosted mem0 API (https://api.mem0.ai) as a Mem0Store.

A thin adapter over ``Mem0PlatformClient`` (the v3 add/search/get-all plus v1
by-id CRUD client): the poll budget rides the constructor so ``add`` keeps the
uniform store signature, and ``get_all`` adds the pagination loop the raw
client deliberately leaves to the caller.
"""

from mem0_bridge.client import Mem0ApiError, Mem0PlatformClient, _shape_of
from mem0_bridge.stores import Receipt


class PlatformStore:
    def __init__(self, *, api_key: str, base_url: str, poll_budget: float, poll_interval: float):
        self._client = Mem0PlatformClient(api_key=api_key, base_url=base_url)
        self._poll_budget = poll_budget
        self._poll_interval = poll_interval

    def health(self) -> dict:
        return self._client.ping()

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
        return self._client.add(
            messages=messages,
            user_id=user_id,
            run_id=run_id,
            infer=infer,
            metadata=metadata,
            custom_instructions=guidelines,
            poll_budget=self._poll_budget,
            poll_interval=self._poll_interval,
        )

    def search(
        self,
        *,
        query: str,
        user_id: str,
        top_k: int,
        threshold: float,
        timeout: float | None = None,
    ) -> list[dict]:
        return self._client.search(query=query, user_id=user_id, top_k=top_k, threshold=threshold, timeout=timeout)

    def get(self, memory_id: str) -> dict:
        return self._client.get(memory_id)

    def get_all(self, *, user_id: str, limit: int) -> list[dict]:
        """Paginate the v3 envelope to exhaustion (or ``limit`` rows).

        Termination rides the envelope's own ``next`` field (null = last
        page), pinned against the live API; an empty page also ends the walk
        so a drifting ``next`` can never spin the loop forever. The page size
        stays CONSTANT across the walk: DRF computes a page's offset as
        (page-1)*page_size, so shrinking the size on the final page would
        re-pick earlier rows; the trailing slice trims the overage instead.

        Fails closed on a drifted envelope (a page without the ``results``
        list), same discipline as add/search: coercing it to [] would
        silently truncate the final dump with no counter moving.
        """
        rows: list[dict] = []
        page = 1
        while len(rows) < limit:
            envelope = self._client.get_all(user_id=user_id, page_size=100, page=page)
            results = envelope.get("results")
            if not isinstance(results, list):
                raise Mem0ApiError(502, f"mem0 platform get-all returned an unrecognizable response: {_shape_of(envelope)}")
            batch = [item for item in results if isinstance(item, dict)]
            rows.extend(batch)
            if not envelope.get("next") or not batch:
                break
            page += 1
        return rows[:limit]

    def update(self, memory_id: str, *, text: str | None = None, metadata: dict | None = None) -> dict:
        return self._client.update(memory_id, text=text, metadata=metadata)

    def delete(self, memory_id: str) -> dict:
        return self._client.delete(memory_id)

    def close(self) -> None:
        self._client.close()
