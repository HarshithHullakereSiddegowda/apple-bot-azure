# Enterprise Roadmap: Apple Support RAG on Azure

From a working RAG demo to an **enterprise-grade Azure AI platform**, built one layer at a time.
Each layer teaches one skill that Azure-focused employers ask for, adds it to the same project, and ends with
proof you can show in an interview.

> **Head of AI's rule:** every layer must (1) keep the live app running, (2) produce a number or a demo,
> and (3) give you one resume bullet and one interview story. If a layer doesn't do all three, it isn't done.

---

## 0. Where we are (start of this roadmap)

| Area | Status |
|---|---|
| RAG: Azure OpenAI (`chat` gpt-4.1-mini, `embed`), Azure AI Search (hybrid, semantic reranker) | ✅ |
| Document Intelligence: layout-aware text, table and figure chunks (`apple-support-di`, live) | ✅ |
| Evals: 34-question golden set, hit@5 / MRR, groundedness + completeness judges | ✅ |
| Container Apps (Australia East), managed identity, public + rate-limited, upload behind API key | ✅ |
| Logs in Log Analytics (p50 2.2 s, p95 3.6 s, ~$0.0007/query) | ✅ |
| Git repo, CI/CD, Key Vault, tracing, agents, automation, AKS | ❌ ← this roadmap |

Live URL: `https://apple-rag.agreeablestone-6fb7e6d2.australiaeast.azurecontainerapps.io`

---

## 1. Non-negotiables: "the app is always running"

These rules apply to **every** layer. ELI5: we renovate the shop **while it stays open**.

| Rule | How |
|---|---|
| **Production never goes down** | Every change ships as a **new Container Apps revision**. Traffic moves only after the smoke test passes. |
| **One-command rollback** | `az containerapp ingress traffic set -n apple-rag -g rg-apple-rag --revision-weight <previous-revision>=100` |
| **Smoke test after every change** | `scripts/smoke.sh <url>` (Layer 0): `/health` 200, `/ask` answers with a citation, `/upload` without key 401 |
| **Evals before every prompt, model, chunking or index change** | `python evals/run_evals.py`; keep the change only if the numbers hold |
| **Uptime watched 24/7** | Application Insights availability test on `/health` every 5 minutes, with an email alert (Layer 3) |
| **Cost guardrails** | Budget alert (raise to **$30** for this roadmap); `ASK_GLOBAL_PER_DAY` cap; delete AKS when not demoing |
| **No secrets in code, images or git** | `.env` is git-ignored; cloud uses managed identity + Key Vault (Layer 2) |
| **Experiments get their own copy** | New index, new revision or new resource; never mutate the live one in place |

---

## 2. Order of layers (and why this order gets you hired fastest)

```
L0 Repo + smoke test ─► L1 CI/CD ─► L2 Key Vault ─► L3 OpenTelemetry ─► L4 Foundry Agent + tools
                                                                              │
L8 Enterprise capstone ◄─ L7 AKS ◄─ L6 Copilot Studio multi-agent ◄─ L5 Actions & automation
          (GitHub Copilot is used in every layer, not a separate one)
```

| # | Layer | Days | Why here | JD keywords it unlocks |
|---|---|---|---|---|
| L0 | Git repo, README, smoke test | 0.5 | CI/CD needs a repo; recruiters need a link | GitHub |
| L1 | CI/CD with eval gate | 1.5 | Every later layer ships through it safely | Azure DevOps, GitHub Actions, CI/CD |
| L2 | Key Vault | 0.5 | Small, high-signal security win | Key Vault, secrets management |
| L3 | OpenTelemetry → App Insights | 1 | You need traces **before** adding agents (agents are hard to debug blind) | Application Insights, Azure Monitor, observability |
| L4 | Foundry Agent + tools | 2 | The #1 gap in most AI Engineer JDs | Azure AI Foundry, agents, tool calling |
| L5 | Actions & automation | 1 | Agents that *do* things, not just answer | Power Automate, Logic Apps, enterprise integration |
| L6 | Copilot Studio multi-agent | 1.5 | Must-have for Microsoft-partner roles (TCS, Infosys, Accenture, Avanade) | Copilot Studio, multi-agent |
| L7 | AKS | 1.5 | Last because it costs money while running | AKS, Kubernetes, Helm, workload identity |
| L8 | Enterprise capstone | 1 | Ties it together for interviews | Private Link, VNET, governance, cost |

