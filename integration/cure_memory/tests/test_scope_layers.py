"""Two-layer applicability scoping under scope="run": repo-bound memories
(scope="project", project_id=<repo>) are retrievable only inside episodes of
their repository, while general memories (scope="user", project_id=NULL) flow
to every episode of the run. The layer is decided once at extraction and never
re-classified."""

import json
from types import SimpleNamespace

from conftest import ScriptedDecisionClient, approved_candidate, db_rows

from cure_memory_bridge.backend import CureMemoryBackend, _repo_of


def _extract_candidates(backend, fake_client, *candidates, step=1):
    """Record one message and run one extraction carrying the given candidates."""
    backend.record([{"role": "user", "content": "layer seed"}], step=step)
    fake_client.queue.append({"candidates": list(candidates), "deletions": [], "rejections": []})
    backend._extract(step)


# ---------------------------------------------------------------------------
# Repo identity (§1/§2)
# ---------------------------------------------------------------------------
def test_repo_of_strips_the_trailing_issue_number():
    assert _repo_of("pydata__xarray-2905") == "pydata__xarray"
    assert _repo_of("django__django-11099") == "django__django"
    # No trailing -<digits>: the full id stays its own project scope.
    assert _repo_of("i1") == "i1"
    assert _repo_of("test-instance") == "test-instance"
    assert _repo_of("owner__repo-") == "owner__repo-"


def test_project_id_layers_by_scope(tmp_path, make_backend):
    """scope="run" binds the repo key derived from the instance id;
    scope="instance" keeps the whole instance id (byte-identical to before)."""
    run_backend = make_backend(instance_id="pydata__xarray-2905", scope="run")
    assert run_backend._project_id() == "pydata__xarray"
    instance_backend = make_backend(instance_id="pydata__xarray-2905", scope="instance")
    assert instance_backend._project_id() == "pydata__xarray-2905"


# ---------------------------------------------------------------------------
# Extraction-time layer decision (§3)
# ---------------------------------------------------------------------------
def test_user_candidate_never_binds_a_project(tmp_path, make_backend, fake_client):
    """The load-bearing invariant: a scope="user" candidate ALWAYS lands with
    project_id=None even when the session passes a repo key — while the repo
    key still reaches the decision LLM as extraction context."""
    backend = make_backend(instance_id="pydata__xarray-2905", output_dir=str(tmp_path / "runs" / "a"), scope="run")
    backend.start()
    _extract_candidates(backend, fake_client, approved_candidate(999, "general_lesson", "general debugging lesson"))
    (row,) = backend._system.memory_search("minisweagent", query=None, review_status=None)
    assert row.scope == "user" and row.project_id is None
    assert fake_client.requests[0]["project_id"] == "pydata__xarray"
    backend.finalize()
    data = json.loads((tmp_path / "runs" / "a" / "memory.json").read_text())
    assert data["project_id"] == "pydata__xarray"


def test_missing_or_malformed_scope_fails_closed_to_project(tmp_path, make_backend, fake_client):
    """A missing or malformed scope must not silently land in the general
    layer: omitted, mis-cased, and bogus values normalize, and anything that
    is not exactly "user" fails closed to "project" (repo-bound is the safe
    default — a wrongly-project memory only fails to help, a wrongly-general
    one leaks)."""
    backend = make_backend(instance_id="pydata__xarray-2905", output_dir=str(tmp_path / "inst"), scope="run")
    backend.start()
    missing = approved_candidate(999, "k_missing", "v missing")
    del missing["scope"]
    _extract_candidates(
        backend,
        fake_client,
        missing,
        approved_candidate(999, "k_miscased", "v miscased", scope="Project"),
        approved_candidate(999, "k_bogus", "v bogus", scope="global"),
        approved_candidate(999, "k_upper_user", "v upper", scope="USER"),
    )
    rows = {row.key: row for row in backend._system.memory_search("minisweagent", query=None, review_status=None)}
    assert rows["k_missing"].scope == "project" and rows["k_missing"].project_id == "pydata__xarray"
    assert rows["k_miscased"].scope == "project" and rows["k_miscased"].project_id == "pydata__xarray"
    assert rows["k_bogus"].scope == "project" and rows["k_bogus"].project_id == "pydata__xarray"
    assert rows["k_upper_user"].scope == "user" and rows["k_upper_user"].project_id is None
    backend.finalize()


