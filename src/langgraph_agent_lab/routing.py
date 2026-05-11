"""Routing functions for conditional edges."""

from __future__ import annotations

import logging

from .state import AgentState, Route

log = logging.getLogger(__name__)


def route_after_classify(state: AgentState) -> str:
    """Map the classified route to the next graph node.

    Falls back to 'answer' for any unknown/unexpected route value, logging a warning
    so production alerting can catch routing gaps without crashing the graph.
    """
    route = state.get("route", Route.SIMPLE.value)
    mapping = {
        Route.SIMPLE.value: "answer",
        Route.TOOL.value: "tool",
        Route.MISSING_INFO.value: "clarify",
        Route.RISKY.value: "risky_action",
        Route.ERROR.value: "retry",
    }
    destination = mapping.get(route)
    if destination is None:
        log.warning("Unknown route '%s' — falling back to 'answer'. scenario_id=%s", route, state.get("scenario_id"))
        return "answer"
    return destination


def route_after_retry(state: AgentState) -> str:
    """Decide whether to retry the tool call or dead-letter the request.

    Compares `attempt` against `max_attempts`. When exhausted the request is
    routed to dead_letter for manual triage. Otherwise routes back to tool
    to execute the next attempt.
    """
    if int(state.get("attempt", 0)) >= int(state.get("max_attempts", 3)):
        return "dead_letter"
    return "tool"


def route_after_evaluate(state: AgentState) -> str:
    """Decide whether the tool result is satisfactory or needs a retry.

    This is the 'done?' gate that powers the retry loop — a key LangGraph
    advantage over LCEL chains. Returns 'retry' on any error signal,
    'answer' on success.
    """
    if state.get("evaluation_result") == "needs_retry":
        return "retry"
    return "answer"


def route_after_approval(state: AgentState) -> str:
    """Continue to tool execution only if the HITL reviewer approved.

    - approved=True  → proceed to tool / execute risky action
    - approved=False → route back to clarify, asking the requester for changes
                       or signalling the action was rejected
    """
    approval = state.get("approval") or {}
    if approval.get("approved"):
        return "tool"
    log.info("Risky action rejected by reviewer. scenario_id=%s", state.get("scenario_id"))
    return "clarify"

