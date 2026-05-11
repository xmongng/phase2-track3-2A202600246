"""Report generation helper."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .metrics import MetricsReport, ScenarioMetric


def _scenario_row(m: ScenarioMetric) -> str:
    status = "✅" if m.success else "❌"
    approval = "✅" if m.approval_required else "—"
    return (
        f"| {m.scenario_id} | {m.expected_route} | {m.actual_route or '—'} "
        f"| {status} | {m.retry_count} | {m.interrupt_count} | {approval} |"
    )


def render_report(metrics: MetricsReport) -> str:
    """Render a complete lab report from execution metrics.

    Covers all rubric sections: architecture, state schema, scenario results,
    failure analysis, persistence evidence, extension work, and improvement plan.
    """
    generated_at = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    rows = "\n".join(_scenario_row(m) for m in metrics.scenario_metrics)
    failed = [m for m in metrics.scenario_metrics if not m.success]
    failed_section = "\n".join(
        f"- **{m.scenario_id}**: expected `{m.expected_route}`, got `{m.actual_route or 'none'}`. "
        f"Errors: {', '.join(m.errors) if m.errors else 'none'}"
        for m in failed
    ) or "All scenarios succeeded — no failures recorded."

    return f"""# Day 08 Lab Report
*Generated: {generated_at}*

## 1. Student
- Student ID: 2A202600246
- Lab: Phase 2 — Track 3 (LangGraph Agentic Orchestration)

---

## 2. Architecture

The workflow is a **LangGraph StateGraph** with 11 nodes wired by deterministic keyword-based routing.

```
START → intake → classify → [conditional routing]
  simple       → answer → finalize → END
  tool         → tool → evaluate → answer → finalize → END
               ↑              ↓ (needs_retry)
               └─── retry ←──┘
                       ↓ (max_attempts exceeded)
                  dead_letter → finalize → END
  missing_info → clarify → finalize → END
  risky        → risky_action → approval → tool → evaluate → answer → finalize → END
  error        → retry → tool → evaluate → [retry loop or dead_letter]
```