# ---------------------------------------------------------------------------
# The visibility lattice (§2)
# ---------------------------------------------------------------------------
def test_repo_bound_rows_never_recall_across_repos(tmp_path, make_backend, fake_client):
    """Repo A's project rows are invisible to repo B's episode; a same-repo
    episode sees them, with the cross-episode provenance suffix."""
    repo_a = make_backend(instance_id="pydata__xarray-2905", output_dir=str(tmp_path / "runs" / "a"), scope="run")
    repo_a.start()
    _extract_candidates(
        repo_a, fake_client, approved_candidate(999, "xarray_quirk", "xarray recallterm workaround", scope="project")
    )
    repo_a.finalize()
    assert db_rows(tmp_path / "runs" / "cure_memory.sqlite3", "SELECT scope, project_id FROM memories") == [
        ("project", "pydata__xarray")
    ]

    repo_b = make_backend(instance_id="django__django-11099", output_dir=str(tmp_path / "runs" / "b"), scope="run")
    repo_b.start()
    repo_b.set_task("recallterm task")
    assert repo_b.recall_context() is None
    repo_b.finalize()

    same_repo = make_backend(instance_id="pydata__xarray-3000", output_dir=str(tmp_path / "runs" / "c"), scope="run")
    same_repo.start()
    same_repo.set_task("recallterm task")
    recall = same_repo.recall_context()
    assert "- [fact:repo] xarray_quirk: xarray recallterm workaround (from earlier episode pydata__xarray-2905)" in recall[
        "content"
    ]
    same_repo.finalize()


def test_general_rows_recall_across_repos(tmp_path, make_backend, fake_client):
    """General rows flow to every episode of the run, whatever the repo —
    rendered with the :general layer tag."""
    repo_a = make_backend(instance_id="pydata__xarray-2905", output_dir=str(tmp_path / "runs" / "a"), scope="run")
    repo_a.start()
    _extract_candidates(repo_a, fake_client, approved_candidate(999, "debug_method", "general recallterm lesson"))
    repo_a.finalize()

    repo_b = make_backend(instance_id="django__django-11099", output_dir=str(tmp_path / "runs" / "b"), scope="run")
    repo_b.start()
    repo_b.set_task("recallterm task")
    recall = repo_b.recall_context()
    assert "- [fact:general] debug_method: general recallterm lesson (from earlier episode pydata__xarray-2905)" in recall[
        "content"
    ]
    repo_b.finalize()


# ---------------------------------------------------------------------------
# Dedupe must not cross layers (§4)
# ---------------------------------------------------------------------------
def test_general_candidate_never_supersedes_a_repo_bound_row(tmp_path, make_backend, fake_client):
    """The layer guard: a general candidate of the same type+key must not
    destroy the repo-bound row — the two layers coexist and both render."""
    backend = make_backend(instance_id="pydata__xarray-2905", output_dir=str(tmp_path / "inst"), scope="run")
    backend.start()
    backend.set_task("sharedkey task")
    _extract_candidates(
        backend, fake_client, approved_candidate(999, "sharedkey", "repo sharedkey value", scope="project"), step=1
    )
    _extract_candidates(backend, fake_client, approved_candidate(999, "sharedkey", "general sharedkey value"), step=2)
    rows = backend._system.memory_search("minisweagent", query=None, review_status=None)
    by_value = {row.value: row for row in rows}
    repo_row = by_value["repo sharedkey value"]
    assert repo_row.review_status == "approved" and repo_row.project_id == "pydata__xarray"
    general_row = by_value["general sharedkey value"]
    assert general_row.review_status == "approved" and general_row.project_id is None
    recall = backend.recall_context()
    assert "- [fact:repo] sharedkey: repo sharedkey value" in recall["content"]
    assert "- [fact:general] sharedkey: general sharedkey value" in recall["content"]
    backend.finalize()


