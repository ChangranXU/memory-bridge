"""Prompt homes for the tencentdb integration.

Every agent-facing string lives here (house rule): the recall header sections
and the two agent-initiated-read curl guides (L2 scene read, L0 conversation
search). The shared recall policy preamble and the extraction guidelines stay
in ``shared_bridge/prompts.py``.
"""

RECALL_TITLE = "## Persistent Memory (TencentDB Agent Memory)"
RECALL_LEAD_IN = (
    "Memory layers are active: recalled atomic facts (L1, repo-scoped, ranked "
    "by relevance), a distilled user profile (L3, if present), and an index of "
    "longer scenario files (L2) you may read on demand. Raw conversation "
    "messages (L0) are not injected — search them on demand with the command "
    "below when you need exact wording."
)
RECALL_SECTION_TITLE = "## Recalled Memories"
# Rendered prefix for the L3 persona pseudo-hit (prepended, score-less): marks
# the distilled profile apart from repo-scoped L1 facts.
PERSONA_LINE_PREFIX = "- (user profile)"
SCENE_INDEX_TITLE = "## Scenario Files (L2 index)"
SCENE_INDEX_LEAD_IN = (
    "Longer write-ups distilled from earlier episodes, listed highest heat "
    "first: heat counts how often a scene has been distilled into, so a "
    "higher heat marks a more reinforced scene. Read one on demand with the "
    "exact command below (it costs one agent step; the output replaces "
    "nothing else in your context):"
)
# The scene-read guide: a fixed, self-contained POST template. The headers are
# mandatory on every data-plane route (parseV2Auth 401s a request without a
# non-empty Bearer and x-tdai-service-id; the router is POST-only). The host
# name is load-bearing: the agent's bash runs inside the prediction container
# where 127.0.0.1 is the container's own loopback — host.docker.internal is
# the host-reachable name. team/agent/user are baked in; task_id is
# deliberately absent (profiles ignore the task dimension upstream).
SCENE_READ_GUIDE = """\
```bash
curl -sS -X POST http://host.docker.internal:8420/v3/scenario/read \\
  -H 'Authorization: Bearer local' \\
  -H 'x-tdai-service-id: default' \\
  -H 'Content-Type: application/json' \\
  -d '{{"team_id":"{team_id}","agent_id":"{agent_id}","user_id":"{user_id}","path":"<scene-file>"}}'
```
Replace `<scene-file>` with a path from the index above."""

CONVO_SEARCH_TITLE = "## Conversation Search (L0, on demand)"
CONVO_SEARCH_LEAD_IN = (
    "Raw stored messages from earlier episodes in this repository — the fallback when "
    "the recalled facts don't have the information you need, or when you want the "
    "exact words said before. Search on demand with the exact command below (it costs "
    "one agent step; stop searching after 3 total attempts in a turn):"
)
# The conversation-search guide: a fixed, self-contained POST template, same
# shape as the scene-read guide (mandatory headers; host.docker.internal is
# load-bearing). team/agent/user/task are ALL baked in — task_id keeps the
# arm's repo tier, the same scoping the host-side L1 search carries (the wire
# schema accepts task_id and the route filters on it). The pipe to jq sits at
# the END of the -d line: a trailing pipe continues the command without a
# backslash, the shape least likely to come back mangled from a model retype.
# The jq formatter renders the response nearly verbatim to the native openclaw
# plugin variant's tool output (the parity target — the core variant's extra
# Session segment cannot be rebuilt from the wire): the null-first branch
# relays an error envelope's own message (jq's null|length is 0, so keying the
# empty case on length alone would render an error as the FALSE empty-result
# string), and map(...) | join("\n\n") emits one output for the whole hit list
# so the stream stays byte-identical to the native lines.join("\n") under jq
# -r's one-newline-per-output (the empty/failure early-returns gain exactly
# one trailing newline). The score truncates to three decimals where native
# toFixed(3) rounds — accepted, display-only (ranking is server-side). tee -a
# both shows the result AND accumulates every search of the episode in one
# container-local file, so later steps re-read it instead of re-searching.
# Authoring rules: this is a .format() template — every literal JSON brace is
# doubled, and the jq program's \n / \( sequences stay literal two-character
# text (the raw string keeps them; the program itself carries no braces).
CONVO_SEARCH_GUIDE = r"""```bash
curl -sS -X POST http://host.docker.internal:8420/v3/conversation/search \
  -H 'Authorization: Bearer local' \
  -H 'x-tdai-service-id: default' \
  -H 'Content-Type: application/json' \
  -d '{{"team_id":"{team_id}","agent_id":"{agent_id}","user_id":"{user_id}","task_id":"{task_id}","query":"<query>","limit":{limit}}}' |
  jq -r 'if .data.messages == null then "Conversation search failed: \(.message // "unexpected response body")" elif (.data.messages|length)==0 then "No matching conversation messages found." else "Found \(.data.messages|length) matching message(s):\n", (.data.messages | map("---\n**[\(.role)]** [\(.timestamp)] (score: \(.score|tostring|.[0:5]))\n\n\(.content)") | join("\n\n")) end' | tee -a /tmp/tdai-l0-searches.md
```
Replace `<query>` with what you want to find (exact wording works well). The formatted result prints above AND appends to `/tmp/tdai-l0-searches.md` — every search of the episode accumulates there, so re-read that file instead of re-searching."""
