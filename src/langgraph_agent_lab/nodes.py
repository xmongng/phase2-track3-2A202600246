"""Node implementations for the LangGraph workflow.

Each function is small, testable, and returns a partial state update. Input state is never mutated.
"""

from __future__ import annotations

import re

from .state import AgentState, ApprovalDecision, Route, make_event

# ---------------------------------------------------------------------------
# Keyword sets — priority order: risky > tool > missing_info > error > simple
# ---------------------------------------------------------------------------
_RISKY_KEYWORDS = {
    "refund", "delete", "send", "cancel", "remove", "revoke", "wipe",
    "erase", "terminate", "deactivate", "credit", "charge", "override",
}
_TOOL_KEYWORDS = {
    "status", "order", "lookup", "check", "track", "find", "search",
    "retrieve", "fetch", "show", "list", "get",
}
_ERROR_KEYWORDS = {
    "timeout", "fail", "failure", "error", "crash", "unavailable",
    "exception", "broken", "cannot", "recover",
}

_PII_PATTERNS = [
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[a-z]{2,}\b"), "[EMAIL]"),
    (re.compile(r"\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b"), "[PHONE]"),
    (re.compile(r"\b\d{4}[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}\b"), "[CARD]"),
]


def _scrub_pii(text: str) -> str:
    """Replace common PII patterns with placeholder tokens."""
    for pattern, replacement in _PII_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _tokenize(query: str) -> set[str]:
    """Return a set of lowercased, punctuation-stripped word tokens."""
    return {re.sub(r"[^a-z0-9]", "", w) for w in query.lower().split() if w}


def intake_node(state: AgentState) -> dict:
    """Normalize raw query: strip whitespace, scrub PII, extract metadata."""
    raw = state.get("query", "")
    normalized = _scrub_pii(raw.strip())
    word_count = len(normalized.split())
    return {
        "query": normalized,
        "messages": [f"intake: {normalized[:60]}"],
        "events": [
            make_event(
                "intake",
                "completed",
                "query normalized",
                word_count=word_count,
                pii_scrubbed=(normalized != raw.strip()),
            )
        ],
    }


def classify_node(state: AgentState) -> dict:
    """Classify the query into a route using prioritized keyword heuristics.

    Priority order: risky > tool > missing_info > error > simple.
    Checks whole-word tokens to avoid substring false positives (e.g. 'it' vs 'item').
    """
    query = state.get("query", "")
    tokens = _tokenize(query)
    query_lower = query.lower()

    route = Route.SIMPLE
    risk_level = "low"

    # Priority 1 — risky: destructive / financial / external side-effects
    if tokens & _RISKY_KEYWORDS:
        route = Route.RISKY
        risk_level = "high"

    # Priority 2 — tool: data lookup / retrieval
    elif tokens & _TOOL_KEYWORDS:
        route = Route.TOOL
        risk_level = "medium"

    # Priority 3 — missing_info: short/vague query with pronouns
    elif len(tokens) < 5 and ("it" in tokens or "this" in tokens or "that" in tokens):
        route = Route.MISSING_INFO

    # Priority 4 — error: system failure / transient problem keywords
    elif tokens & _ERROR_KEYWORDS or any(kw in query_lower for kw in _ERROR_KEYWORDS):
        route = Route.ERROR

    # Priority 5 — simple: safe informational default
    # (no else needed; route already defaults to SIMPLE)

    return {
        "route": route.value,
        "risk_level": risk_level,
        "events": [
            make_event(
                "classify",
                "completed",
                f"route={route.value}",
                risk_level=risk_level,
                matched_tokens=sorted(tokens & (_RISKY_KEYWORDS | _TOOL_KEYWORDS | _ERROR_KEYWORDS)),
            )
        ],
    }


def ask_clarification_node(state: AgentState) -> dict:
    """Ask for missing information instead of hallucinating.

    Generates a clarification question grounded in what was said.
    """
    query = state.get("query", "")
    if query:
        question = (
            f"I see you mentioned '{query[:80]}' but I need more context. "
            "Could you provide the order ID, customer ID, or a more specific description?"
        )
    else:
        question = "Could you provide more details? For example, an order ID or specific issue description."

    return {
        "pending_question": question,
        "final_answer": question,
        "events": [make_event("clarify", "completed", "clarification question generated", query_length=len(query))],
    }