def test_project_candidate_never_supersedes_the_shared_general_row(tmp_path, make_backend, fake_client):
    """The mirror guard: the general layer is shared run-wide, so a repo-bound
    candidate must NOT supersede a general row of the same type+key — one
    repo's refinement would otherwise destroy the shared row for every other
    repo. The two coexist; the repo-bound row overlays the general one in that
    repo's recall."""
    backend = make_backend(instance_id="pydata__xarray-2905", output_dir=str(tmp_path / "inst"), scope="run")
    backend.start()
    backend.set_task("sharedkey task")
    _extract_candidates(backend, fake_client, approved_candidate(999, "sharedkey", "general sharedkey value"), step=1)
    _extract_candidates(
        backend, fake_client, approved_candidate(999, "sharedkey", "repo sharedkey value", scope="project"), step=2
    )
    rows = backend._system.memory_search("minisweagent", query=None, review_status=None)
    assert len(rows) == 2
    by_value = {row.value: row for row in rows}
    general_row, repo_row = by_value["general sharedkey value"], by_value["repo sharedkey value"]
    assert general_row.review_status == "approved" and general_row.project_id is None
    assert repo_row.review_status == "approved" and repo_row.project_id == "pydata__xarray"
    assert repo_row.supersedes == []
    recall = backend.recall_context()
    assert "- [fact:repo] sharedkey: repo sharedkey value" in recall["content"]
    assert "- [fact:general] sharedkey: general sharedkey value" in recall["content"]
    backend.finalize()


def test_repo_candidate_cannot_destroy_the_general_row_for_other_repos(tmp_path, make_backend, fake_client):
    """Cross-repo regression: repo A stores a general lesson; repo B's episode
    extracts a project candidate with a colliding type+key (keys are
    LLM-invented, so collisions happen). The shared row must survive — repo
    A's next episode still recalls it."""
    repo_a = make_backend(instance_id="pydata__xarray-2905", output_dir=str(tmp_path / "runs" / "a"), scope="run")
    repo_a.start()
    _extract_candidates(repo_a, fake_client, approved_candidate(999, "shared_lesson", "general recallterm lesson"))
    repo_a.finalize()

    repo_b = make_backend(instance_id="django__django-11099", output_dir=str(tmp_path / "runs" / "b"), scope="run")
    repo_b.start()
    _extract_candidates(
        repo_b, fake_client, approved_candidate(999, "shared_lesson", "django shared_lesson fact", scope="project")
    )
    repo_b.finalize()
    assert db_rows(tmp_path / "runs" / "cure_memory.sqlite3", "SELECT value, review_status FROM memories ORDER BY id") == [
        ("general recallterm lesson", "approved"),
        ("django shared_lesson fact", "approved"),
    ]

    repo_a2 = make_backend(instance_id="pydata__xarray-3000", output_dir=str(tmp_path / "runs" / "c"), scope="run")
    repo_a2.start()
    repo_a2.set_task("recallterm task")
    recall = repo_a2.recall_context()
    assert (
        "- [fact:general] shared_lesson: general recallterm lesson (from earlier episode pydata__xarray-2905)"
        in recall["content"]
    )
    repo_a2.finalize()


def test_identical_value_noop_spans_layers(tmp_path, make_backend, fake_client):
    """The identical-content no-op deliberately spans layers: a repo-bound
    candidate whose value already lives in a general row stores nothing new —
    the same content is already visible to the candidate's whole lattice, and
    a duplicate repo-bound copy would only double the recall line."""
    backend = make_backend(instance_id="pydata__xarray-2905", output_dir=str(tmp_path / "inst"), scope="run")
    backend.start()
    _extract_candidates(backend, fake_client, approved_candidate(999, "sharedkey", "same sharedkey value"), step=1)
    _extract_candidates(
        backend, fake_client, approved_candidate(999, "sharedkey", "same sharedkey value", scope="project"), step=2
    )
    rows = backend._system.memory_search("minisweagent", query=None, review_status=None)
    assert len(rows) == 1
    assert rows[0].project_id is None and rows[0].review_status == "approved"
    backend.finalize()


def test_first_active_row_mirrors_the_layer_guard():
    """The audit replay applies the exact same layer predicate as the native
    upsert: a general candidate matches only NULL-project rows (a newer
    repo-bound row of the same type+key is NOT the no-op target); a repo-bound
    candidate matches its own repo's rows plus the general rows, never another
    repo's."""

    def row(row_id, project_id, updated_at):
        return SimpleNamespace(
            id=row_id,
            user_id="minisweagent",
            project_id=project_id,
            memory_type="fact",
            key="k",
            review_status="approved",
            updated_at=updated_at,
        )

    view = {
        1: row(1, "pydata__xarray", "2026-01-02T00:00:00"),  # newer, repo-bound
        2: row(2, None, "2026-01-01T00:00:00"),  # older, general
    }
    general = SimpleNamespace(user_id="minisweagent", project_id=None, memory_type="fact", key="k")
    assert CureMemoryBackend._first_active_row(view, general) is view[2]
    same_repo = SimpleNamespace(user_id="minisweagent", project_id="pydata__xarray", memory_type="fact", key="k")
    assert CureMemoryBackend._first_active_row(view, same_repo) is view[1]
    other_repo = SimpleNamespace(user_id="minisweagent", project_id="django__django", memory_type="fact", key="k")
    assert CureMemoryBackend._first_active_row(view, other_repo) is view[2]


