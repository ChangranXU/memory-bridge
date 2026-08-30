"""
Runtime extractor for CURE Memory.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from .models import INACTIVE_REVIEW_STATUSES, ExtractionResult, Memory, Rejection, SessionMessage
from .prompts import MEMORY_EXTRACTION_LLM_PROMPT, memory_policy_prompt


class ChatGPTMemoryDecisionClient:
    """
    OpenAI-compatible chat adapter for memory extraction decisions.

    Connection settings come from the constructor (or the CURE_MEMORY_LLM_*
    env). There are deliberately no built-in endpoint/credential defaults: an
    ``or``-chain fallback to a hardcoded third-party endpoint could leak
    $OPENAI_API_KEY there. If no API key (or no base URL) is configured, the
    client returns an empty decision with ``last_error`` set, so local tests
    and demos remain offline-safe.
    """

    def __init__(
        self,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        max_completion_tokens: int = 1200,
        reasoning_effort: Optional[str] = "minimal",
        response_format: Optional[Dict[str, Any]] = None,
        timeout: float = 60.0,
        max_retries: int = 2,
    ):
        self.model = model or os.getenv("CURE_MEMORY_LLM_MODEL")
        self.base_url = (base_url or os.getenv("CURE_MEMORY_LLM_BASE_URL") or "").strip().rstrip("/")
        self.api_key = api_key or os.getenv("CURE_MEMORY_LLM_API_KEY")
        self.max_completion_tokens = max_completion_tokens
        self.reasoning_effort = reasoning_effort
        self.response_format = response_format
        self.timeout = timeout
        self.max_retries = max_retries
        self.last_error: Optional[str] = None

    def decide_memory_updates(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """One decision round over the batch.

        The return value alone cannot distinguish "the model decided there is
        nothing to change" from a failed call: every failure class (transport,
        HTTP status, decode, wrong envelope, empty completion) returns the
        all-empty decision with ``last_error`` set. A caller MUST consult
        ``last_error`` before treating the decision as authoritative —
        ``BasicMemoryExtractor.extract`` does, which is what holds the
        extraction checkpoint over the unprocessed batch.
        """
        raw_response = self._call_chatgpt_api(request)
        if not raw_response:
            return self.empty_decision()
        raw_response = self._extract_json_text(raw_response)
        try:
            parsed = json.loads(raw_response)
        except json.JSONDecodeError:
            self.last_error = "json_decode_error"
            return self.empty_decision()
        if not isinstance(parsed, dict):
            self.last_error = "non_dict_json"
            return self.empty_decision()
        # A dict without any decision key is a wrong-envelope response (the
        # model may never have run the per-message decisions): fail so the
        # checkpoint holds and the batch retries — same failure mode as an
        # empty completion, one layer up. A present key carrying null is the
        # common "none" idiom with no content to lose (reads as an empty
        # list); any other non-list value (e.g. a bare dict) could carry
        # content the application layer would silently drop, so it errors.
        # Same one level down: the application layer keeps only dict items,
        # so a non-dict item inside a list is silently dropped content too.
        present = [key for key in ("candidates", "deletions", "rejections") if key in parsed]
        invalid = any(
            parsed[key] is not None
            and (
                not isinstance(parsed[key], list)
                or any(not isinstance(item, dict) for item in parsed[key])
            )
            for key in present
        )
        if not present or invalid:
            self.last_error = "invalid_decision_schema"
            return self.empty_decision()
        self.last_error = None
        return parsed

    def _call_chatgpt_api(self, request: Dict[str, Any]) -> str:
        if not self.api_key:
            self.last_error = "missing_api_key"
            return ""
        if not self.base_url:
            # A key without an endpoint would only fail deeper — urlopen raises
            # ValueError on the relative URL, outside the retryable tuple:
            # offline-safe like a missing key, never an uncaught traceback.
            self.last_error = "missing_base_url"
            return ""

        payload = {
            "model": self.model,
            "messages": self._chat_messages(request),
            "max_completion_tokens": self.max_completion_tokens,
        }
        if self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort
        if self.response_format:
            payload["response_format"] = self.response_format
        http_request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        parsed = None
        for attempt in range(self.max_retries + 1):
            try:
                with urllib.request.urlopen(http_request, timeout=self.timeout) as response:
                    body = response.read().decode("utf-8", "replace")
                parsed = json.loads(body)
            except urllib.error.HTTPError as error:
                error.close()  # release the socket; an unread error body leaks it
                self.last_error = f"http_{error.code}"
                if error.code < 500 or attempt >= self.max_retries:
                    return ""
            except (
                urllib.error.URLError,
                TimeoutError,
                OSError,
                json.JSONDecodeError,
            ) as error:
                self.last_error = error.__class__.__name__
                if attempt >= self.max_retries:
                    return ""
            else:
                if isinstance(parsed, dict):
                    self.last_error = None
                    break
                # A 200 carrying a non-object body ("null", a bare list, a
                # number) is a failed response in the decode class: retried
                # like one, and it must never reach the choices read — a None
                # would read as "no error", so the extraction checkpoint would
                # silently advance past the unprocessed batch.
                self.last_error = "non_dict_body"
                if attempt >= self.max_retries:
                    return ""
            time.sleep(0.5 * (attempt + 1))

        choices = parsed.get("choices", [])
        # A non-empty non-list "choices" (or a non-dict first item) is the
        # same malformed-envelope class one level down: guard it instead of
        # raising an uncaught KeyError/AttributeError past the error taxonomy.
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            self.last_error = "missing_choices"
            return ""
        message = choices[0].get("message", {})
        if not isinstance(message, dict):
            self.last_error = "missing_content"
            return ""
        content = message.get("content", "")
        if isinstance(content, str):
            text = content.strip()
        elif isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    parts.append(part["text"])
            text = "".join(parts).strip()
        else:
            self.last_error = "missing_content"
            return ""
        # An empty completion (a reasoning model can burn the whole token
        # budget before emitting content) is a failed decision, not a
        # "nothing worth memorizing" one: without last_error the extraction
        # checkpoint would silently advance past the unprocessed messages.
        if not text:
            self.last_error = "empty_content"
            return ""
        return text

    def _chat_messages(self, request: Dict[str, Any]) -> List[Dict[str, str]]:
        system_prompt = "\n\n".join(
            part
            for part in [
                str(request.get("system_prompt") or ""),
                str(request.get("policy_prompt") or ""),
            ]
            if part
        )
        extraction_input = {
            "project_id": request.get("project_id"),
            "messages": request.get("messages", []),
            "existing_memories": request.get("existing_memories", []),
            "policy_hints": request.get("policy_hints", []),
        }
        return [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": json.dumps(extraction_input, ensure_ascii=False),
            },
        ]

    def _extract_json_text(self, raw_response: str) -> str:
        text = raw_response.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
            text = re.sub(r"\s*```$", "", text)
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            return text[start:end + 1]
        return text

    @staticmethod
    def empty_decision() -> Dict[str, Any]:
        return {"candidates": [], "deletions": [], "rejections": []}


class BasicMemoryExtractor:
    """
    LLM-backed extractor with deterministic policy hints and safety gates.

    Rules no longer write or delete memory directly. They provide non-authoritative
    hints to the LLM decision client, except for sensitive-information rejection,
    which is kept as a hard local guard so secrets are not sent to the model.
    """

    VALID_REVIEW_STATUSES = {"candidate", "pending_review", "approved"}

    def __init__(self, llm_client: Optional[Any] = None, policy_guidelines: Optional[str] = None):
        self.llm_client = llm_client or ChatGPTMemoryDecisionClient()
        # The run's extraction guidelines (the shared host-side policy layer),
        # composed into the policy prompt — None/"" conveys nothing.
        self.policy_guidelines = (policy_guidelines or "").strip()

    def extract(
        self,
        messages: List[SessionMessage],
        existing: Optional[List[Memory]] = None,
        project_id: Optional[str] = None,
    ) -> ExtractionResult:
        result = ExtractionResult()
        messages_for_llm: List[SessionMessage] = []

        for message in messages:
            text = message.content.strip()
            if not text:
                continue

            if self._is_sensitive(text):
                result.rejected.append(
                    Rejection("sensitive_information", self._snippet(text), self._source(message))
                )
                continue

            messages_for_llm.append(message)

        if not messages_for_llm:
            return result

        request = self._build_llm_request(messages_for_llm, existing or [], project_id)
        decision = self.llm_client.decide_memory_updates(request)
        llm_error = getattr(self.llm_client, "last_error", None)
        if llm_error:
            result.errors.append(f"llm_decision_failed:{llm_error}")
            return result
        self._apply_llm_decision(
            decision=decision,
            messages=messages_for_llm,
            existing=existing or [],
            project_id=project_id,
            result=result,
        )

        return result

    def _build_llm_request(
        self,
        messages: List[SessionMessage],
        existing: List[Memory],
        project_id: Optional[str],
    ) -> Dict[str, Any]:
        return {
            "model": getattr(self.llm_client, "model", "unknown"),
            "system_prompt": MEMORY_EXTRACTION_LLM_PROMPT,
            "policy_prompt": memory_policy_prompt(self.policy_guidelines),
            "project_id": project_id,
            "messages": [self._message_payload(message) for message in messages],
            "existing_memories": [
                self._existing_memory_payload(memory) for memory in existing
            ],
            "policy_hints": [
                self._policy_hint(message) for message in messages
            ],
        }

    def _apply_llm_decision(
        self,
        decision: Dict[str, Any],
        messages: List[SessionMessage],
        existing: List[Memory],
        project_id: Optional[str],
        result: ExtractionResult,
    ) -> None:
        source_by_id = {
            message.id: self._source(message)
            for message in messages
            if message.id is not None
        }
        message_by_id = {
            message.id: message
            for message in messages
            if message.id is not None
        }
        fallback_message = next((message for message in messages if message.role == "user"), messages[0])

        for item in self._decision_items(decision, "rejections"):
            source = self._decision_source(item, source_by_id, fallback_message)
            reason = str(item.get("reason") or "llm_rejected")
            snippet = str(item.get("snippet") or source.get("snippet", ""))
            result.rejected.append(Rejection(reason, self._snippet(snippet), source))

        deleted_ids = set()
        for item in self._decision_items(decision, "deletions"):
            target = str(item.get("target") or item.get("query") or "").strip()
            if not target:
                continue
            # Layer addressing: only an exact "user"/"project" names the layer
            # the deletion applies to; anything else (including a missing
            # scope) keeps the deletion in the session's own layer — fail
            # closed for destruction, so a sloppy target from one repo's
            # episode cannot wipe the shared general layer run-wide.
            scope = str(item.get("scope") or "").strip().lower()
            if scope not in ("user", "project"):
                scope = None
            for memory in self._matching_memories(existing, target, scope=scope, project_id=project_id):
                if memory.id is not None and memory.id in deleted_ids:
                    continue
                if memory.id is not None:
                    deleted_ids.add(memory.id)
                result.deleted.append(memory)

        for item in self._decision_items(decision, "candidates"):
            memory = self._memory_from_candidate(
                item=item,
                source_by_id=source_by_id,
                message_by_id=message_by_id,
                fallback_message=fallback_message,
                project_id=project_id,
            )
            if memory is None:
                continue
            if self._is_sensitive(memory.value):
                result.rejected.append(
                    Rejection("sensitive_information", self._snippet(memory.value), memory.sources[0])
                )
                continue

            result.candidates.append(memory)
            if memory.review_status == "approved":
                result.approved.append(memory)
            elif memory.review_status == "pending_review":
                result.pending_review.append(memory)

    def _memory_from_candidate(
        self,
        item: Dict[str, Any],
        source_by_id: Dict[int, dict],
        message_by_id: Dict[int, SessionMessage],
        fallback_message: SessionMessage,
        project_id: Optional[str],
    ) -> Optional[Memory]:
        key = str(item.get("key") or "").strip()
        value = str(item.get("value") or "").strip()
        if not key or not value:
            return None

        message_id = self._message_id_from_item(item)
        source = source_by_id.get(message_id, self._source(fallback_message))
        source_message = message_by_id.get(message_id, fallback_message)
        confidence = self._clamp_confidence(item.get("confidence", 0.5))
        review_status = str(item.get("review_status") or "candidate").strip()
        if review_status not in self.VALID_REVIEW_STATUSES:
            review_status = "candidate"
        if review_status == "approved" and confidence < 0.85:
            review_status = "pending_review"
        needs_verification = bool(item.get("needs_verification", False))
        if review_status == "approved" and needs_verification:
            review_status = "pending_review"

        # Fail closed on the layer decision: anything but an explicit "user"
        # (including a missing or malformed scope) lands repo-bound — a
        # wrongly-project memory only fails to help, a wrongly-general one
        # leaks into other repositories.
        scope = str(item.get("scope") or "").strip().lower()
        if scope not in ("user", "project"):
            scope = "project"
        if project_id is None:
            # A project-less session (the standardized endpoint's add path)
            # cannot bind a row to a repository: a "project" label would claim
            # repo-bound while the NULL project_id flows to every episode.
            # Label the row honestly — the convention memory_add already uses.
            scope = "user"
        return Memory(
            user_id=source_message.user_id,
            project_id=project_id if scope == "project" else None,
            scope=scope,
            memory_type=str(item.get("memory_type") or "fact").strip() or "fact",
            key=key,
            value=value,
            description=str(item.get("description") or key).strip(),
            confidence=confidence,
            review_status=review_status,
            source_type=str(item.get("source_type") or "llm_extracted").strip(),
            sources=[source],
            evidence=self._candidate_evidence(item, source),
            sensitivity=str(item.get("sensitivity") or "private").strip() or "private",
            needs_verification=needs_verification,
            metadata={
                "decision_source": "llm",
                "llm_model": getattr(self.llm_client, "model", "unknown"),
            },
        )

    def _decision_items(self, decision: Dict[str, Any], key: str) -> List[Dict[str, Any]]:
        items = decision.get(key, []) if isinstance(decision, dict) else []
        if not isinstance(items, list):
            return []
        return [item for item in items if isinstance(item, dict)]

    def _candidate_evidence(self, item: Dict[str, Any], source: dict) -> List[str]:
        evidence = item.get("evidence")
        if isinstance(evidence, list):
            snippets = [str(snippet).strip() for snippet in evidence if str(snippet).strip()]
            if snippets:
                return [self._snippet(snippet) for snippet in snippets]
        return [source.get("snippet", "")]

    def _message_id_from_item(self, item: Dict[str, Any]) -> Optional[int]:
        raw = item.get("message_id", item.get("source_message_id"))
        if raw is None:
            sources = item.get("sources")
            if isinstance(sources, list) and sources and isinstance(sources[0], dict):
                raw = sources[0].get("message_id")
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    def _decision_source(
        self,
        item: Dict[str, Any],
        source_by_id: Dict[int, dict],
        fallback_message: SessionMessage,
    ) -> dict:
        message_id = self._message_id_from_item(item)
        return source_by_id.get(message_id, self._source(fallback_message))

    def _clamp_confidence(self, raw: Any) -> float:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return 0.5
        return max(0.0, min(1.0, value))

    def _message_payload(self, message: SessionMessage) -> Dict[str, Any]:
        return {
            "id": message.id,
            "session_id": message.session_id,
            "user_id": message.user_id,
            "role": message.role,
            "content": message.content,
            "created_at": message.created_at,
            "metadata": message.metadata,
        }

    def _existing_memory_payload(self, memory: Memory) -> Dict[str, Any]:
        return {
            "id": memory.id,
            "scope": memory.scope,
            "memory_type": memory.memory_type,
            "key": memory.key,
            "value": memory.value,
            "confidence": memory.confidence,
            "review_status": memory.review_status,
            "source_type": memory.source_type,
            "needs_verification": memory.needs_verification,
        }

    def _policy_hint(self, message: SessionMessage) -> Dict[str, Any]:
        text = message.content.strip()
        preference_statement = self._preference_statement(text)
        return {
            "message_id": message.id,
            "role": message.role,
            "contains_memory_keyword": self._contains_memory_keyword(text),
            "contains_forget_keyword": self._contains_forget_keyword(text),
            "looks_like_preference": preference_statement is not None,
            "preference_statement": preference_statement,
            "looks_transient": self._is_transient(text),
            "has_relative_date": self._has_relative_date(text),
        }

    def _contains_memory_keyword(self, text: str) -> bool:
        lowered = text.lower()
        return bool(
            "remember" in lowered
            or "记住" in text
        )

    def _contains_forget_keyword(self, text: str) -> bool:
        lowered = text.lower()
        return bool(re.search(r"(?i)\bforget\b", lowered) or "忘记" in text)

    def _preference_statement(self, text: str) -> Optional[str]:
        patterns = [
            r"(?i)\bI\s+now\s+prefer\s+(.+)",
            r"(?i)\bI\s+prefer\s+(.+)",
            r"(?i)\bI\s+like\s+(.+)",
            r"我(?:现在)?(?:更)?喜欢(.+)",
            r"我(?:现在)?(?:更)?偏好(.+)",
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return match.group(1).strip(" .。")
        return None

    def _is_sensitive(self, text: str) -> bool:
        lowered = text.lower()
        return bool(
            re.search(r"\b(api[-_ ]?key|password|token|secret|private key)\b", lowered)
            or re.search(r"\bsk-[a-zA-Z0-9_-]+", text)
        )

    def _is_transient(self, text: str) -> bool:
        lowered = text.lower()
        has_time = any(term in lowered for term in ["today", "current", "right now"])
        has_task = any(term in lowered for term in ["debug", "debugging", "failing test", "fixing"])
        return has_time and has_task

    def _has_relative_date(self, text: str) -> bool:
        lowered = text.lower()
        return any(
            term in lowered
            for term in ["today", "yesterday", "tomorrow", "last week", "next week"]
        )

    def _matching_memories(
        self,
        memories: List[Memory],
        target: str,
        scope: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> List[Memory]:
        tokens = [token for token in re.findall(r"[a-zA-Z0-9_]+", target.lower()) if len(token) > 2]
        matches = []
        for memory in memories:
            if memory.review_status in INACTIVE_REVIEW_STATUSES:
                # Deletions address live rows only: re-matching a terminal row
                # would count one logical deletion twice and overwrite the
                # row's lifecycle marker (a superseded row's history).
                continue
            if scope == "user":
                if memory.project_id is not None:
                    continue
            elif scope == "project":
                # The session's own repository only — a project-less session
                # (project_id=None) matches nothing here, so one repo's client
                # cannot delete another repo's rows.
                if memory.project_id is None or memory.project_id != project_id:
                    continue
            elif memory.project_id != project_id:
                continue
            haystack = f"{memory.key} {memory.value}".lower()
            if any(token in haystack for token in tokens):
                matches.append(memory)
        return matches

    def _source(self, message: SessionMessage) -> dict:
        return {
            "source_type": "message",
            "session_id": message.session_id,
            "message_id": message.id,
            "role": message.role,
            "timestamp": message.created_at,
            "snippet": self._snippet(message.content),
        }

    def _snippet(self, text: str, limit: int = 180) -> str:
        text = " ".join(text.split())
        return text if len(text) <= limit else text[: limit - 3] + "..."