**Total: about 10–11 focused days.** Keep applying while building. After L1 and L4, your profile is already much
stronger, so don't wait for L8 to apply.

---

## L0. Repo, README, smoke test (½ day)

**ELI5:** put the project in a shop window (GitHub) with a clear label (README), and make a 10-second
"is the shop open?" check (smoke test).

**Build**
- [ ] `git init`, a first commit, and a GitHub repo `apple-bot-azure`. Confirm `.env` and `data/di/figures_*` are ignored or acceptable to publish.
- [ ] `scripts/smoke.sh URL`: health 200 · `/ask` returns an answer containing "page" · `/upload` without key returns 401 · exits non-zero on any failure
- [ ] `README.md`: one-line pitch, architecture diagram, eval table (pypdf vs DI), live numbers (p50/p95/cost), how to run, known limits
- [ ] `.github/copilot-instructions.md`: house rules for GitHub Copilot (it mirrors `CLAUDE.md`)

**Done when:** the repo is public, the README shows real numbers, and `scripts/smoke.sh <live-url>` passes.

**Interview line:** *"Every deploy is verified by a smoke test that checks health, a grounded answer and an auth boundary."*

---

## L1. CI/CD with an eval gate and zero-downtime deploys (1.5 days)

**ELI5:** a robot that, on every code change, checks the work (tests + evals), builds the lunchbox, puts it next
to the old one, tastes it (smoke test), and only then swaps it in. If anything fails, customers never notice.

**Pipeline design** (GitHub Actions first because it starts instantly; then the same stages in Azure DevOps YAML):
```
PR:    lint (ruff) → unit tests (pytest) → offline checks → build image (no push)
main:  build linux/amd64 → push ghcr.io/...:<git-sha>
       → deploy NEW revision with 0% traffic  (multiple-revision mode)
       → smoke test the revision's own URL
       → shift 100% traffic → keep the previous revision for instant rollback
nightly: live evals (34 questions) → fail + alert if hit@5, MRR or groundedness drop vs baseline
```

**Build**
- [ ] Switch the app to **multiple-revision mode**, which enables blue/green (`az containerapp revision set-mode --mode multiple`)
- [ ] **Keyless pipeline auth:** OIDC / workload identity federation from GitHub to Azure. No Azure secrets stored in GitHub. Give the pipeline identity the least role it needs (Contributor on `rg-apple-rag` only).
- [ ] `tests/`: unit tests for `ratelimit.py`, `cited_pages()`, `_split_table()`, `client_ip()`
- [ ] Eval gate: offline (free) on every PR; live evals nightly, compared against a committed baseline
- [ ] Image tags = **git SHA** (traceable), not `v5`
- [ ] **Azure DevOps version:** `azure-pipelines.yml` with the same stages, a service connection using workload identity federation, and Environments with an approval check before production. Note: new Azure DevOps organizations may need to request the free hosted parallel job, so check the current policy and start that request on day 1.

**Done when:** merging a one-line change deploys automatically with zero failed requests, and a deliberately broken
build is blocked before production.

**Interview line:** *"CI runs tests and an offline eval gate; CD deploys a new revision at 0% traffic, smoke-tests
it, then shifts traffic, so rollback is one command. Pipeline auth is OIDC federation with no stored secrets."*

---

## L2. Key Vault (½ day)

**ELI5:** a bank vault for passwords. The app proves who it is with its badge (managed identity) and reads the
password from the vault at startup. Nobody copies passwords around anymore.