# ---------------------------------------------------------------------------
# Recall rendering names the layer (§5)
# ---------------------------------------------------------------------------
def test_render_line_names_the_layer(tmp_path, make_backend):
    """The layer tag derives from project_id — the field every semantic
    decision (recall lattice, upsert guard, audit replay) keys on — and
    augments the memory_type tag; the needs_verification suffix composes
    after it."""
    backend = make_backend()
    memory = SimpleNamespace(memory_type="fact", key="k", value="v", needs_verification=False, project_id=None)
    assert backend._render_line(memory) == "- [fact:general] k: v"
    memory.project_id = "pydata__xarray"
    assert backend._render_line(memory) == "- [fact:repo] k: v"
    memory.needs_verification = True
    assert backend._render_line(memory) == "- [fact:repo needs_verification] k: v"


# ---------------------------------------------------------------------------
# Deletion layer treatment (the §4 guard's deletion counterpart)
# ---------------------------------------------------------------------------
def _extract_deletion(backend, fake_client, deletion, step):
    """Record one message and run one extraction carrying the deletion item."""
    backend.record([{"role": "user", "content": "forget the shared rule"}], step=step)
    fake_client.queue.append({"candidates": [], "deletions": [deletion], "rejections": []})
    backend._extract(step)


def _seed_both_layers(backend, fake_client):
    """One extraction seeding the §4 coexistence case: a repo-bound row and a
    general row sharing type+key, so one free-text target matches both."""
    _extract_candidates(
        backend,
        fake_client,
        approved_candidate(999, "shared_rule", "repo shared_rule value", scope="project"),
        approved_candidate(999, "shared_rule", "general shared_rule value"),
        step=1,
    )


def test_deletion_stays_in_the_session_layer_by_default(tmp_path, make_backend, fake_client):
    """A layer-less deletion from one repo's episode removes only that repo's
    own matching rows: the coexisting general row of the same type+key
    survives for the rest of the run. Fail closed for destruction — crossing
    into the shared layer requires naming it."""
    backend = make_backend(instance_id="pydata__xarray-2905", output_dir=str(tmp_path / "inst"), scope="run")
    backend.start()
    _seed_both_layers(backend, fake_client)
    _extract_deletion(backend, fake_client, {"message_id": 999, "target": "shared_rule"}, step=2)
    rows = {row.value: row for row in backend._system.memory_search("minisweagent", query=None, review_status=None)}
    assert rows["repo shared_rule value"].review_status == "deleted"
    assert rows["general shared_rule value"].review_status == "approved"
    backend.finalize()


def test_deletion_addresses_the_general_layer_only_when_named(tmp_path, make_backend, fake_client):
    """scope="user" on a deletion targets the general layer explicitly: the
    shared row is removed for the whole run while the repo-bound row of the
    same type+key survives — the mirror of the default."""
    backend = make_backend(instance_id="pydata__xarray-2905", output_dir=str(tmp_path / "inst"), scope="run")
    backend.start()
    _seed_both_layers(backend, fake_client)
    _extract_deletion(backend, fake_client, {"message_id": 999, "target": "shared_rule", "scope": "user"}, step=2)
    rows = {row.value: row for row in backend._system.memory_search("minisweagent", query=None, review_status=None)}
    assert rows["general shared_rule value"].review_status == "deleted"
    assert rows["repo shared_rule value"].review_status == "approved"
    backend.finalize()


# ---------------------------------------------------------------------------
# Project-less sessions (the standardized endpoint's add path)
# ---------------------------------------------------------------------------
def test_project_less_session_labels_every_candidate_user(tmp_path):
    """A session without a project cannot bind a row to a repository, so every
    candidate — an explicit "project", the fail-closed default for a missing
    scope, and an explicit "user" — lands labeled "user" with project_id=None
    (label == behavior; the convention memory_add already uses)."""
    from cure_memory.system import CUREMemorySystem

    client = ScriptedDecisionClient()
    system = CUREMemorySystem(str(tmp_path / "noproj.sqlite3"), llm_client=client)
    try:
        system.start_session("alice", session_id="s1")  # no project_id: the endpoint's add path
        message = system.record_message("user", "remember the layered rule")
        missing = approved_candidate(message.id, "k_missing", "v missing")
        del missing["scope"]
        client.queue.append(
            {
                "candidates": [
                    missing,
                    approved_candidate(message.id, "k_project", "v project", scope="project"),
                    approved_candidate(message.id, "k_user", "v user", scope="user"),
                ],
                "deletions": [],
                "rejections": [],
            }
        )
        system.extract_runtime_memories()
        rows = {row.key: row for row in system.store.list_memories("alice", review_status=None)}
        assert set(rows) == {"k_missing", "k_project", "k_user"}
        assert all(row.scope == "user" and row.project_id is None for row in rows.values())
    finally:
        system.close()