def tool_node(state: AgentState) -> dict:
    """Execute a mock tool call with idempotent retry simulation.

    Error-route scenarios simulate transient failures on attempt < 2 to exercise the retry loop.
    All other routes succeed on first call.
    """
    attempt = int(state.get("attempt", 0))
    scenario_id = state.get("scenario_id", "unknown")
    route = state.get("route", "")

    # Simulate transient failure for error-route scenarios (first two attempts)
    if route == Route.ERROR.value and attempt < 2:
        result = f"ERROR: transient failure attempt={attempt} scenario={scenario_id}"
        status = "error"
    else:
        query_snippet = state.get("query", "")[:30]
        result = (
            f"mock-tool-result: scenario={scenario_id} "
            f"query='{query_snippet}' "
            f"attempt={attempt} status=ok"
        )
        status = "ok"

    return {
        "tool_results": [result],
        "events": [
            make_event(
                "tool",
                "completed",
                f"tool executed attempt={attempt} status={status}",
                attempt=attempt,
                status=status,
                scenario_id=scenario_id,
            )
        ],
    }


def risky_action_node(state: AgentState) -> dict:
    """Prepare a structured risky action proposal with evidence and risk justification.

    The proposed_action is consumed by approval_node during HITL review.
    """
    query = state.get("query", "")
    risk_level = state.get("risk_level", "high")
    scenario_id = state.get("scenario_id", "unknown")

    # Identify which risky keywords triggered this route
    matched = sorted(_tokenize(query) & _RISKY_KEYWORDS)

    proposed_action = (
        f"RISKY ACTION PROPOSAL\n"
        f"Scenario: {scenario_id}\n"
        f"Risk level: {risk_level}\n"
        f"Triggered by keywords: {', '.join(matched) if matched else 'destructive action detected'}\n"
        f"Original request: {query[:120]}\n"
        f"Action required: Execute {', '.join(matched) if matched else 'sensitive operation'} after human approval.\n"
        f"Justification: This action has irreversible external effects and requires explicit authorization."
    )

    return {
        "proposed_action": proposed_action,
        "events": [
            make_event(
                "risky_action",
                "pending_approval",
                "risky action prepared — awaiting HITL approval",
                risk_level=risk_level,
                triggered_keywords=matched,
            )
        ],
    }


def approval_node(state: AgentState) -> dict:
    """Human-in-the-loop approval step.

    - If LANGGRAPH_INTERRUPT=true: uses LangGraph interrupt() for real HITL.
    - Default: mock approval (approved=True) for CI/offline runs.

    Reject decisions are recorded and route back to clarify via route_after_approval.
    """
    import os

    if os.getenv("LANGGRAPH_INTERRUPT", "").lower() == "true":
        from langgraph.types import interrupt  # type: ignore[import-untyped]

        value = interrupt(
            {
                "proposed_action": state.get("proposed_action"),
                "risk_level": state.get("risk_level"),
                "query": state.get("query"),
            }
        )
        if isinstance(value, dict):
            decision = ApprovalDecision(**value)
        else:
            decision = ApprovalDecision(approved=bool(value))
    else:
        # Mock: auto-approve so all scenarios complete end-to-end in tests
        decision = ApprovalDecision(
            approved=True,
            reviewer="mock-reviewer",
            comment="Auto-approved in offline/test mode. Set LANGGRAPH_INTERRUPT=true for real HITL.",
        )

    return {
        "approval": decision.model_dump(),
        "events": [
            make_event(
                "approval",
                "completed",
                f"HITL decision: approved={decision.approved}",
                approved=decision.approved,
                reviewer=decision.reviewer,
                comment=decision.comment,
            )
        ],
    }


