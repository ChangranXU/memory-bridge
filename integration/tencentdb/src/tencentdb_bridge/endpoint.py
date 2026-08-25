"""Standardized-endpoint adapter over the MemoryCore gateway.

Implements the shared ``MemoryEndpoint`` contract (add / search / update /
delete, synchronous writes, ``user_id`` the sole retrieval-isolation
boundary). Request ``user_id`` maps onto the gateway's team/agent/user
triple with no ``task_id``: the endpoint keeps the contract's user-wide
semantics — its search sees every row of the user. The arm's repo narrowing
stays arm-internal (the same breadth carve-out as the other integrations'
applicability layers).

One deliberate deviation from the contract's echo rule: ``add`` does NOT
reuse the request's ``session_id`` — it mints a fresh one per add and
returns that. A fresh session is load-bearing for the contract's harder
rule (success only after immediately searchable): the pipeline's warmup
threshold starts at 1 per session, so a fresh session's add always enqueues
its L1 task synchronously, while a reused session's add can fall
sub-threshold and leave the memories unsearchable behind an armed 30 s
idle timer. Callers that need correlation get the minted session id in the
response.

``search`` is deliberately the L1 atomic layer alone (one native
``atomic/search``, native ranking — the arm's measured surface): the
contract's records are the rows ``add`` returns and ``update``/``delete``
address by id. The arm's L0 excerpts, L3 persona, and L2 scene index are
episode-bound recall augmentations with no CRUD identity (updating a
``msg-*`` excerpt or the persona pseudo-id would 404) and no endpoint-side
episode window to self-exclude against, so they stay arm-internal like the
repo narrowing.

The minted session id also rides as the add's ``task_id``: it is the only
per-row marker the query wire exposes (rows carry no session id), so the
post-drain resolve filters on it and ``memory_ids`` names exactly what THIS
add produced — never a concurrent add's rows landing inside the same
watermark window for the same user. The tag changes no retrieval surface:
search without a task filter keeps returning tagged rows user-wide, exactly
as untagged ones.

Three request validations come from upstream pipeline facts, all answered with
the contract's 400 rather than a silent no-op: the conversation schema's role
enum is ``user``/``assistant`` only (anything else is a gateway 400, which
must not surface as a 500), an add carrying no ``user`` round never
extracts at all (the gateway notifies the pipeline only on user rounds, and
the per-add fresh session means no later add can pick the rows up) — a
"success" there would promise searchability that can never arrive — and the
wire's 8192-char content cap (shared by add messages and update text; counted
in UTF-16 code units — the zod schema's String.length unit — so the host-side
check measures the same length the gateway rejects on), a
contract-legal request the gateway must reject, answered 400 before any
write rather than relayed as a 500.
"""

from __future__ import annotations

import time
from uuid import uuid4

from shared_bridge.annotate import normalize_score
from shared_bridge.endpoint import (
    AddRequest,
    AddResponse,
    DeleteResponse,
    MemoryEndpoint,
    MemoryEndpointError,
    MemoryRecord,
    SearchRequest,
    SearchResponse,
    UpdateRequest,
    UpdateResponse,
)

from tencentdb_bridge.client import (
    AGENT_ID,
    IDLE_WAIT_MARGIN_SECONDS,
    MESSAGE_CONTENT_MAX_CHARS,
    SEARCH_LIMIT_MAX,
    TEAM_ID,
    WATERMARK_SKEW_SECONDS,
    TencentDBApiError,
    TencentDBClient,
    utc_now_iso,
    utf16_units,
)

# One L1 cycle consumes at most 10 L0 rows (upstream pipeline-factory.ts
# L1_BATCH_PROCESS; the 2N=20 over-fetch is backlog detection only). A cycle
# ending with a 1-9-row tail defers it to the L1 idle timer, which
# /v2/pipeline/status never exposes — the drain below must wait it out.
_L1_CYCLE_ROWS = 10