**Build**
- [ ] Create a Key Vault in **RBAC mode** in `rg-apple-rag`
- [ ] Store `APP_API_KEY` (and, for tooling, the DI key) as secrets
- [ ] Give the app's managed identity **Key Vault Secrets User** (read-only, secrets only)
- [ ] Change the Container App secret `app-key` to a **Key Vault reference** (`keyvaultref:<secret-uri>,identityref:system`)
- [ ] Rotate `APP_API_KEY` in Key Vault and confirm the app picks it up after a restart. Rotation is now a vault operation, not a redeploy.

**Done when:** no secret values are stored directly in the Container App, and rotation is tested.

**Interview line:** *"Services use managed identity; the one app secret lives in Key Vault, referenced by the
container, with read-only RBAC and a tested rotation."*

---

## L3. OpenTelemetry → Application Insights (1 day)

**ELI5:** today we have receipts (log lines). Now we add CCTV: every request becomes a timeline you can click
through (retrieve → embed → search → generate), like LangSmith, but your data stays in your Azure tenant.

**Build**
- [ ] Create Application Insights (workspace-based, same Log Analytics workspace)
- [ ] `azure-monitor-opentelemetry` with `configure_azure_monitor()` at startup; auto-instrument FastAPI + HTTP
- [ ] Manual spans: `retrieve` (mode, k, source, hit pages), `generate` (model, tokens in/out), `upload` (chunks, seconds)
- [ ] Use the OpenTelemetry **GenAI semantic conventions** for model spans (`gen_ai.*` attributes). Don't record prompt text by default (privacy); record it only behind a flag.
- [ ] **Alerts:** p95 > 8 s, 5xx rate > 2%, 429s spike, and an availability test on `/health` every 5 minutes
- [ ] A **workbook/dashboard:** requests, p50/p95, tokens, cost per day, top questions refused
- [ ] Connect App Insights to your **Foundry project** so traces show up in Foundry's tracing view

**Done when:** you can open one slow request and see which span was slow, and the uptime alert emails you when
you stop the app on purpose.

**Interview line:** *"Every request is an OpenTelemetry trace in Application Insights with GenAI conventions, so I can
see retrieval vs generation time per request; alerts cover latency, errors, throttling and uptime."*

---

## L4. Azure AI Foundry Agent with tools (2 days)

**ELI5:** today the bot is a librarian who only reads books. An agent is an assistant who can also use tools: look
something up, check a status, open a ticket. It decides which tool to use, step by step.

**Build**
- [ ] Create a **Foundry project**; connect your existing Azure OpenAI and Azure AI Search resources
- [ ] Create an agent (Foundry Agent Service) with tools:
  - **Azure AI Search tool** → `apple-support-di` (knowledge)
  - **Function tool** `create_support_ticket(summary, device, severity)` → calls your API (L5 wires it to real automation)
  - **Function tool** `check_warranty(serial)` → a mock endpoint (shows input validation + PII handling)
- [ ] Add `POST /agent` to the FastAPI app (behind the API key + rate limit). It runs the agent thread and returns the answer + tool calls made.
- [ ] Safety: Azure OpenAI **content filters** + **Prompt Shields** (jailbreak / indirect injection); max tool calls per run; refuse tool calls with missing fields
- [ ] **Agent evals:** 10 scenarios measuring tool-choice accuracy (did it call the right tool with the right arguments?), task success and groundedness. Add them to the nightly evals.

**Done when:** "My iPhone won't charge, please open a ticket" leads to search → answer → `create_support_ticket` with
correct arguments, visible as spans in App Insights.

**Interview line:** *"I built a Foundry agent with a search tool and function tools, guarded by content filters and
Prompt Shields, and I evaluate it on tool-call accuracy, not just answer quality."*

---

## L5. Actions & automation: Power Automate / Logic Apps (1 day)

**ELI5:** the agent decides "open a ticket"; automation actually does it: writes the ticket, emails the user,
posts to Teams.

