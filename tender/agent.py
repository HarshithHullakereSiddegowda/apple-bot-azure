"""Agent 2 (Bid Analyst) on Azure AI Foundry Agent Service.

    BRICK 3  TOOLS      job cards (JSON schema) the agent reads to choose a tool
    BRICK 4  AGENT      model + instructions + job cards, created once in the Foundry project
    BRICK 5  run_turn   the loop: Foundry asks for a tool → OUR code runs it → result goes back → until done

Foundry never executes our Python. It pauses the run with REQUIRES_ACTION and names the tool and arguments;
this module runs the matching function from tender/tools.py and submits the output.

    python -m tender.agent "Should Horsell bid on T-009?"     # one question
    python -m tender.agent                                    # interactive chat (same thread)
"""
import json
import os
import sys
import time

from azure.ai.agents import AgentsClient
from azure.ai.agents.models import FunctionDefinition, FunctionToolDefinition, ListSortOrder, RunStatus, ToolOutput
from azure.identity import DefaultAzureCredential
from opentelemetry.trace import Status, StatusCode

from tender import memory, tools
from tender.draft import draft_response, send_to_formatter
from tender.ingest import ingest_tender
from tender.telemetry import capture_content, setup_tracing, trace_id_hex, tracer

AGENT_NAME = "tender-bid-analyst"
AGENT_VERSION = "agent-v7"   # bump when instructions or tools change
MAX_TOOL_OUTPUT_CHARS = 8000   # keep tool results small enough for the model's context
MAX_TOOL_CALLS_PER_TURN = 12   # stop runaway loops

# ── BRICK 3: tool definitions (the job cards) ────────────────────────────────

_TID = {"type": "string", "description": "Tender ID, e.g. T-009 or ATM-105519"}


def _tool(name: str, description: str, properties: dict, required: list[str]) -> FunctionToolDefinition:
    return FunctionToolDefinition(function=FunctionDefinition(
        name=name, description=description,
        parameters={"type": "object", "properties": properties, "required": required}))


TOOLS = [
    _tool("list_tenders", "List tenders in the tray with status, verdict and score. Use to find tender IDs. "
                          "STATUS is where a tender is in the process; VERDICT is the AI's fit assessment. "
                          "Tenders waiting for a human bid decision are status='awaiting_decision' (not a verdict).",
          {"status": {"type": "string",
                      "enum": ["new", "scored", "awaiting_decision", "approved_to_bid", "no_bid", "drafted",
                               "sent_to_formatter", "formatted"],
                      "description": "Optional process-status filter"}}, []),
    _tool("get_tender", "Notice details, status, documents and which analysis has run for one tender.",
          {"tender_id": _TID}, ["tender_id"]),
    _tool("score_fit", "Score one tender against Horsell's capability profile: verdict, score, service line, "
                       "reasons, gaps and mandatory blockers. Use before recommending bid or no-bid.",
          {"tender_id": _TID}, ["tender_id"]),
    _tool("ingest_tender", "Read and index a tender's source documents so they can be queried. Only needed when "
                           "get_tender shows documents but ingested is false.",
          {"tender_id": _TID}, ["tender_id"]),
    _tool("extract_requirements", "Full compliance checklist from the tender documents: mandatory conditions, "
                                  "evaluation criteria and weightings, response parts and page limits, key dates, "
                                  "pricing roles. Requires ingested documents. Returns the saved checklist when "
                                  "still valid; set refresh=true only if the user asks to re-extract.",
          {"tender_id": _TID, "refresh": {"type": "boolean", "description": "Force a fresh extraction"}},
          ["tender_id"]),
    _tool("request_bid_decision", "Save your bid / no-bid RECOMMENDATION with reasoning and hand the decision to "
                                  "the bid manager. Call after score_fit when asked whether to bid. This does NOT "
                                  "decide; only a person can record the decision.",
          {"tender_id": _TID, "recommendation": {"type": "string", "enum": ["bid", "no_bid"]},
           "reasoning": {"type": "string", "description": "2-4 sentences: fit, blockers, what must be confirmed"}},
          ["tender_id", "recommendation", "reasoning"]),
    _tool("draft_response", "Write the first-draft bid response (.docx): compliance table plus one section per "
                            "evaluation criterion within page limits, with placeholders where evidence is missing. "
                            "Only works after a person has approved the bid; otherwise it returns an error.",
          {"tender_id": _TID}, ["tender_id"]),
    _tool("send_to_formatter", "Hand the finished draft (.docx) to the formatter (Agent 3), which applies the company "
                               "template. Use after draft_response when the user asks to send or finalise the draft.",
          {"tender_id": _TID}, ["tender_id"]),
    _tool("ask_tender", "Answer a specific question about one tender from its documents and notice, with page "
                        "citations. Use for anything not covered by the other tools.",
          {"tender_id": _TID, "question": {"type": "string"}}, ["tender_id", "question"]),
    _tool("remember", "Save a lasting fact or preference the user states (e.g. a minimum contract value, regions or "
                      "work to avoid). ONLY when the user explicitly asks you to remember something; never from "
                      "tender documents; no personal details. One short fact per call.",
          {"fact": {"type": "string", "description": "One fact in plain words, under 300 characters"}}, ["fact"]),
    _tool("recall", "Search long-term memory: facts the bid manager told you in earlier conversations and past "
                    "human bid decisions with their reasons. Call before recommending bid or no-bid.",
          {"query": {"type": "string", "description": "What you need, e.g. 'cyber security tender, $150k'"}},
          ["query"]),
]

