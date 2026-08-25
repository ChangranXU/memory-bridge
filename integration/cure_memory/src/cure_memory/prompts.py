"""
Prompt contracts used by the CURE Memory product.
"""

MEMORY_POLICY_PROMPT = """
You manage durable memory for a software-engineering agent that debugs, patches,
and tests code repositories.

Save only information that should change future agent behavior on coding tasks:
- verified failure modes paired with the fix or workaround that resolved them
- error messages / tracebacks and their root causes
- build commands, flags, or dependency versions that proved necessary
- project-specific conventions, file-layout patterns, or architectural constraints
  not obvious from the source tree
- reusable workflows (e.g. "run pytest with -x flag in this repo")
- explicit user memory requests

Do not save:
- secrets, tokens, passwords, API keys, private keys
- transient task state (which file is open, what the agent plans to try next)
- facts trivially available in project files or git history
- assistant guesses that the user did not confirm
- raw terminal output, test logs, or large code snippets without an insight

Every memory must have:
- memory_type
- scope ("project" for repo-bound facts, "user" for repo-independent lessons)
- key
- concise value
- confidence
- review_status
- source evidence
""".strip()


def memory_policy_prompt(guidelines: str = "") -> str:
    """The extraction policy prompt with the run's extraction guidelines
    (the shared host-side policy layer) appended as one extra section. Empty
    guidelines keep the prompt byte-identical to ``MEMORY_POLICY_PROMPT`` —
    an integration whose engine accepts no prompt rules conveys nothing."""
    guidelines = (guidelines or "").strip()
    if not guidelines:
        return MEMORY_POLICY_PROMPT
    return f"{MEMORY_POLICY_PROMPT}\n\nAdditional extraction guidelines for this run:\n{guidelines}"


MEMORY_EXTRACTION_LLM_PROMPT = """
You are the memory extraction decision model for CURE Memory, serving a
software-engineering agent that works on code repositories.

Use the memory policy to decide whether new conversation messages should create,
update, reject, or delete durable memories. Policy hints from deterministic code are
only hints. Do not treat words like "remember", "forget", "记住", or "忘记" as
commands by themselves; decide from the full conversational intent.

Focus on extracting facts that help a coding agent avoid repeating mistakes:
- A confirmed bug root cause and its fix (approved, high confidence)
- A build/test command that worked after earlier failures (approved)
- A dependency constraint discovered during debugging (approved)
- An ongoing investigation with no conclusion yet (reject: transient_task_state)
- A raw error traceback without analysis (reject: needs further context)

Choose each candidate's scope deliberately — it fixes which future episodes may
see the memory:
- scope "project": the fact is bound to this repository — file paths, module
  layout, repo-specific commands or flags, API or behavior of this codebase, its
  dependency constraints. The value may (and usually should) name the repo or
  its modules.
- scope "user": the lesson survives a repo switch — debugging methods, tool
  usage lessons, generic workflow patterns (e.g. "prefer running the failing
  test file directly over the whole suite"). The value must NOT name this repo,
  its files, or its APIs; if it cannot be stated without them, it is a project
  memory.
When in doubt, choose "project": a wrongly-general memory leaks into other
repositories, while a wrongly-project memory only fails to help.

Return strict JSON with these top-level keys:

{
  "candidates": [
    {
      "message_id": 1,
      "memory_type": "workflow",
      "scope": "project",
      "key": "xarray_test_command",
      "value": "Run xarray tests with: python -m pytest xarray/tests/test_dataset.py -x",
      "description": "Verified test invocation for xarray",
      "confidence": 0.95,
      "review_status": "approved",
      "source_type": "observed_action",
      "evidence": ["agent ran this command and tests passed"],
      "sensitivity": "private",
      "needs_verification": false
    }
  ],
  "deletions": [
    {
      "message_id": 2,
      "target": "outdated test command",
      "scope": "project"
    }
  ],
  "rejections": [
    {
      "message_id": 3,
      "reason": "transient_task_state",
      "snippet": "Currently reading the source of Dataset.merge()."
    }
  ]
}

A deletion's optional "scope" names the layer it applies to; each existing
memory's own scope is shown in existing_memories. Omit it to delete only this
repository's own matching memories — never the general ones. Set "user" only
when the repo-independent lesson itself is wrong (that removes it for every
repository of the run); "project" names the repo-bound layer explicitly.

Use approved only when confidence is high and the memory is stable, durable, and
safe. Use pending_review for weak inference, possible conflict, sensitive
organizational context, or time-sensitive project state. Never save secrets.
""".strip()


MEMORY_RECALL_PROMPT = """
Use recalled memory as context for coding tasks, not as unquestionable truth.

If a memory names a file path, function signature, dependency version, build flag,
or test command, verify it against the current working tree before acting — the
codebase may have changed since the memory was stored.
By default, only approved memories should affect agent behavior.
""".strip()


# The recall block's section headers (composed by the bridge over the shared
# recall policy): the policy section title for MEMORY_RECALL_PROMPT and the
# title of the memory-list section itself.
MEMORY_RECALL_POLICY_HEADER = "## CURE Memory Policy"
MEMORY_RECALL_SECTION_HEADER = "## Relevant Approved Memories"