**Build**
- [ ] **Power Automate** flow with an HTTP trigger → add a row to a SharePoint list "Support tickets" → send an email/Teams message. Note: the HTTP trigger is a premium connector, so use the **Power Apps Developer Plan** (free developer environment) or a trial; check current licensing.
- [ ] Azure-native alternative: the same flow in **Logic Apps (Consumption)**, which costs pennies at demo volume. Know both and when to pick each.
- [ ] `create_support_ticket` calls the flow URL, stored in **Key Vault**, never in code
- [ ] Idempotency: pass a `request_id` so a retried tool call doesn't create two tickets
- [ ] Trace the automation call as a span (L3)

**Done when:** a chat message creates a real SharePoint row + notification, once, even if the call is retried.

**Interview line:** *"Agent tool calls trigger Power Automate flows for enterprise actions, with idempotency keys so
retries never duplicate tickets, and the flow URL kept in Key Vault."*

---

## L6. Copilot Studio multi-agent (1.5 days)

**ELI5:** the same assistant, built in Microsoft's low-code studio and published where employees already are
(Teams), with a "manager" agent that hands work to specialist agents.

**Build**
- [ ] Copilot Studio (trial / developer environment): agent **"Apple Support Desk"**
- [ ] **Knowledge:** connect Azure AI Search `apple-support-di` (your index, your chunking)
- [ ] **Actions:** the L5 Power Automate flow (create ticket)
- [ ] **Multi-agent:** an orchestrator plus connected agents, **Troubleshooting** (knowledge) and **Ticketing** (action). Optionally connect your **Foundry agent** from L4 as a connected agent (check current support).
- [ ] Publish to **Teams** (test tenant) and a web demo page
- [ ] Compare: pro-code (FastAPI + Foundry) vs low-code (Copilot Studio). When would you use each? Write this down; it's a classic consulting interview question.

**Done when:** in Teams, "my phone keeps dying, raise a ticket" is routed troubleshooting → ticketing, and a ticket appears.

**Interview line:** *"I delivered the same capability pro-code (Foundry + FastAPI) and low-code (Copilot Studio
multi-agent in Teams), sharing one governed search index and one automation layer."*

---

## L7. AKS (1.5 days, then delete)

**ELI5:** Container Apps is a serviced apartment; AKS is renting the whole building: more control, more chores.
We prove you can run the same app there, then hand the keys back to save money.

**Build**
- [ ] First on a local `kind` cluster ($0): Deployment, Service, HPA, readiness/liveness probes on `/health`, ConfigMap, resource requests/limits
- [ ] Package as a **Helm chart** (`deploy/helm/apple-rag`)
- [ ] AKS: smallest practical node pool, **workload identity** (federated credential, same roles as the Container App identity), **Key Vault CSI driver** for secrets, ingress (App Routing add-on), Container Insights
- [ ] Pipeline stage (L1) that deploys the Helm chart to AKS on demand
- [ ] Run, smoke test, record a 2-minute demo, then **delete the cluster** (nodes cost money every hour)
- [ ] Write the trade-off table: Container Apps vs AKS (ops effort, cost, control, scaling, networking)

**Done when:** the same image runs on AKS with workload identity and passes `scripts/smoke.sh`, and the cluster is deleted afterwards.

**Interview line:** *"Production runs on Container Apps; I also deploy the same image to AKS via Helm with workload
identity and Key Vault CSI, and I can explain when AKS's control justifies its operational cost."*

---

## L8. Enterprise capstone (1 day)

**ELI5:** the "how a bank would run this" chapter: private networking, governance and cost, on paper and in a
diagram, because building all of it costs money a demo doesn't need.

**Deliver**
- [ ] **Target architecture diagram:** VNET; private endpoints for OpenAI, AI Search, Key Vault, Storage; public network access disabled; Container Apps in a VNET-integrated environment; Front Door + WAF for public entry; no keys anywhere
- [ ] **Architecture Decision Records** (`docs/adr/`): vector vs hybrid default, DI vs pypdf, Container Apps vs AKS, Easy Auth removed vs rate limit, LangSmith vs App Insights
- [ ] **Governance:** content filters, Prompt Shields, audit logs (who asked what, hashed), data retention, PII handling, Azure Policy ideas (deny public endpoints)
- [ ] **Cost model:** per-query cost, monthly at 1k / 10k / 100k queries, what changes at each tier
- [ ] **Runbook:** rollback, key rotation, reindex, incident steps