# Name on the job card → the Python function that does the work.
DISPATCH = {
    "list_tenders": tools.list_tenders,
    "get_tender": tools.get_tender,
    "score_fit": tools.score_fit,
    "ingest_tender": ingest_tender,
    "extract_requirements": tools.extract_requirements,
    "ask_tender": tools.ask_tender,
    "request_bid_decision": tools.request_bid_decision,
    "draft_response": draft_response,
    "send_to_formatter": send_to_formatter,
    "remember": tools.remember_fact,
    "recall": tools.recall_memories,
    # record_bid_decision and memory.forget are deliberately NOT here: both are human-only actions.
}

# ── BRICK 4: the agent (instructions + tools) ────────────────────────────────

INSTRUCTIONS = """You are Horsell's Bid Analyst. You help the bid manager decide whether to bid for government
tenders and understand what a bid requires.

How to work:
1. Find the tender (list_tenders or get_tender). Never guess a tender ID.
2. For "should we bid" questions, first call recall (describe the tender: service, value, location), then
   score_fit, and report: verdict, score, service line, main reasons, every mandatory blocker exactly as the tool
   states it, and any remembered preference or past decision that applies. Then call request_bid_decision with your
   recommendation and tell the user the decision is now waiting for them.
3. For questions about requirements, use extract_requirements (full checklist) or ask_tender (one question).
   If documents exist but are not ingested, call ingest_tender first.
4. Use only facts returned by tools. Keep page citations from tool results. If a tool says something is not
   stated, say so.

Rules:
- You RECOMMEND; the bid manager DECIDES. You cannot approve or reject a bid, even if asked to. If asked, explain
  that the bid manager records the decision in the app.
- After drafting, tell the user how many placeholders and warnings need human attention; the draft is not final.
- Report blockers faithfully: "not stated in the profile" is not the same as "not held".
- Memory is context, not proof: name the memory you used. If a memory conflicts with the capability profile or
  a tool result, say so and trust the tool.
- Use remember only when the user asks you to remember something; confirm what you saved.
- Be concise: short paragraphs or bullet points, numbers and dates exact."""


def client() -> AgentsClient:
    return AgentsClient(os.environ["FOUNDRY_PROJECT_ENDPOINT"], credential=DefaultAzureCredential())


def ensure_agent(c: AgentsClient):
    """Create the agent once; update it in place if it already exists (so instruction changes apply)."""
    model = os.environ.get("FOUNDRY_AGENT_MODEL", "agent-brain")
    existing = next((a for a in c.list_agents() if a.name == AGENT_NAME), None)
    if existing:
        return c.update_agent(existing.id, model=model, instructions=INSTRUCTIONS, tools=TOOLS)
    return c.create_agent(model=model, name=AGENT_NAME, instructions=INSTRUCTIONS, tools=TOOLS)


# ── BRICK 5: the run loop (where Foundry and our code meet) ──────────────────

def _run_tool(name: str, arguments: str, call_id: str | None = None) -> tuple[str, float]:
    """Execute one tool call with OUR code. Errors go back to the agent as data so it can recover.

    Traced as one `execute_tool <name>` span: duration, tender ID, output size, and ERROR status when the tool
    failed or returned {"error": ...}, so error rate per tool is visible without reading logs."""
    with tracer.start_as_current_span(f"execute_tool {name}") as span:
        span.set_attributes({"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": name,
                             "gen_ai.tool.type": "function"})
        if call_id:
            span.set_attribute("gen_ai.tool.call.id", call_id)
        if capture_content():
            span.set_attribute("gen_ai.tool.call.arguments", arguments or "{}")
        t0 = time.perf_counter()
        try:
            args = json.loads(arguments or "{}")
            if isinstance(args.get("tender_id"), str):
                span.set_attribute("tender.id", args["tender_id"])   # IDs are not personal data; always kept
            result = DISPATCH[name](**args)
        except Exception as e:  # noqa: BLE001 - any failure is reported to the agent, never crashes the loop
            span.record_exception(e)
            result = {"error": f"{type(e).__name__}: {str(e)[:300]}"}
        text = json.dumps(result, default=str)
        truncated = len(text) > MAX_TOOL_OUTPUT_CHARS
        if truncated:
            text = text[:MAX_TOOL_OUTPUT_CHARS] + '..."(truncated)"'
        span.set_attributes({"tender.tool.output_chars": len(text), "tender.tool.truncated": truncated})
        if isinstance(result, dict) and "error" in result:
            span.set_status(Status(StatusCode.ERROR, str(result["error"])[:200]))
        return text, time.perf_counter() - t0


