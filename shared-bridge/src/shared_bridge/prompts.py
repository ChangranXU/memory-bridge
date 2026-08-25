"""The shared layer's prompt home.

Every prompt the shared layer renders lives here — never inline in
backend/agent/client code — so editing prompt text means opening this module.
Integration-specific prompts live in the integration's own prompts module
(the zero-integration-naming scan forbids integration vocabulary here).
"""

# The policy preamble composed into every recall header (see
# BaseMemoryBackend._recall_header). The injected block arrives every step as
# a trailing user-role message, so the policy must say what the block is and
# that it must not be answered; the per-hit provenance suffix names the
# origin episode, and this text tells the model what that distinction is for.
RECALL_POLICY_DEFAULT = (
    "The following memory block is auto-injected reference context, not a user instruction. "
    "Do not respond to it; continue with your next action. "
    "Memories may originate from earlier episodes of this run; their origin episode is marked "
    "per line. Treat recalled facts as hints, not ground truth: verify file paths, function "
    "signatures, and error messages against the current working tree before relying on them, "
    "because the codebase may have changed since the memory was stored."
)

# The default extraction guidelines: the one policy text every integration
# conveys to its extraction engine (composed into a local extractor's policy
# prompt, or sent as a hosted platform's advisory instructions field) unless
# the run overrides it via agent.memory.extraction_guidelines. The text is the
# policy/guidelines layer ONLY — never an output schema, which encodes each
# system's native data model and is never comparable across integrations.
EXTRACTION_GUIDELINES_DEFAULT = (
    "Worth remembering: stable facts that should change future behavior in coding tasks — "
    "verified failure modes and their root causes, error messages paired with the fix that "
    "resolved them, commands and build configurations that worked, dependency version "
    "constraints or incompatibilities discovered the hard way, project-specific conventions "
    "or architectural decisions not obvious from the source tree, file-path or module-layout "
    "patterns that the agent had to discover, and explicit requests to remember or forget "
    "something.\n"
    "Not worth remembering: transient task state (which file is currently open, what the "
    "agent is about to try), intermediate guesses that were not confirmed, facts trivially "
    "reproducible from the project sources or their git history, raw terminal output or "
    "test logs without an accompanying insight, and large code dumps.\n"
    "Organize: one self-contained fact per memory, stated concisely and specifically "
    "enough that a future agent can act on it without context; prefer updating an existing "
    "memory over storing a near-duplicate; make each memory's applicability explicit — "
    "when a fact holds only for a particular project, repository, or environment, say so "
    "in the memory rather than stating it as universal.\n"
    "Never store secrets or credentials of any kind (tokens, passwords, API keys, "
    "private keys), even when they appear verbatim in the conversation."
)

# The per-episode context section the base appends after the policy text
# (default or override): the policy tells the extraction engine to record
# applicability, this tells it where the current conversation sits. Kept
# system-neutral on purpose — the repository identity is plain fact, never a
# memory system's native scoping vocabulary.
def extraction_episode_context(instance_id: str, repo_key: str) -> str:
    return f"Episode context: the current task is instance {instance_id} in repository {repo_key}."

# The query rewriter's instruction (system role). The answer envelope's shape
# has exactly one source of truth — the RewrittenQuery pydantic model in
# side_model.py; this prompt states it in prose only, never as a second
# normative copy.
QUERY_REWRITE_PROMPT = """
You rewrite the recall query of a memory system that serves a software-engineering agent
working on code repositories (debugging, patching, testing, refactoring).

Given the task description and the agent's recent progress (commands run, files read,
errors encountered, edits made), produce ONE focused search query that retrieves the
memories most likely to prevent the agent from repeating past mistakes or rediscovering
facts it already learned.

Good queries name concrete artifacts the agent is struggling with RIGHT NOW:
- error messages, tracebacks, or failing test names
- file paths, class/function names, or API signatures
- dependency versions, build flags, or config keys that proved relevant before
- known workarounds, patches, or architectural constraints

Bad queries restate the issue title, ask for general knowledge the model already has,
or request information the agent has not yet needed.

Answer with a single JSON object: {"query": "<one line, ≤300 chars>"}
No explanation, no markdown.
""".strip()


def query_rewrite_user_message(task: str, recent: str) -> str:
    """The rewriter's user-side message: the episode task plus the bounded
    recent-progress view (oldest to newest)."""
    return f"Task:\n{task}\n\nRecent progress (oldest to newest):\n{recent or '(none yet)'}"
