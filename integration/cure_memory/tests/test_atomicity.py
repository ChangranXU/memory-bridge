"""Crash-window atomicity pins for the store's multi-write sequences
(system.py ``memory_replace`` / ``_upsert_memory`` over ``store.atomic()``):
a failure partway through one logical write rolls the whole unit back — never
a half-written supersede pair (the replacement live while the old row stays
approved, or the old rows terminal with no successor saved)."""

import pytest

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
