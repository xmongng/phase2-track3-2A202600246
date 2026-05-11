"""Graph construction.

This module is intentionally import-safe. It imports LangGraph only inside the builder so unit tests
that check schema/metrics can run even if students are still debugging graph wiring.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .nodes import (
    answer_node,
    approval_node,
    ask_clarification_node,
    classify_node,
    dead_letter_node,
    evaluate_node,
    finalize_node,
    intake_node,
    retry_or_fallback_node,
    risky_action_node,
    tool_node,
)
from .routing import route_after_approval, route_after_classify, route_after_evaluate, route_after_retry
from .state import AgentState


def build_graph(checkpointer: Any | None = None):
    """Build and compile the LangGraph workflow.

    Architecture:
    - intake → classify (normalization + routing)
    - classify routes to answer/tool/clarify/risky_action/retry
    - tool → evaluate creates the retry loop (the 'done?' gate)
    - risky path requires HITL approval before tool/action
    - retry loop bounded by max_attempts → dead_letter on exhaustion
    - all paths eventually reach finalize → END
    """
    try:
        # pyrefly: ignore [missing-import]
        from langgraph.graph import END, START, StateGraph
    except Exception as exc:  # pragma: no cover - helpful install error
        raise RuntimeError("LangGraph is required. Run: pip install -e '.[dev]' or pip install langgraph") from exc

    graph = StateGraph(AgentState)
    graph.add_node("intake", intake_node)
    graph.add_node("classify", classify_node)
    graph.add_node("answer", answer_node)
    graph.add_node("tool", tool_node)
    graph.add_node("evaluate", evaluate_node)
    graph.add_node("clarify", ask_clarification_node)
    graph.add_node("risky_action", risky_action_node)
    graph.add_node("approval", approval_node)
    graph.add_node("retry", retry_or_fallback_node)
    graph.add_node("dead_letter", dead_letter_node)
    graph.add_node("finalize", finalize_node)

    graph.add_edge(START, "intake")
    graph.add_edge("intake", "classify")
    graph.add_conditional_edges(
        "classify", 
        route_after_classify,
        {
            "answer": "answer",
            "tool": "tool",
            "clarify": "clarify",
            "risky_action": "risky_action",
            "retry": "retry",
        }
    )
    graph.add_edge("tool", "evaluate")
    graph.add_conditional_edges(
        "evaluate", 
        route_after_evaluate,
        {"retry": "retry", "answer": "answer"}
    )
    graph.add_edge("clarify", "finalize")
    graph.add_edge("risky_action", "approval")
    graph.add_conditional_edges(
        "approval", 
        route_after_approval,
        {"tool": "tool", "clarify": "clarify"}
    )
    graph.add_conditional_edges(
        "retry", 
        route_after_retry,
        {"dead_letter": "dead_letter", "tool": "tool"}
    )
    graph.add_edge("answer", "finalize")
    graph.add_edge("dead_letter", "finalize")
    graph.add_edge("finalize", END)

    return graph.compile(checkpointer=checkpointer)


def export_graph_diagram(output_path: str | Path = "outputs/graph_diagram.md") -> str:
    """Export a Mermaid diagram of the compiled graph (bonus extension).

    Builds the graph without a checkpointer (diagram-only, no persistence needed),
    calls draw_mermaid(), writes the result to a Markdown file, and returns the
    raw Mermaid string for embedding in reports.
    """
    compiled = build_graph(checkpointer=None)
    mermaid_str = compiled.get_graph().draw_mermaid()

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(f"```mermaid\n{mermaid_str}\n```\n", encoding="utf-8")
    return mermaid_str


def get_state_history(graph: Any, thread_id: str) -> list[dict]:
    """Return the full state history for a thread (time-travel extension).

    Usage::

        compiled = build_graph(checkpointer=build_checkpointer("memory"))
        # ... run scenarios ...
        history = get_state_history(compiled, "thread-S01_simple")
        for snapshot in history:
            print(snapshot)

    Each snapshot is a StateSnapshot with .values (the AgentState dict) and
    .config (which checkpoint this snapshot belongs to). You can replay from
    any earlier checkpoint by passing its config back to graph.invoke().
    """
    history = []
    for snapshot in graph.get_state_history({"configurable": {"thread_id": thread_id}}):
        history.append({"values": snapshot.values, "config": snapshot.config})
    return history

