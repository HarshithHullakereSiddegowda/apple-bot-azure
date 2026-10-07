# CLAUDE.md

Guidance for Claude Code when working in this repository.

---

## 1. What this project is

**A RAG support bot for Apple devices, rebuilt on Azure to benchmark retrieval strategies.**

It answers questions from the Apple support PDF using Azure OpenAI for generation and embeddings, and
Azure AI Search for retrieval. It compares four retrieval modes (keyword, vector, hybrid, hybrid +
semantic reranker) on a golden eval set, and it is deployed on Azure Container Apps.

It is a portfolio project for AI Engineer roles in Australia. Every decision should be explainable in an
interview and backed by a number from `evals/report.md` or the logs.

Sibling project: `../apple_support_bot/` is the original AWS / LangGraph / PageIndex version. This repo
reuses its `apple-support.pdf` and `evals/datasets/golden.json`, but shares no code with it.

Next stage: **`ROADMAP.md`** (layers L0–L8: CI/CD, Key Vault, OpenTelemetry, Foundry agent, automation, Copilot Studio, AKS, capstone). Its non-negotiables apply to every change: the live app must stay up (new revision → smoke test → shift traffic).

Workflow doc (phases 0–8, checklists, code): https://claude.ai/code/artifact/0d3567c0-2f06-45d4-9ad0-94688d320708

---

## 2. Layout

```
apple-bot-azure/
├── CLAUDE.md          # this file
├── config.py          # loads env vars, builds the shared clients (aoai, index_client, search_client, embed)
├── di_ingest.py       # Document Intelligence: layout-aware text/table/figure chunks → index apple-support-di
├── ingest.py          # PDF → chunks → embeddings → index; index_pdf() is shared by the CLI and POST /upload
├── retrieval.py       # retrieve(q, mode, k, source=None) with 4 modes: keyword | vector | hybrid | hybrid_rerank
├── app.py             # FastAPI: GET / (chat page), /health, /documents, POST /ask, POST /upload
├── ratelimit.py       # sliding-window limiter + client_ip() + hashed visitor id
├── static/index.html  # chat page: API-key box (sessionStorage only), document picker, mode picker, PDF upload
├── evals/
│   ├── golden.json    # test cases: query + expected_topics (grow to 20+)
│   ├── run_evals.py   # retrieval hit rate per mode + groundedness (LLM-as-judge)
│   └── report.md      # latest eval output, committed
├── data/
│   └── apple-support.pdf
├── Dockerfile
├── .dockerignore      # excludes .env, .venv, data/, evals/
├── requirements.txt
├── .env.example       # variable names only, committed
├── .env               # real keys, NEVER committed
└── .gitignore         # must contain .env
```

**Ownership rule:** only `config.py` reads env vars and creates clients. Every other module imports from it.

---

## 3. Azure resources (fixed names, keep them consistent)

| Thing | Name | Notes |
|---|---|---|
| Region | `australiaeast` | Data residency story; use it for everything |
| Resource group | `rg-apple-rag` | Deleting it removes everything |
| AI Search service | `harshith-apple-search2` | **Free tier only** (`--sku free`). The first service (`harshith-apple-search`) was deleted: its usage counter broke and it rejected every upload with "Unable to get service usage to enforce quota". |
| Azure OpenAI resource | `harshith-apple-aoai` | Kind OpenAI, S0 |
| Search index | `apple-support-di` (live), `apple-support` (old pypdf, kept for A/B) | Set via `INDEX_NAME` |
| Chat deployment | `chat` | gpt-4.1-mini (2025-04-14), GlobalStandard, 20K TPM. Code uses the deployment name, not the model name. Not gpt-5-mini: reasoning models reject `temperature=0`. |
| Embedding deployment | `embed` | `text-embedding-3-small`, 1536 dimensions, Standard (regional), 50K TPM |
| Container App | `apple-rag` in env `apple-rag-env` | 0.5 vCPU / 1 GiB, scale 0–2 replicas. Live URL: `https://apple-rag.agreeablestone-6fb7e6d2.australiaeast.azurecontainerapps.io` |
| Image | `ghcr.io/harshithhullakeresiddegowda/apple-rag:vN` | Free registry, **public** package (contains no secrets), versioned tags. Build with `--platform linux/amd64` (this Mac is arm64). |
| Container App auth | **Managed identity** (system-assigned, principal `82fbd20c-caa5-48bc-abfe-ff24140d4da0`) | Roles: *Cognitive Services OpenAI User* on `harshith-apple-aoai`, *Search Index Data Contributor* on `harshith-apple-search2` (search `authOptions` = `aadOrApiKey`). The only secret left is `app-key` (APP_API_KEY). `config.py` uses keys when AOAI_KEY/SEARCH_KEY are set (local `.env`) and DefaultAzureCredential when they are not (cloud). Image v3. Testing locally with keys blanked proves nothing about least privilege, because your login is subscription Owner. Check the app's roles with `az role assignment list --assignee <principal>`. |
| Documents in the index | `iPhone User Guide` (418 chunks, ids `p{page}-{n}`), `iPhone Reference Guide` (276 chunks, test upload, ids `iphone-reference-guide-p{page}-{n}`) | Every chunk has a `source` field (filterable, facetable). Evals pin retrieval to `iPhone User Guide`. |