**Key design decisions:**
- **Priority routing**: risky > tool > missing_info > error > simple — prevents keyword conflicts.
- **Whole-word tokenization**: uses `re.sub` to strip punctuation before matching, avoiding substring false positives (e.g., `"it"` won't match `"item"`).
- **Append-only state lists**: `messages`, `tool_results`, `errors`, `events` all use `Annotated[list, add]` reducer for immutable audit trail.
- **Bounded retry**: `route_after_retry` compares `attempt >= max_attempts` before looping, so S07 (max_attempts=1) dead-letters immediately.
- **HITL approval**: `approval_node` defaults to mock-approve for CI. Set `LANGGRAPH_INTERRUPT=true` for real human pause via `interrupt()`.

---

## 3. State Schema

| Field | Reducer | Why |
|---|---|---|
| `query` | overwrite | normalized once by intake_node |
| `route` | overwrite | current classification only |
| `risk_level` | overwrite | latest risk assessment |
| `attempt` | overwrite | monotonically increasing counter |
| `max_attempts` | overwrite | scenario-level config |
| `final_answer` | overwrite | last answer wins |
| `pending_question` | overwrite | last clarification question |
| `proposed_action` | overwrite | latest HITL proposal |
| `approval` | overwrite | last approval decision |
| `evaluation_result` | overwrite | latest evaluate gate decision |
| `messages` | **append** | conversation history audit |
| `tool_results` | **append** | all tool call outputs for audit |
| `errors` | **append** | accumulate all error messages |
| `events` | **append** | full node-by-node audit trail |

---

## 4. Scenario Results

| Scenario | Expected | Actual | Success | Retries | Interrupts | Approval Required |
|---|---|---|---|---:|---:|---|
{rows}

**Summary**
- Total scenarios: **{metrics.total_scenarios}**
- Success rate: **{metrics.success_rate:.1%}**
- Average nodes visited: **{metrics.avg_nodes_visited:.1f}**
- Total retries: **{metrics.total_retries}**
- Total HITL interrupts: **{metrics.total_interrupts}**

---

## 5. Failure Analysis

{failed_section}

### Considered failure modes

1. **Unbounded retry loop (S05, S07):** Without the `attempt >= max_attempts` guard in `route_after_retry`, error-route scenarios would loop forever. S07 sets `max_attempts=1`, so the first retry immediately dead-letters. The guard in `retry_or_fallback_node` increments `attempt` and `route_after_retry` checks the bound before routing back to `tool`.

2. **Risky action without approval (S04, S06):** If `approval_node` were omitted or the conditional edge bypassed, destructive actions (delete, refund) would execute without human sign-off. The graph enforces `risky_action → approval → tool`; a rejected decision reroutes to `clarify` rather than `tool`.

3. **Keyword substring collisions:** Queries like `"tracking an item"` contain both `"track"` (tool) and `"it"` (missing_info). Whole-word tokenization via `_tokenize()` and priority order (tool > missing_info) prevent misclassification.

4. **Dead-letter route mismatch:** `dead_letter_node` now writes `route = Route.DEAD_LETTER.value` back to state. Without this, `metric_from_state` would compare `"dead_letter"` against `"error"` and mark S07 as failed even though it reached the correct terminal state.

---

## 6. Persistence & Recovery Evidence

- **Checkpointer**: `MemorySaver` (default) — all runs use `thread_id = f"thread-{{scenario.id}}"`.
- **Thread IDs**: Each scenario gets a unique thread, enabling `graph.get_state_history(thread_id)` for replay.
- **SQLite support**: `build_checkpointer("sqlite")` creates `outputs/checkpoints.db` with WAL mode (`PRAGMA journal_mode=WAL`). This allows state to survive process restarts — run a scenario, kill the process, re-invoke with the same `thread_id`, and the graph resumes from the last checkpoint.
- **Time travel**: `graph.get_state_history()` (exposed via `get_state_history()` in `graph.py`) lists every checkpoint. Passing an earlier `.config` to `graph.invoke()` replays from that point.

---

## 7. Extension Work

| Extension | Status | Evidence |
|---|---|---|
| SQLite persistence + WAL mode | ✅ Implemented | `persistence.py` — `SqliteSaver(conn=sqlite3.connect(...))` |
| Graph diagram (Mermaid) | ✅ Implemented | `graph.py::export_graph_diagram()` → `outputs/graph_diagram.md` |
| Time-travel helper | ✅ Implemented | `graph.py::get_state_history()` |
| Rich proposed_action with evidence | ✅ Implemented | `nodes.py::risky_action_node` |
| PII scrubbing in intake | ✅ Implemented | `nodes.py::_scrub_pii()` — email, phone, card patterns |
| Grounded answer (tool results + approval) | ✅ Implemented | `nodes.py::answer_node` |
| Exponential backoff metadata | ✅ Implemented | `nodes.py::retry_or_fallback_node` — `backoff_seconds` in event |

---

## 8. Improvement Plan

If given one more day, the top priorities would be:

1. **LLM-as-judge in evaluate_node**: Replace the heuristic `startswith("ERROR")` check with a small LLM call (e.g., GPT-4o-mini) that reads the tool result and returns a structured JSON verdict. This handles ambiguous responses better than string matching.
2. **Real Streamlit HITL UI**: Wire `approval_node` to a Streamlit app that displays the `proposed_action` and lets a reviewer click Approve/Reject, resuming the graph via `graph.invoke(None, config=checkpoint_config)`.
3. **Structured tool results**: Replace the free-text mock with a Pydantic `ToolResult` model, making `evaluate_node` validation typed and LLM-free.
4. **Parallel fan-out**: Use LangGraph's `Send()` API to dispatch two tools concurrently (e.g., CRM lookup + order DB), merging via the `add` reducer — demonstrating true async agent parallelism.
"""


# Keep backward-compatible alias used by cli.py
render_report_stub = render_report


def write_report(metrics: MetricsReport, output_path: str | Path) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_report(metrics), encoding="utf-8")

