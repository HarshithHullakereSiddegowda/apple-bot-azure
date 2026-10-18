"""OpenTelemetry tracing for the Tender Copilot agent.

One TRACE per user turn; one SPAN per thing that takes time inside it:

    invoke_agent tender-bid-analyst        the whole turn (tokens, tool-call count, final status)
    ├── model_step 1                       Foundry/LLM thinking until it asks for a tool or answers
    ├── execute_tool get_tender            OUR code running one tool (args, output size, error)
    ├── model_step 2
    ├── execute_tool score_fit
    └── model_step 3                       ...until the final answer

Off by default (the tracer is a no-op, zero cost). Turn it on with environment variables:

    OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318   send spans over OTLP (Jaeger locally, a Collector in Azure)
    TENDER_TRACE_CONSOLE=true                          also print spans to the terminal (learning/debugging)
    TENDER_TRACE_CONTENT=true                          also record questions, answers and tool arguments

Content is OFF by default: prompts can hold personal or commercial data, and traces are kept and read by more
people than the app's data is. Timings, token counts, tool names and tender IDs are always recorded.

Attribute names follow the OpenTelemetry GenAI semantic conventions (gen_ai.*), so any tracing backend that
understands agents can display them.
"""
import os

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter, SimpleSpanProcessor

SERVICE_NAME = "tender-copilot"

tracer = trace.get_tracer("tender.agent")   # a proxy: real once setup_tracing() installs a provider

_configured = False


def setup_tracing() -> bool:
    """Install the tracer provider once per process. Returns True when spans are being exported."""
    global _configured
    if _configured:
        return isinstance(trace.get_tracer_provider(), TracerProvider)
    _configured = True
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    console = os.environ.get("TENDER_TRACE_CONSOLE", "").lower() == "true"
    if not (endpoint or console):
        return False
    provider = TracerProvider(resource=Resource.create({"service.name": SERVICE_NAME}))
    if endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))   # reads the endpoint from the env
    if console:
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    trace.set_tracer_provider(provider)
    return True


def capture_content() -> bool:
    """Record prompt/answer text and tool arguments only when explicitly allowed."""
    return os.environ.get("TENDER_TRACE_CONTENT", "").lower() == "true"


def trace_id_hex(span: trace.Span) -> str | None:
    """The 32-hex trace ID people paste into Jaeger / App Insights to find this turn (None when tracing is off)."""
    ctx = span.get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else None
