"""T3: recording — trajectory messages land in session_messages, normalized."""

import json

from minisweagent.models.test_models import DeterministicToolcallModel

from conftest import SUBMIT_COMMAND, db_rows


def test_trajectory_messages_recorded_and_normalized(tmp_path, make_agent, make_bash_output, extract_env, fake_client):
    output_dir = tmp_path / "inst"
    tool_only = make_bash_output(None, ["echo hello_rec"])
    tool_only["extra"]["response"] = {"raw": "RAW_RESPONSE_MARKER_xyz"}
    structured = make_bash_output(
        [{"type": "text", "text": "structured_text_block"}, {"type": "image_url", "image_url": {"url": "RAW_IMAGE_BYTES"}}],
        ["echo structured"],
    )
    outputs = [
        tool_only,
        structured,
        make_bash_output("y" * 500, ["echo long"]),
        make_bash_output("submitting", [SUBMIT_COMMAND]),
    ]
    model = DeterministicToolcallModel(outputs=outputs)
    agent = make_agent(
        model,
        memory={
            "enabled": True,
            "scope": "instance",
            "output_dir": str(output_dir),
            "max_message_chars": 120,
            "extract_every_n_steps": 0,
        },
        cost_limit=100.0,
    )
    agent.run("task")

    db_path = output_dir / "cure_memory.sqlite3"
    assert db_path.exists()
    rows = db_rows(db_path, "SELECT role, content, metadata FROM session_messages ORDER BY id")
    # system, user(instance), a0, t0, a1, t1, a2, t2, a3(submit), exit
    assert len(rows) == 10
    assert {row[0] for row in rows} == {"system", "user", "assistant", "tool", "exit"}
    for _, _, metadata in rows:
        parsed = json.loads(metadata)
        assert set(parsed) == {"step"}
        assert isinstance(parsed["step"], int)

    contents = [row[1] for row in rows]
    # tool-only assistant turn (content=None) exposes its parsed bash command
    assistant_rows = [c for role, c, _ in rows if role == "assistant"]
    assert any("Actions:" in c and "echo hello_rec" in c for c in assistant_rows)
    # structured content: text blocks preserved, non-text blocks become placeholders
    assert any("structured_text_block" in c and "[image_url]" in c for c in assistant_rows)
    # hard cap includes the truncation marker
    long_row = next(c for c in assistant_rows if "yyyy" in c)
    assert len(long_row) <= 120
    assert long_row.endswith("[truncated]")
    # raw extra.response and raw image data never reach the store
    assert all("RAW_RESPONSE_MARKER_xyz" not in c for c in contents)
    assert all("RAW_IMAGE_BYTES" not in c for c in contents)

    data = json.loads((output_dir / "memory.json").read_text())
    assert data["counts"]["messages_recorded"] == 10


def test_record_skips_transient_marker_and_handles_none_content(make_backend):
    backend = make_backend()
    backend.start()
    backend.record(
        [{"role": "user", "content": "must not land", "extra": {"transient_recall": True}}], step=1
    )
    assert backend._counts["messages_recorded"] == 0
    backend.record(
        [
            {
                "role": "assistant",
                "content": None,
                "extra": {"actions": [{"command": "echo z", "tool_call_id": "c1"}]},
            }
        ],
        step=2,
    )
    assert backend._counts["messages_recorded"] == 1
    backend.finalize()
    rows = db_rows(backend._db_path, "SELECT role, content, metadata FROM session_messages")
    assert rows == [("assistant", 'Actions:\n[{"command": "echo z", "tool_call_id": "c1"}]', '{"step": 2}')]
