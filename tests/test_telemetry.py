"""Tracing contract for the agent loop: span tree, timings split by model vs tools, errors, and content privacy.

A fake Foundry client replays a scripted run (thinking → two tool calls → answer), so no Azure calls are made.
"""
from types import SimpleNamespace as NS

import pytest
from azure.ai.agents.models import RunStatus
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from tender import agent

EXPORTER = InMemorySpanExporter()


@pytest.fixture(autouse=True)
def tracing(monkeypatch):
    if not isinstance(trace.get_tracer_provider(), TracerProvider):   # the global provider can be set only once
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(EXPORTER))
        trace.set_tracer_provider(provider)
    EXPORTER.clear()
    monkeypatch.setattr(agent.time, "sleep", lambda s: None)
    monkeypatch.setitem(agent.DISPATCH, "get_tender", lambda tender_id: {"tender_id": tender_id, "status": "scored"})
    monkeypatch.setitem(agent.DISPATCH, "list_tenders", lambda status=None: {"error": "storage unavailable"})
    monkeypatch.delenv("TENDER_TRACE_CONTENT", raising=False)


def _call(i: int, name: str, arguments: str):
    return NS(id=f"call_{i}", function=NS(name=name, arguments=arguments))


class FakeClient:
    """Plays back run states: create → in progress → wants two tools → (after submit) completed."""

    def __init__(self):
        wants_tools = NS(id="run_1", status=RunStatus.REQUIRES_ACTION, required_action=NS(submit_tool_outputs=NS(
            tool_calls=[_call(1, "get_tender", '{"tender_id": "T-009"}'), _call(2, "list_tenders", "{}")])))
        done = NS(id="run_1", status=RunStatus.COMPLETED, last_error=None,
                  usage=NS(prompt_tokens=1200, completion_tokens=80, total_tokens=1280))
        script = [NS(id="run_1", status=RunStatus.IN_PROGRESS), wants_tools, done]
        self.submitted = []
        self.messages = NS(create=lambda **k: None,
                           get_last_message_text_by_role=lambda **k: NS(text=NS(value="Bid: strong fit.")))
        self.runs = NS(create=lambda **k: script.pop(0), get=lambda **k: script.pop(0),
                       submit_tool_outputs=lambda **k: (self.submitted.append(k["tool_outputs"]), script.pop(0))[1])


def _turn():
    result = agent.run_turn(FakeClient(), "asst_1", "thread_1", "Should Horsell bid on T-009?", trace=False)
    return result, {s.name: s for s in EXPORTER.get_finished_spans()}


def test_one_turn_is_one_trace_with_model_and_tool_spans():
    result, spans = _turn()
    assert set(spans) == {f"invoke_agent {agent.AGENT_NAME}", "model_step 1", "model_step 2",
                          "execute_tool get_tender", "execute_tool list_tenders"}
    turn = spans[f"invoke_agent {agent.AGENT_NAME}"]
    for name, s in spans.items():
        assert s.context.trace_id == turn.context.trace_id, name          # all in ONE trace
        if s is not turn:
            assert s.parent.span_id == turn.context.span_id, name         # all direct children of the turn
    assert result["trace_id"] == format(turn.context.trace_id, "032x")
    assert turn.attributes["gen_ai.conversation.id"] == "thread_1"
    assert turn.attributes["gen_ai.usage.input_tokens"] == 1200
    assert turn.attributes["gen_ai.usage.output_tokens"] == 80
    assert turn.attributes["tender.tool_calls"] == 2
    assert turn.attributes["tender.model_steps"] == 2


def test_tool_spans_carry_tender_id_and_flag_errors():
    _, spans = _turn()
    ok, failed = spans["execute_tool get_tender"], spans["execute_tool list_tenders"]
    assert ok.attributes["tender.id"] == "T-009"
    assert ok.attributes["gen_ai.tool.call.id"] == "call_1"
    assert ok.status.status_code != StatusCode.ERROR
    assert failed.status.status_code == StatusCode.ERROR      # tool returned {"error": ...}: counted as a failure
    assert "storage unavailable" in failed.status.description


def test_content_is_not_recorded_unless_allowed(monkeypatch):
    _, spans = _turn()
    every_attribute = {k for s in spans.values() for k in s.attributes}
    assert not {"tender.question", "tender.answer", "gen_ai.tool.call.arguments"} & every_attribute

    EXPORTER.clear()
    monkeypatch.setenv("TENDER_TRACE_CONTENT", "true")
    _, spans = _turn()
    assert spans[f"invoke_agent {agent.AGENT_NAME}"].attributes["tender.question"] == "Should Horsell bid on T-009?"
    assert spans["execute_tool get_tender"].attributes["gen_ai.tool.call.arguments"] == '{"tender_id": "T-009"}'


def test_tracing_off_is_a_no_op(monkeypatch):
    """With no exporter configured, setup_tracing installs nothing and the agent still works."""
    from tender import telemetry

    monkeypatch.setattr(telemetry, "_configured", False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("TENDER_TRACE_CONSOLE", raising=False)
    assert telemetry.setup_tracing() is False