def retry_or_fallback_node(state: AgentState) -> dict:
    """Increment attempt counter and record retry metadata with backoff details.

    Bounded by max_attempts (enforced in route_after_retry).
    Exponential backoff delay is logged in metadata for observability.
    """
    attempt = int(state.get("attempt", 0)) + 1
    max_attempts = int(state.get("max_attempts", 3))
    backoff_seconds = min(2 ** (attempt - 1), 30)  # 1s, 2s, 4s … capped at 30s

    errors = [f"transient failure attempt={attempt}/{max_attempts}"]

    return {
        "attempt": attempt,
        "errors": errors,
        "events": [
            make_event(
                "retry",
                "retry_scheduled",
                f"retry attempt {attempt}/{max_attempts}",
                attempt=attempt,
                max_attempts=max_attempts,
                backoff_seconds=backoff_seconds,
            )
        ],
    }


def answer_node(state: AgentState) -> dict:
    """Produce a grounded final response.

    Grounds the answer in tool_results (if present), approval decision (if risky route),
    and route context for a contextually appropriate reply.
    """
    route = state.get("route", "simple")
    tool_results = state.get("tool_results", []) or []
    approval = state.get("approval") or {}
    query = state.get("query", "")

    if tool_results:
        latest_result = tool_results[-1]
        if route == Route.RISKY.value and approval.get("approved"):
            answer = (
                f"Your request has been reviewed and approved by {approval.get('reviewer', 'a reviewer')}. "
                f"Result: {latest_result}"
            )
        else:
            answer = f"Here is what I found for your request: {latest_result}"
    else:
        answer = (
            f"Your request '{query[:80]}' has been processed. "
            "No additional data was retrieved from external systems."
        )

    return {
        "final_answer": answer,
        "events": [
            make_event(
                "answer",
                "completed",
                "grounded answer generated",
                route=route,
                has_tool_results=bool(tool_results),
                approved=approval.get("approved", False),
            )
        ],
    }


def evaluate_node(state: AgentState) -> dict:
    """Evaluate tool results — the 'done?' gate that enables the retry loop.

    Checks the latest tool result for error indicators. A result is considered
    successful if it contains no ERROR prefix, otherwise signals needs_retry.
    """
    tool_results = state.get("tool_results", []) or []
    latest = tool_results[-1] if tool_results else ""

    if latest.startswith("ERROR"):
        return {
            "evaluation_result": "needs_retry",
            "events": [
                make_event(
                    "evaluate",
                    "needs_retry",
                    "tool result contains error — retry required",
                    latest_result=latest[:80],
                )
            ],
        }

    return {
        "evaluation_result": "success",
        "events": [
            make_event(
                "evaluate",
                "success",
                "tool result satisfactory — proceeding to answer",
                latest_result=latest[:80],
            )
        ],
    }


def dead_letter_node(state: AgentState) -> dict:
    """Log unresolvable failures for manual review.

    Third layer of error strategy: retry → fallback → dead letter.
    Emits a structured dead-letter event for alerting and audit.
    The original route (e.g. 'error') is preserved so metrics compare correctly.
    """
    attempt = int(state.get("attempt", 0))
    scenario_id = state.get("scenario_id", "unknown")
    errors = list(state.get("errors", []) or [])

    return {
        "final_answer": (
            f"Request '{state.get('query', '')[:60]}' could not be completed after "
            f"{attempt} attempt(s). Logged for manual review. Reference: {scenario_id}."
        ),
        "events": [
            make_event(
                "dead_letter",
                "failed",
                f"max retries exceeded — dead-lettered after {attempt} attempts",
                attempt=attempt,
                scenario_id=scenario_id,
                error_count=len(errors),
            )
        ],
    }


def finalize_node(state: AgentState) -> dict:
    """Finalize the run: emit a summary audit event covering the full execution trace."""
    events = state.get("events", []) or []
    nodes_visited = [e.get("node", "unknown") for e in events]
    return {
        "events": [
            make_event(
                "finalize",
                "completed",
                "workflow finished",
                route=state.get("route"),
                nodes_visited=nodes_visited,
                final_answer_set=bool(state.get("final_answer") or state.get("pending_question")),
            )
        ]
    }
