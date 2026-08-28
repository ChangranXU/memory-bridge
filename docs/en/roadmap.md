---
description: Future development directions for memory-bridge.
---

# Roadmap

Planned development by layer.

## Planned

### Layer 4 — Experimental

* **Agent portability** — a second agent integration (beyond mini-swe-agent) to validate the adapter pattern and document the porting surface (~200 LOC agent hook layer; backends and integrations stay untouched).
* **`persistent_slot` arm** — a persistent recall slot that stays in the conversation across steps, addressing the token-waste problem of per-step injection repetition.
* **Optional per-repo scope for mem0** — mem0 carries no bridge-side scope in any mode; layering there needs metadata filters (the cure\_memory arm's two-layer scoping — repo-bound vs general — has landed).
* **`extract_max_message_chars`** (mechanism undecided), a `trace.py` split of the tracing code out of `backend.py`, and CURE extractor adoption.

## Design invariants

These properties hold across all future work:

{% hint style="warning" %}
These invariants are non-negotiable — they define what memory-bridge is.
{% endhint %}

* Adding a new memory system must require **no `shared-bridge` change** (the zero-naming scan is a test).
* Schema-v6 event shapes stay stable.
* Annotation remains pure observability — new capability never trades the fail-closed discipline for behavior risk.
