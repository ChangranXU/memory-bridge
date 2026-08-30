"""Crash-window atomicity pins for the store's multi-write sequences
(system.py ``memory_replace`` / ``_upsert_memory`` / the extraction deletion
batch over ``store.atomic()``): a failure partway through one logical write
rolls the whole unit back — never a half-written supersede pair (the
replacement live while the old row stays approved, or the old rows terminal
with no successor saved) or a half-applied deletion batch. Also pinned: a
failed single write outside atomic() leaves no open implicit transaction
behind (the next atomic()'s BEGIN would otherwise mask the root cause)."""

import sqlite3

import pytest

from conftest import ScriptedDecisionClient

from cure_memory.models import SessionMessage
from cure_memory.system import CUREMemorySystem


@pytest.fixture
def system(tmp_path):
    instance = CUREMemorySystem(str(tmp_path / "atomicity.sqlite3"))
    yield instance
    instance.close()


def _rows(system, user_id="u", review_status=None):
    return system.store.list_memories(user_id=user_id, review_status=review_status)


def test_memory_replace_rolls_back_the_pair_when_the_second_write_fails(system):
    """memory_replace saves the replacement, then stamps the old row: failing
    the stamp must roll back the replacement's save too (pre-fix, the save was
    already committed — two approved rows for one key)."""
    row = system.memory_add("u", "fact", "k1", "original value")

    def boom(memory):
        raise RuntimeError("disk full mid-replace")

    system.store.update_memory = boom
    with pytest.raises(RuntimeError):
        system.memory_replace("u", row.id, "new value")

    (only,) = _rows(system)
    assert only.id == row.id
    assert only.review_status == "approved" and only.value == "original value"


def test_upsert_rolls_back_the_supersede_when_the_save_fails(system):
    """_upsert_memory marks the active rows superseded, then saves the
    candidate: failing the save must roll the status change back (pre-fix, the
    old row stayed superseded with no successor — the content lost)."""
    row = system.memory_add("u", "fact", "k1", "original value")

    def boom(memory):
        raise RuntimeError("disk full mid-upsert")

    system.store.save_memory = boom
    with pytest.raises(RuntimeError):
        system.memory_add("u", "fact", "k1", "new value")

    (only,) = _rows(system)
    assert only.id == row.id
    assert only.review_status == "approved" and only.value == "original value"


def test_upsert_rolls_back_the_new_row_when_the_pointer_stamp_fails(system):
    """The second pass stamps superseded_by on the old rows after the save:
    failing there must roll back the saved candidate as well (pre-fix, the new
    row stayed live and the old row's history pointer was lost)."""
    row = system.memory_add("u", "fact", "k1", "original value")
    calls = 0
    real_update = system.store.update_memory

    def flaky(memory):
        nonlocal calls
        calls += 1
        if calls == 2:  # the superseded_by stamp (second pass)
            raise RuntimeError("disk full at the pointer stamp")
        return real_update(memory)

    system.store.update_memory = flaky
    with pytest.raises(RuntimeError):
        system.memory_add("u", "fact", "k1", "new value")

    (only,) = _rows(system)
    assert only.id == row.id
    assert only.review_status == "approved" and only.superseded_by is None


def test_atomic_block_commits_jointly_on_success(system):
    """The success path is unchanged: one logical replace lands both halves —
    the replacement approved and the old row superseded with its pointer."""
    row = system.memory_add("u", "fact", "k1", "original value")
    replacement = system.memory_replace("u", row.id, "new value")

    by_id = {item.id: item for item in _rows(system)}
    assert by_id[row.id].review_status == "superseded"
    assert by_id[row.id].superseded_by == replacement.id
    assert by_id[replacement.id].review_status == "approved"
    assert by_id[replacement.id].value == "new value"
    # ...and an identical-content re-add still dedupes against the live row.
    again = system.memory_add("u", "fact", "k1", "new value")
    assert again.id == replacement.id and len(_rows(system)) == 2


def test_failed_step_write_leaves_no_open_transaction(system):
    """A failed write outside atomic() must roll sqlite's implicit transaction
    back: otherwise the next atomic()'s explicit BEGIN fails with "cannot
    start a transaction within a transaction", and every later extraction
    errors with that instead of the original root cause."""
    system.start_session("u", session_id="s1")
    with pytest.raises(sqlite3.IntegrityError):
        # content is NOT NULL: the INSERT fails after sqlite auto-began.
        system.store.save_message(SessionMessage(session_id="s1", user_id="u", role="user", content=None))
    # The next atomic unit must work (pre-fix: OperationalError on BEGIN).
    system.memory_add("u", "fact", "k1", "value")
    (only,) = _rows(system)
    assert only.value == "value"
    # The failed message was never persisted.
    assert system.store.list_messages("s1", "u") == []


def test_extraction_deletion_batch_rolls_back_as_one_unit(tmp_path):
    """The extraction's deletion batch is one atomic unit (the supersede
    sequences' discipline): failing the second row's delete must roll the
    first row's back — the checkpoint holds for the retry either way, so a
    half-applied batch would be the only state the retry cannot cleanly
    re-decide from."""
    client = ScriptedDecisionClient()
    instance = CUREMemorySystem(str(tmp_path / "delbatch.sqlite3"), llm_client=client)
    try:
        instance.start_session("u", session_id="s1")
        instance.memory_add("u", "fact", "rule_alpha", "shared rule alpha value")
        instance.memory_add("u", "fact", "rule_beta", "shared rule beta value")
        instance.record_message("user", "forget the shared rules")
        # One deletion decision matching BOTH live rows.
        client.queue.append({"candidates": [], "deletions": [{"target": "shared rule"}], "rejections": []})
        real_update = instance.store.update_memory
        calls = 0

        def flaky(memory):
            nonlocal calls
            calls += 1
            if calls == 2:  # the second row's delete
                raise RuntimeError("disk full mid-deletion")
            return real_update(memory)

        instance.store.update_memory = flaky
        with pytest.raises(RuntimeError):
            instance.extract_runtime_memories()
        rows = _rows(instance, user_id="u")
        assert {row.key for row in rows} == {"rule_alpha", "rule_beta"}
        assert all(row.review_status == "approved" for row in rows)
    finally:
        instance.close()
