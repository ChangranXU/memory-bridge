"""mem0 integration prompt home.

Every prompt/header string the bridge renders lives here — never inline in
backend/agent/client code — so editing prompt text means opening this module.
The shared recall policy preamble (the do-not-respond sentence) lives in the
shared layer's prompts module and is composed in by the base's header.
"""

# The recall block's section text: the integration title, the descriptive
# lead-in, and the memory-list section title.
RECALL_TITLE = "## Persistent Memory (mem0)"
RECALL_LEAD_IN = "Auto-extracted facts from earlier coding sessions in this memory scope, ranked by relevance to the current task:"
RECALL_SECTION_TITLE = "## Recalled Memories"
