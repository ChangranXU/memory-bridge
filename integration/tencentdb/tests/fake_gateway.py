"""The scripted fake gateway client and shared constants (unique module
name: safe under pytest's prepend import mode alongside sibling suites)."""

from tencentdb_bridge.client import utc_now_iso


SUBMIT_COMMAND = "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\necho done"

SCENE_READ_COMMAND = (
    "curl -sS -X POST http://host.docker.internal:8420/v3/scenario/read "
    "-H 'Authorization: Bearer local' -H 'x-tdai-service-id: default' "
    "-H 'Content-Type: application/json' "
    "-d '{\"team_id\":\"minisweagent\",\"agent_id\":\"memory-bridge\","
    "\"user_id\":\"u1\",\"path\":\"scenes/debugging.md\"}'"
)

CONVO_SEARCH_COMMAND = (
    "curl -sS -X POST http://host.docker.internal:8420/v3/conversation/search "
    "-H 'Authorization: Bearer local' -H 'x-tdai-service-id: default' "
    "-H 'Content-Type: application/json' "
    "-d '{\"team_id\":\"minisweagent\",\"agent_id\":\"memory-bridge\","
    "\"user_id\":\"u1\",\"task_id\":\"pydata__xarray\",\"query\":\"exact failing command\",\"limit\":5}'"
)


# ---------------------------------------------------------------------------
# Scripted fake gateway client (the backend's _make_client seam)
# ---------------------------------------------------------------------------
def _api_error(status_code, reason):
    from tencentdb_bridge.client import TencentDBApiError

    return TencentDBApiError(status_code, reason)