class TencentDBEndpoint(MemoryEndpoint):
    """MemoryCore exposed through the standardized memory contract."""

    def __init__(
        self,
        client: TencentDBClient,
        default_user_id: str = "minisweagent",
        *,
        # The drain must dominate one full L1 cycle: the extraction LLM call
        # caps at 180 s upstream (hardcoded in l1-extractor, independent of
        # the yaml's llm.timeoutMs) plus the vector lane's per-memory embeds,
        # so a smaller budget reports a 500 for a write that already persisted
        # (and a caller retry would re-feed the pipeline wholesale). One add
        # can chain several cycles (upstream consumes at most
        # _L1_CYCLE_ROWS L0 rows per cycle), and each wait gets the full
        # budget — the arm's per-tick/finalize budget split in miniature.
        drain_budget: float = 300.0,
        drain_interval: float = 1.0,
        add_timeout: float = 300.0,
        # Must match the gateway's l1IdleTimeoutSeconds: an add over
        # _L1_CYCLE_ROWS messages can leave a 1-9-row tail whose only landing
        # mechanism is the idle timer, which the status poll never exposes —
        # the drain waits it out like the arm's finalize. The arm-side wait no
        # longer carries its own copy (the backend resolves the generated
        # gateway yaml at start); a standalone deployment passes the gateway's
        # configured value here — this parameter is NOT a second source for
        # the arm.
        l1_idle_timeout: float = 30.0,
    ):
        self._client = client
        self._default_user_id = default_user_id
        self._drain_budget = drain_budget
        self._drain_interval = drain_interval
        # The add's own client timeout, mirroring TencentDBConfig.add_timeout:
        # with the vector lane on, the gateway embeds every L0 message
        # sequentially inside the add, so a slow embedding provider can
        # stretch one add past any generic timeout — dying client-side would
        # report a failure for a write the gateway may still complete (and a
        # caller retry would re-feed the pipeline wholesale).
        self._add_timeout = add_timeout
        self._l1_idle_timeout = l1_idle_timeout

    # ------------------------------------------------------------------
    def add(self, request: AddRequest) -> AddResponse:
        if not request.infer:
            # The platform has no verbatim insert (there is no atomic/add
            # route at all; conversation/add always feeds the extraction
            # pipeline) — silently ignoring the flag would claim persistence
            # of something that was not stored.
            raise MemoryEndpointError(
                400, "infer=false is unsupported: the gateway has no verbatim insert, every add feeds extraction"
            )
        roles = {message.role for message in request.messages}
        if unsupported := sorted(roles - {"user", "assistant"}):
            # The conversation schema's role enum is user/assistant (anything
            # else is a gateway 400): a caller bug, not an integration
            # failure — answer the contract's 400. The backend's deliberate
            # system/tool fold is arm-internal (it feeds the round counter);
            # a generic ingest API must not silently rewrite roles.
            raise MemoryEndpointError(400, f"unsupported message role(s) {unsupported}: only user/assistant are accepted")
        if "user" not in roles:
            # The gateway notifies the pipeline only on role=="user" rounds,
            # and the fresh per-add session means no later add can pick these
            # rows up: the write would persist L0 yet NEVER become searchable
            # — not the honest-empty case below (the extractor saw the
            # conversation and produced nothing).
            raise MemoryEndpointError(400, "add carries no user round: the pipeline extracts only on user rounds")
        if over_cap := sum(utf16_units(message.content) > MESSAGE_CONTENT_MAX_CHARS for message in request.messages):
            # The wire's per-message content cap (the role pre-validation's
            # class), counted in the wire's own unit — UTF-16 code units, not
            # Python code points: a contract-legal request the gateway must
            # reject is a caller bug answered 400, never relayed as a 500.
            raise MemoryEndpointError(
                400, f"{over_cap} message(s) exceed the gateway's {MESSAGE_CONTENT_MAX_CHARS}-char content cap"
            )
        session_id = str(uuid4())  # fresh per add: see the module docstring's deviation note
        # The resolve filters on the gateway's updated_time clock: stamp with
        # a skew margin so a host clock running ahead of the container's
        # cannot strand this add's first rows (the per-add task tag makes the
        # margin free — no foreign row can match it).
        watermark = utc_now_iso(skew_seconds=WATERMARK_SKEW_SECONDS)
        try:
            self._client.conversation_add(
                [{"role": message.role, "content": message.content} for message in request.messages],
                team_id=TEAM_ID,
                agent_id=AGENT_ID,
                user_id=request.user_id,
                session_id=session_id,
                task_id=session_id,  # the per-add row marker the resolve below filters on
                timeout=self._add_timeout,
            )
            self._drain(len(request.messages))
            rows = self._client.atomic_query(
                team_id=TEAM_ID,
                agent_id=AGENT_ID,
                user_id=request.user_id,
                time_start=watermark,
                task_id=session_id,
            )
        except TencentDBApiError as e:
            raise MemoryEndpointError(500, f"tencentdb add failed: {e.reason}") from e
        memory_ids = [str(row["id"]) for row in rows if row.get("id")]
        # Empty is honest: the extractor may legitimately produce nothing.
        return AddResponse(
            success=True, request_id=request.request_id, user_id=request.user_id, session_id=session_id, memory_ids=memory_ids
        )

    def _drain(self, n_messages: int) -> None:
        """Wait for this add's L1 work to land. The threshold-triggered task
        is enqueued before conversation/add returns and the status's
        ``idle = queued==0 && running==0`` covers queued work, so one wait
        suffices for an add consumed in a single cycle (<= _L1_CYCLE_ROWS
        messages). A larger add can end a cycle with a 1-9-row tail that
        upstream defers to the L1 idle timer — invisible to the status poll
        — so mirror the arm's finalize drain: wait out the armed timer, then
        drain the timer-fired tail with a fresh budget."""
        if not self._client.wait_l1_idle(self._drain_budget, self._drain_interval):
            raise MemoryEndpointError(500, f"tencentdb add failed: L1 did not settle within {self._drain_budget:.0f}s")
        if n_messages > _L1_CYCLE_ROWS:
            self._sleep(self._l1_idle_timeout + IDLE_WAIT_MARGIN_SECONDS)
            if not self._client.wait_l1_idle(self._drain_budget, self._drain_interval):
                raise MemoryEndpointError(
                    500, f"tencentdb add failed: L1 tail did not land within {self._drain_budget:.0f}s post-idle drain budget"
                )

    def _sleep(self, seconds: float) -> None:
        """Test seam over the idle-timer wait (the backend's _sleep pattern)."""
        time.sleep(seconds)

    def search(self, request: SearchRequest) -> SearchResponse:
        try:
            hits = self._client.atomic_search(
                request.query,
                limit=min(SEARCH_LIMIT_MAX, request.top_k),
                team_id=TEAM_ID,
                agent_id=AGENT_ID,
                user_id=request.user_id,
            )
        except TencentDBApiError as e:
            raise MemoryEndpointError(500, f"tencentdb search failed: {e.reason}") from e
        records = []
        for hit in hits:
            memory_id = hit.get("id")
            content = hit.get("content")
            if not memory_id or not isinstance(content, str) or not content:
                continue
            records.append(
                MemoryRecord(
                    id=str(memory_id),
                    content=content,
                    score=normalize_score(hit.get("score")),
                    user_id=hit.get("user_id"),
                    created_at=hit.get("created_at"),
                    updated_at=hit.get("updated_at"),
                )
            )
        return SearchResponse(data=records[: request.top_k])

    def update(self, memory_id: str, request: UpdateRequest, *, user_id: str | None = None) -> UpdateResponse:
        if request.text is None or request.metadata is not None:
            # L1 rows carry only content (+ background); metadata has no
            # upstream counterpart, so a metadata-only update cannot be
            # honored, and a text+metadata update applied partially would
            # silently drop the metadata half — answer 400 rather than claim
            # a write that was not (fully) made.
            raise MemoryEndpointError(400, "update requires text alone: L1 rows carry no metadata")
        if utf16_units(request.text) > MESSAGE_CONTENT_MAX_CHARS:
            # The wire's content cap (the add path's pre-validation class),
            # counted in UTF-16 code units like the gateway's zod schema: a
            # gateway rejection of a contract-legal request is a caller
            # bug answered 400, never relayed as a 500.
            raise MemoryEndpointError(400, f"update text exceeds the gateway's {MESSAGE_CONTENT_MAX_CHARS}-char content cap")
        owner = user_id or self._default_user_id
        try:
            data = self._client.atomic_update(
                memory_id,
                content=request.text,
                team_id=TEAM_ID,
                agent_id=AGENT_ID,
                user_id=owner,
            )
        except TencentDBApiError as e:
            # Native 404 (unknown id) and 403 (isolation mismatch) both map
            # to the contract's 404 — isolation must look like absence.
            status = 404 if e.status_code in (403, 404) else 500
            raise MemoryEndpointError(status, f"tencentdb update failed: {e.reason}") from e
        return UpdateResponse(
            success=True,
            memory=MemoryRecord(
                id=str(data.get("id") or memory_id),
                content=request.text,
                user_id=owner,
                updated_at=data.get("updated_at"),
            ),
        )

    def delete(self, memory_id: str, *, user_id: str | None = None) -> DeleteResponse:
        owner = user_id or self._default_user_id
        try:
            # atomic/delete is batch (ids[] -> {deleted_count} only, no
            # per-id list); a single-element batch with deleted_count == 0
            # is exactly the isolation-mismatch-deletes-nothing behavior the
            # contract requires to look like a 404.
            deleted = self._client.atomic_delete(
                [memory_id], team_id=TEAM_ID, agent_id=AGENT_ID, user_id=owner
            )
        except TencentDBApiError as e:
            raise MemoryEndpointError(500, f"tencentdb delete failed: {e.reason}") from e
        if deleted == 0:
            raise MemoryEndpointError(404, f"memory {memory_id} not found for user {owner}")
        return DeleteResponse(success=True, memory_id=memory_id)