def test_project_less_session_deletion_defaults_to_the_general_layer(tmp_path):
    """On the project-less surface the session's own layer IS the general one:
    a layer-less deletion target matches only project_id=NULL rows, never a
    repo-bound row."""
    from cure_memory.system import CUREMemorySystem

    client = ScriptedDecisionClient()
    system = CUREMemorySystem(str(tmp_path / "noprojdel.sqlite3"), llm_client=client)
    try:
        system.start_session("alice", session_id="s1")
        system.memory_add("alice", memory_type="fact", key="general_rule", value="general shared rule value")
        system.memory_add(
            "alice", memory_type="fact", key="repo_rule", value="repo shared rule value", project_id="pydata__xarray"
        )
        system.record_message("user", "forget the shared rule")
        client.queue.append({"candidates": [], "deletions": [{"target": "shared rule"}], "rejections": []})
        system.extract_runtime_memories()
        rows = {row.key: row for row in system.store.list_memories("alice", review_status=None)}
        assert rows["general_rule"].review_status == "deleted"
        assert rows["repo_rule"].review_status == "approved"
    finally:
        system.close()


def test_project_less_session_scoped_project_deletion_matches_nothing(tmp_path):
    """A project-less session holds no repository context, so a deletion
    naming scope="project" cannot bind to any repository: it matches nothing
    rather than every repo's rows (fail closed for destruction)."""
    from cure_memory.system import CUREMemorySystem

    client = ScriptedDecisionClient()
    system = CUREMemorySystem(str(tmp_path / "noprojscope.sqlite3"), llm_client=client)
    try:
        system.start_session("alice", session_id="s1")
        system.memory_add(
            "alice", memory_type="fact", key="repo_a_rule", value="repo a shared rule value", project_id="repo_a"
        )
        system.memory_add(
            "alice", memory_type="fact", key="repo_b_rule", value="repo b shared rule value", project_id="repo_b"
        )
        system.memory_add("alice", memory_type="fact", key="general_rule", value="general shared rule value")
        system.record_message("user", "forget the shared rule")
        client.queue.append(
            {"candidates": [], "deletions": [{"target": "shared rule", "scope": "project"}], "rejections": []}
        )
        result = system.extract_runtime_memories()
        assert result.deleted == []
        rows = system.store.list_memories("alice", review_status=None)
        assert all(row.review_status == "approved" for row in rows)
    finally:
        system.close()


def test_deletion_skips_terminal_rows(tmp_path):
    """One logical deletion counts once: an already-superseded row is history —
    re-matching it would inflate the deletion count and overwrite the
    superseded marker, so only the live row is deleted."""
    from cure_memory.system import CUREMemorySystem

    client = ScriptedDecisionClient()
    system = CUREMemorySystem(str(tmp_path / "terminal.sqlite3"), llm_client=client)
    try:
        system.start_session("alice", session_id="s1")
        message = system.record_message("user", "remember the rule v1")
        client.queue.append(
            {"candidates": [approved_candidate(message.id, "rule", "shared rule v1")], "deletions": [], "rejections": []}
        )
        system.extract_runtime_memories()
        message = system.record_message("user", "remember the rule v2")
        client.queue.append(
            {"candidates": [approved_candidate(message.id, "rule", "shared rule v2")], "deletions": [], "rejections": []}
        )
        system.extract_runtime_memories()
        system.record_message("user", "forget the rule")
        client.queue.append({"candidates": [], "deletions": [{"target": "shared rule"}], "rejections": []})
        result = system.extract_runtime_memories()
        assert [row.value for row in result.deleted] == ["shared rule v2"]
        rows = {row.value: row for row in system.store.list_memories("alice", review_status=None)}
        assert rows["shared rule v1"].review_status == "superseded"
        assert rows["shared rule v2"].review_status == "deleted"
    finally:
        system.close()