class FakeGatewayClient:
    """Offline duck-type stand-in for TencentDBClient.

    Models the gateway surfaces the backend/endpoint use: chunked
    conversation/add (optionally auto-producing watermark rows the way the
    server-side pipeline would), a scriptable L1 idle poll, watermark
    pagination over ``updated_time``, and the null-field 200 answers of
    scenario/core reads. ``wait_l1_idle`` is replaced wholesale (no real
    sleeping): each call pops one scripted answer, default idle.
    """

    def __init__(self):
        self.add_calls: list[dict] = []
        self.search_calls: list[dict] = []
        self.query_calls: list[dict] = []
        self.count_calls = 0
        self.count_task_ids: list = []
        self.scene_ls_calls = 0
        self.core_read_calls = 0
        self.drain_calls = 0
        self.drain_budgets: list[float] = []  # the budget each wait_l1_idle got
        self.closed = False
        # L1 store: rows carry updated_at ISO strings; the watermark query
        # filters on them (the real handler filters updated_time).
        self.rows: list[dict] = []
        self.auto_produce = True
        self.next_version = 0
        self.add_error: Exception | None = None
        self.search_error: Exception | None = None
        self.query_error: Exception | None = None
        self.update_error: Exception | None = None
        self.idle_answers: list[bool] = []  # popped per wait_l1_idle call
        self.search_hits: list[dict] = []
        self.scene_entries: list[dict] = []
        self.persona: dict = {}
        self.deleted: list[str] = []
        self.update_calls: list[dict] = []
        self.delete_calls: list[dict] = []

    # -- health / pipeline ------------------------------------------------
    def health(self):
        return {"status": "ok"}

    def l1_idle(self, *, timeout=None):
        return True

    def wait_l1_idle(self, budget, interval, *, timeout=None):
        self.drain_calls += 1
        self.drain_budgets.append(budget)
        return self.idle_answers.pop(0) if self.idle_answers else True

    # -- L0 ----------------------------------------------------------------
    def conversation_add(self, messages, *, team_id, agent_id, user_id, session_id, task_id=None, timeout=None):
        self.add_calls.append(
            {
                "messages": [dict(m) for m in messages],
                "team_id": team_id,
                "agent_id": agent_id,
                "user_id": user_id,
                "session_id": session_id,
                "task_id": task_id,
                "timeout": timeout,
            }
        )
        if self.add_error is not None:
            raise self.add_error
        if self.auto_produce:
            for message in messages:
                if message.get("role") != "user":
                    continue
                memory_id = f"a{len(self.rows) + 1}"
                self.rows.append(
                    {
                        "id": memory_id,
                        "type": "atomic",
                        "content": f"fact: {str(message.get('content'))[:40]}",
                        "version": self.next_version,
                        "created_at": utc_now_iso(),
                        "updated_at": utc_now_iso(),
                        "task_id": task_id,
                    }
                )
        return {"accepted_ids": [f"msg-{i}" for i in range(len(messages))], "total_count": len(messages)}

    # -- L1 ----------------------------------------------------------------
    def atomic_search(self, query, *, limit, team_id, agent_id, user_id, task_id=None, timeout=None):
        self.search_calls.append(
            {"query": query, "limit": limit, "team_id": team_id, "agent_id": agent_id,
             "user_id": user_id, "task_id": task_id, "timeout": timeout}
        )
        if self.search_error is not None:
            raise self.search_error
        # Pass-through like the real client: the backend's own id filter is
        # what the suite pins, not a stricter fake.
        return list(self.search_hits)[:limit]

    def atomic_query(self, *, team_id, agent_id, user_id, time_start=None, task_id=None, page_limit=5000, timeout=None):
        self.query_calls.append(
            {"time_start": time_start, "task_id": task_id, "user_id": user_id}
        )
        if self.query_error is not None:
            raise self.query_error
        # Both filters mirror the real handler: time_start on updated_time,
        # task_id strict-equality (untagged rows never match a task filter).
        return [
            row
            for row in self.rows
            if (task_id is None or row.get("task_id") == task_id)
            and (time_start is None or row.get("updated_at", "") >= time_start)
        ][:page_limit]

    def atomic_count(self, *, team_id, agent_id, user_id, task_id=None, timeout=None):
        self.count_calls += 1
        self.count_task_ids.append(task_id)
        return len([row for row in self.rows if task_id is None or row.get("task_id") == task_id])

    def atomic_update(self, memory_id, *, content, background=None, team_id, agent_id, user_id, timeout=None):
        self.update_calls.append(
            {"id": memory_id, "content": content, "background": background,
             "team_id": team_id, "agent_id": agent_id, "user_id": user_id}
        )
        if self.update_error is not None:
            raise self.update_error
        row = next((row for row in self.rows if row["id"] == memory_id), None)
        if row is None:
            raise _api_error(404, "memory not found")
        row["content"] = content
        return {"id": memory_id, "version": f"v{row['version'] + 1}", "updated_at": utc_now_iso()}

    def atomic_delete(self, ids, *, team_id, agent_id, user_id, timeout=None):
        self.delete_calls.append({"ids": list(ids), "team_id": team_id, "agent_id": agent_id, "user_id": user_id})
        deleted = 0
        for memory_id in ids:
            row = next((row for row in self.rows if row["id"] == memory_id), None)
            if row is not None:
                self.rows.remove(row)
                self.deleted.append(memory_id)
                deleted += 1
        return deleted

    # -- L2/L3 ---------------------------------------------------------------
    def scenario_ls(self, *, team_id, agent_id, user_id, timeout=None):
        self.scene_ls_calls += 1
        return [dict(entry) for entry in self.scene_entries]

    def scenario_read(self, path, *, team_id, agent_id, user_id, timeout=None):
        entry = next((e for e in self.scene_entries if e.get("path") == path), None)
        if entry is None:
            return {"path": path, "content": None, "created_at": None, "updated_at": None}
        return {"path": path, "content": f"# {path}\nbody", "version": entry.get("version", 1)}

    def core_read(self, *, team_id, agent_id, user_id, timeout=None):
        self.core_read_calls += 1
        return dict(self.persona)

    def close(self):
        self.closed = True