**Done when:** you can whiteboard the production version in 10 minutes and answer "what would you change for a bank?"

---

## GitHub Copilot (every layer)

**ELI5:** a pair programmer in your editor. Interviewers want to hear *how* you use it safely, not just that you have it.

- [ ] Install GitHub Copilot (the Free tier is enough) in VS Code; add `.github/copilot-instructions.md`
- [ ] Use it for: unit tests (L1), Helm/YAML (L1, L7), KQL queries (L3), docstrings and README
- [ ] Rules: review every suggestion, never paste secrets into prompts, run tests and evals before committing
- [ ] Keep a short log of where it saved time and where it was wrong. That's your interview story.

**Interview line:** *"I use GitHub Copilot for scaffolding tests and infrastructure YAML, with repo-level instructions;
everything still goes through tests, evals and review before merge."*

---

## Cost plan (estimates; check current Azure pricing)

| Layer | Extra cost | How to keep it low |
|---|---|---|
| L0–L2 | ~$0 | Free tiers; Key Vault operations cost cents |
| L3 | ~$0–2/month | App Insights first 5 GB/month free; sample traces |
| L4 | cents per run | gpt-4.1-mini; cap tool calls |
| L5 | $0 | Power Apps Developer Plan / Logic Apps Consumption |
| L6 | $0 during trial | Copilot Studio trial; check message limits |
| L7 | **a few dollars per day while running** | Local `kind` first; create AKS only for the demo; delete the same day |
| L8 | $0 | Design on paper, don't deploy private endpoints |

Raise the budget alert to **$30** for this roadmap. Check spend weekly: Cost Management → Cost analysis by resource.

---

## Job plan running alongside the build

| When | Do |
|---|---|
| Every day | 3–5 tailored applications (Azure-heavy roles first: consultancies, banks, government suppliers) |
| After L1 | Update resume: CI/CD with eval gates, zero-downtime deploys |
| After L3 | Write a LinkedIn post: "Tracing a RAG app with OpenTelemetry on Azure" (with a screenshot) |
| After L4–L5 | Record a 2-minute Loom: question → answer → ticket created. Put it at the top of the README. |
| After L6 | Apply to Microsoft-partner roles (TCS, Infosys, Accenture, Avanade, Wipro, DXC) and lead with Copilot Studio |
| After L7 | Add AKS to your resume with the trade-off story |
| Weekly | 2 mock interviews: one system design ("design an enterprise RAG on Azure"), one project deep-dive |

**Resume bullets to unlock (fill in real numbers as you go):**
- Built CI/CD with an LLM eval gate and blue/green revision deploys on Azure Container Apps (OIDC, zero stored secrets).
- Instrumented a RAG service with OpenTelemetry in Application Insights; alerting on p95 latency, errors and uptime.
- Built an Azure AI Foundry agent with search and function tools wired to Power Automate, evaluated on tool-call accuracy.
- Delivered a Copilot Studio multi-agent assistant in Teams over the same governed Azure AI Search index.
- Deployed the service to AKS with Helm, workload identity and Key Vault CSI; documented the Container Apps vs AKS trade-offs.

---

## Progress tracker

- [x] L0 Repo + README + smoke test: github.com/HarshithHullakereSiddegowda/apple-bot-azure (public)
- [ ] L1 CI/CD + eval gate + blue/green
- [ ] L2 Key Vault
- [ ] L3 OpenTelemetry + alerts + uptime test
- [ ] L4 Foundry agent + tools + agent evals
- [ ] L5 Power Automate / Logic Apps actions
- [ ] L6 Copilot Studio multi-agent in Teams
- [ ] L7 AKS (Helm, workload identity), then deleted
- [ ] L8 Enterprise capstone: diagram, ADRs, runbook, cost model
- [ ] GitHub Copilot used and documented across layers