**Access model (image v5):** `/`, `/ask` and `/documents` are **public and rate-limited** (`ratelimit.py`, in-memory sliding window per visitor IP = last `X-Forwarded-For` entry, so client-supplied fake IPs are ignored). Defaults: `/ask` 10/min and 100/day per visitor, **1,000/day global** (cost ceiling ~$0.70/day); `/documents` 30/min. Override with env vars `ASK_PER_MINUTE`, `ASK_PER_DAY`, `ASK_GLOBAL_PER_DAY`, `DOCS_PER_MINUTE`. Limits are per replica (max 2, so up to 2x); Redis or Front Door would be the shared fix. `/upload` requires `X-API-Key` (`app-key` secret). Logs record `caller` as `visitor:<sha256(ip)[:12]>`, never the raw IP. Easy Auth was tried (v4) and removed: its app registration and secret were deleted. The user's resume was removed from the index before going public.

**Document Intelligence pipeline (`di_ingest.py`):** resource `harshith-apple-docintel` (S0; Free F0 only reads
the first 2 pages and 4 MB, so it can't handle this 156-page, 15 MB manual). prebuilt-layout → Markdown + figures
→ section-aware chunks: text packed to ~1,200 chars at paragraph boundaries with a `[Section]` prefix, tables kept
whole (split by rows past 3,000 chars), figures ≥ 1.2 in described by gpt-4.1-mini vision, page headers/footers
dropped. Separate index **`apple-support-di`** (461 chunks: 369 text, 6 tables, 86 figures; extra fields `section`,
`content_type`), rebuilt from empty on every run because chunk ids are positional. Cached in `data/di/` (layout JSON,
figure PNGs, descriptions), so re-runs cost $0. Full run cost ≈ $1.60 layout + ≈ $0.03 vision. **The live app and local `.env` now use `apple-support-di`** (switched via the `INDEX_NAME` env var,
revision `apple-rag--0000005`; managed-identity roles are service-wide, so no role change was needed). The old
`apple-support` index is kept for A/B: `INDEX_NAME=apple-support python evals/run_evals.py`. `iPhone Reference Guide`
(276 chunks, pypdf) was copied into the DI index. `/upload` still uses the pypdf pipeline, so uploaded chunks have
no `section`/`content_type`.
A/B on 34 questions (`evals/report_pypdf.md` vs `evals/report_di.md`): hit@5 equal (97%), hybrid_rerank MRR
0.88 → 0.95, answers equal; table/figure questions rank the table/figure card #1 in 8 of 9. Known issues: 2 of the 6
"tables" are screenshots (pages 23, 43); inline icons OCR as letters ("Home button O").
`chat` deployment raised to 100K TPM / 100 RPM: at 20K it allowed only 20 requests per minute, and vision batch jobs
hit 429.

**Deploy pipeline (Layer 1):** every push to `main` runs `.github/workflows/deploy.yml`: CI gate → image
`ghcr.io/harshithhullakeresiddegowda/apple-rag:<git-sha>` → revision `apple-rag--sha-<7>` at 0% → `scripts/smoke.sh` on the
revision URL → 100% traffic → smoke public URL → auto-rollback. The app is in **multiple-revision mode**; the previous
revision stays active at 0% for rollback. Azure login is OIDC: app `apple-rag-github-deployer` (52057a8b-…), Contributor on
`rg-apple-rag` + OpenAI User + Search Index Data Reader. Its federated subjects use GitHub's ID format
`repo:HarshithHullakereSiddegowda@100402681/apple-bot-azure@1408202525:environment:{production|evals}`; the plain
`repo:owner/name` form fails with AADSTS700213. Don't deploy by hand with `az containerapp update` any more: push to `main`
(or run the workflow manually) so every revision is traceable to a commit.

Local dev port: **8010** (`uvicorn app:app --port 8010`). Port 8000 is used by the original bot's Docker stack.
`/upload` requires header `X-API-Key: <APP_API_KEY>`; everything else is public.

Env vars (see `.env.example`): `AOAI_ENDPOINT`, `AOAI_KEY`, `SEARCH_ENDPOINT`, `SEARCH_KEY`, `INDEX_NAME`,
`APP_API_KEY`.

---

## 4. Commands

```bash
source .venv/bin/activate
python ingest.py                         # (re)build index and upload chunks; idempotent
python retrieval.py                      # compare the 4 modes on a sample question
uvicorn app:app --reload                 # local API at http://localhost:8000/docs
python evals/run_evals.py > evals/report.md

docker buildx build --platform linux/amd64 -t ghcr.io/<github-user>/apple-rag:vN --push .
az containerapp update -n apple-rag -g rg-apple-rag --image ghcr.io/<github-user>/apple-rag:vN
az containerapp logs show -n apple-rag -g rg-apple-rag --follow
```

---

## 5. Phase checklist

- [x] P0 Prep: personal Azure account, budget alert, public repo github.com/HarshithHullakereSiddegowda/apple-bot-azure (`data/` is git-ignored: copyrighted manual)
- [x] P1 Provision: resource group, AI Search Free, `chat` + `embed` deployments, `config.py`
- [x] P2 Ingest: 418 chunks from 155 pages
- [x] P3 Retrieve: all 4 modes return results
- [x] P4 Generate: `/ask` cites pages and refuses out-of-scope questions
- [x] P5 Evaluate: 25-question golden set; baseline in `evals/report_baseline.md` (vector hit@5 100%, MRR 0.95;
      hybrid_rerank 96% / 0.86; groundedness 0.99; completeness 1.00). The v2 prompt (cites document + page)
      raised citations 21/23 → 23/23, which settled Experiment A. Experiment B (default mode vector vs
      hybrid_rerank) still to run. Citation check uses `cited_pages()`, which parses "pages 132 and 135".
- [x] P6 Ship: live URL answers via `curl`, API-key auth (moved up from P7). v2 image adds the chat page at `/`,
      `/documents` and `/upload` (PDF ≤ 15 MB, ≤ 200 pages; same file name replaces the old chunks).
- [x] P7 Observe and harden: live p50 2.24 s / p95 3.63 s, generation ~90% of latency, ~1,111 tokens in / 160 out, ~$0.0007 per query (logs in `ContainerAppConsoleLogs_CL`, workspace `workspace-rgapplerag1wD4`); API key on `/ask`, `/upload`, `/documents`; managed identity, no Azure keys in the app
- [ ] P8 Package: README results, Loom, CV bullet, cleanup decision

---

## 6. Conventions and house rules

**Cost guardrails (non-negotiable):**
- Never create or suggest Basic/Standard Search tiers, large models, or a paid container registry.
  `az containerapp up --source` silently creates a paid registry, so don't use it. Push to GHCR instead.
- Keep `--max-replicas 2` and `--min-replicas 0`.
- Semantic reranker free quota is 1,000 queries/month. Count semantic calls before running evals in a loop.

**Evals drive changes:**
- Any change to chunking, `k`, the prompt, the model or the retrieval mode is followed by
  `python evals/run_evals.py`. Keep a change only if the numbers improve. Change one variable at a time.
- When a wrong answer is fixed, add that question to `evals/golden.json`.
- Debug order for a wrong answer: run `retrieval.py` first (right pages?), then look at generation.

**Code style:**
- Python 3.12, type hints, Pydantic models for request/response.
- Logs are one JSON line per event with `request_id`, `mode`, `latency_ms`, `tokens_in`, `tokens_out`.
  The Log Analytics queries depend on these field names, so don't rename them.
- `temperature=0` for generation and for the judge.

**Secrets:**
- `.env` holds real keys. Never read it into context, print it, or commit it.
- In Azure, keys are Container Apps secrets referenced with `secretref:`; managed identity is the
  stretch goal (P7c).

---

## 7. Known landmines

- **Deployment name ≠ model name.** `model="chat"` / `model="embed"` must match the Foundry deployment
  names, or you get `DeploymentNotFound`.
- **Embedding dimensions must match the index.** `vector_search_dimensions=1536` is tied to
  `text-embedding-3-small`. Changing the embedding model means re-creating the index.
- **Apple Silicon builds ARM images.** Always build with `--platform linux/amd64`, or the container
  crashes on Azure with `exec format error`.
- **Cold starts.** With scale-to-zero, the first request after idle is slow. Set `--min-replicas 1` only
  during a live demo.
- **Azure SDKs and APIs change often.** If a call fails, check the current docs for
  `azure-search-documents` and the Azure OpenAI v1 API before rewriting the code.
- **Eval limits.** Substring matching on `expected_topics` and a same-family judge model are rough
  proxies. Say so in the README; don't present them as rigorous benchmarks.