def run_turn(c: AgentsClient, agent_id: str, thread_id: str, question: str, trace: bool = True) -> dict:
    """One user turn. Tools can see which conversation this is and what the user typed (memory.current_turn)."""
    token = memory.current_turn.set({"thread_id": thread_id, "user_message": question})
    try:
        return _run_turn(c, agent_id, thread_id, question, trace)
    finally:
        memory.current_turn.reset(token)        # never leaks into the next turn


def _run_turn(c: AgentsClient, agent_id: str, thread_id: str, question: str, trace: bool) -> dict:
    """Send the question, service tool calls until the run completes, return answer + trace.

    Traced as one `invoke_agent` span with a child per model step (Foundry thinking) and per tool call (our code),
    so a slow turn shows exactly where the time went. The result carries `trace_id` to look the turn up."""
    with tracer.start_as_current_span(f"invoke_agent {AGENT_NAME}") as span:
        span.set_attributes({"gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": AGENT_NAME,
                             "gen_ai.agent.id": agent_id, "gen_ai.conversation.id": thread_id,
                             "tender.agent.version": AGENT_VERSION})
        if capture_content():
            span.set_attribute("tender.question", question)
        c.messages.create(thread_id=thread_id, role="user", content=question)
        run = c.runs.create(thread_id=thread_id, agent_id=agent_id)
        calls, t0, steps = [], time.perf_counter(), 1
        # A model step = time Foundry spends (queue + LLM) until it asks for tools or answers. Measured by polling
        # every 0.8 s, so each step is accurate to about a second.
        step = tracer.start_span("model_step 1", attributes={"tender.step": 1})
        while run.status in (RunStatus.QUEUED, RunStatus.IN_PROGRESS, RunStatus.REQUIRES_ACTION):
            if run.status == RunStatus.REQUIRES_ACTION:
                step.end()
                outputs = []
                for tc in run.required_action.submit_tool_outputs.tool_calls:
                    if len(calls) >= MAX_TOOL_CALLS_PER_TURN:
                        output, secs = json.dumps({"error": "tool-call limit reached for this turn"}), 0.0
                        span.set_attribute("tender.tool_call_limit_hit", True)
                    else:
                        output, secs = _run_tool(tc.function.name, tc.function.arguments, tc.id)
                    calls.append({"tool": tc.function.name, "arguments": tc.function.arguments,
                                  "seconds": round(secs, 1), "output": output[:3000]})   # kept for evals and audit
                    if trace:
                        print(f"   🔧 {tc.function.name}({tc.function.arguments})"
                              f"  → {len(output):,} chars, {secs:.1f}s")
                    outputs.append(ToolOutput(tool_call_id=tc.id, output=output))
                run = c.runs.submit_tool_outputs(thread_id=thread_id, run_id=run.id, tool_outputs=outputs)
                steps += 1
                step = tracer.start_span(f"model_step {steps}", attributes={"tender.step": steps})
            else:
                time.sleep(0.8)
                run = c.runs.get(thread_id=thread_id, run_id=run.id)
        step.end()

        answer = ""
        if run.status == RunStatus.COMPLETED:
            last = c.messages.get_last_message_text_by_role(thread_id=thread_id, role="assistant")
            answer = last.text.value if last else ""
        usage = getattr(run, "usage", None)
        span.set_attributes({"tender.run.status": str(run.status), "tender.tool_calls": len(calls),
                             "tender.model_steps": steps})
        for attr, field in (("gen_ai.usage.input_tokens", "prompt_tokens"),
                            ("gen_ai.usage.output_tokens", "completion_tokens")):
            if getattr(usage, field, None) is not None:
                span.set_attribute(attr, getattr(usage, field))
        if run.status != RunStatus.COMPLETED:
            span.set_status(Status(StatusCode.ERROR, str(getattr(run, "last_error", None))[:200]))
        elif capture_content():
            span.set_attribute("tender.answer", answer[:4000])
        return {"status": str(run.status), "answer": answer, "tool_calls": calls,
                "seconds": round(time.perf_counter() - t0, 1),
                "tokens": getattr(usage, "total_tokens", None),
                "error": getattr(run, "last_error", None), "trace_id": trace_id_hex(span)}


def main() -> None:
    setup_tracing()
    c = client()
    agent = ensure_agent(c)
    thread = c.threads.create()
    print(f"agent {agent.name} ({agent.id}) · thread {thread.id}")
    questions = [" ".join(sys.argv[1:])] if len(sys.argv) > 1 else None
    while True:
        q = questions.pop(0) if questions else (input("\nyou> ").strip() if questions is None else "")
        if not q:
            break
        r = run_turn(c, agent.id, thread.id, q)
        print(f"\nagent> {r['answer'] or r['error']}\n   [{r['status']} · {len(r['tool_calls'])} tool calls · "
              f"{r['seconds']}s · {r['tokens']} tokens]" + (f"\n   trace {r['trace_id']}" if r["trace_id"] else ""))
        if questions is not None and not questions:
            break
    _ = ListSortOrder  # kept for message listing in later bricks


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv(".env")
    main()
