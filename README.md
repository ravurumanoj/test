# Relationship Manager Agentic RAG — POC

A backend-only FastAPI application that uses the **Unique AI SDK and Unique Toolkit** to
answer relationship-manager questions through a fully-managed agentic loop. Every
underlying data API (Portfolio and CRM) is exposed to the LLM as its own tool, so the
model decides which data to fetch — nothing is hardcoded.

It can be driven two ways:

- **REST** — `POST /relationship-manager/query` (direct API usage / testing).
- **Unique AI space webhook** — `POST /relationship-manager/webhook` (integrate the app
  as an external module inside a Unique AI space).

---

## What it does

- Accepts a natural-language question via REST **or** a Unique AI webhook event.
- Runs an iterative orchestrator loop that plans, dispatches granular tool calls, and
  finalises an answer — aligned with the Unique Toolkit orchestrator pattern.
- Exposes **8 granular data tools** (4 Portfolio + 4 CRM) to the LLM; the model selects
  the right one(s) per question (e.g. a "performance" question calls
  `portfolio_performance`, not a generic snapshot).
- Wires all five Unique Toolkit managers: **HistoryManager**, **ReferenceManager**,
  **DebugInfoManager**, **EvaluationManager**, and **PostprocessorManager**.
- Optionally discovers and calls **MCP** tools at the orchestrator level (LLM-driven).
- Loads and persists conversation history through the Unique **chat** APIs.
- Applies structured logging (console + rotating file) to every manager action, tool
  call, and iteration so every step is visible.

---

## Architecture

```
POST /relationship-manager/query          POST /relationship-manager/webhook
              │                                        │ (verify HMAC, parse event)
              └───────────────┬────────────────────────┘
                              ▼
                RelationshipManagerOrchestrator
  ├─ HistoryManager       — token-window-aware conversation history (Loop Token Reducer)
  ├─ ReferenceManager     — content-chunk extraction and sequential citation numbering
  ├─ DebugInfoManager     — per-tool debug trace store
  ├─ EvaluationManager    — FinancialSafetyEvaluation (compliance check)
  └─ PostprocessorManager — FinancialDisclaimerPostprocessor (response enrichment)
                              │
                              ▼ plan_with_tools → unique_sdk.ChatCompletion.create
                              │  (LLM chooses which tools to call)
        ┌────────────────┬────┴───────────┬────────────────┐
        ▼                ▼                ▼                 ▼
 portfolio_snapshot  portfolio_        crm_profile     mcp__<tool>   ← optional, LLM-driven
 portfolio_performance  compliance     crm_interactions
 portfolio_book_summary                crm_advisory / crm_book_summary
        │                                │
   PortfolioTools                     CrmTools           ← in-process, JSON-backed
        │                                │
   portfolio.json                     crm.json
```

Each tool is a `DataQueryTool` (subclass of the abstract `Tool`) that wraps exactly **one**
data method and returns a `ToolCallResponse` with:
- `content` — LLM-generated summary (sent to HistoryManager)
- `content_chunks` — referenceable data sections (sent to ReferenceManager)
- `debug_info` — execution metadata (sent to DebugInfoManager)

> The data tools call `PortfolioTools` / `CrmTools` **methods directly (in-process)** — they
> do not call the REST endpoints over HTTP. The REST routes and the tools are two
> independent entry points to the same methods.

---

## The 8 granular tools

| Tool | Domain | Underlying method | Purpose |
|---|---|---|---|
| `portfolio_snapshot` | portfolio | `get_portfolio_snapshot` | Holdings + asset allocation + P&L |
| `portfolio_performance` | portfolio | `get_performance_view` | Returns, risk metrics, sector/geo exposure, events |
| `portfolio_compliance` | portfolio | `get_compliance_view` | Line of credit, tax summary, alerts |
| `portfolio_book_summary` | portfolio | `get_all_portfolios_summary` | Book-of-business overview (all customers) |
| `crm_profile` | crm | `get_customer_full_profile` | Demographics + account metadata + RM |
| `crm_interactions` | crm | `get_interactions` | Conversation history + open service requests (filterable) |
| `crm_advisory` | crm | `get_advisory_view` | Suggestions + compliance flags + alerts |
| `crm_book_summary` | crm | `get_all_customers_summary` | Pipeline overview (all customers) |

Tools are built by `build_portfolio_tools()` and `build_crm_tools()` and passed to the
orchestrator as a flat list. Each tool's `domain` drives `AgentAnswer` grouping and the
`routing_decision`.

---

## Project structure

```text
src/app/
    main.py              — FastAPI application bootstrap + tool/orchestrator wiring
    settings.py          — environment-driven Settings dataclass
    logging_config.py    — structured logging (console + rotating file)
    errors.py            — AppError hierarchy (ValidationError, DataAccessError, …)
    schemas.py           — Pydantic schemas: Unique Toolkit types, API + webhook schemas
    api/
        routes.py            — orchestrator query, webhook, health, models endpoints
        portfolio_routes.py  — 4 read-only Portfolio GET endpoints
        crm_routes.py        — 4 read-only CRM GET endpoints
    agents/
        base_tool.py         — abstract Tool + BaseToolConfig (has `domain`)
        data_query_tool.py   — generic DataQueryTool + DataQuerySpec (wraps one data API)
        portfolio_agent.py   — build_portfolio_tools() factory (4 portfolio tools)
        crm_agent.py         — build_crm_tools() factory (4 CRM tools)
        mcp_tool_wrapper.py  — McpToolWrapper — exposes each MCP tool to the LLM
        prompts.py           — system prompt strings for summarisation
        relationship_manager.py — RelationshipManagerOrchestrator (all 5 managers)
    services/
        managers.py          — 5 managers + FinancialSafetyEvaluation + FinancialDisclaimerPostprocessor
        unique_client.py     — UniqueAIClient (wraps unique_sdk.ChatCompletion.create)
        unique_toolkit.py    — UniqueToolkit facade (tries unique_toolkit, falls back to SDK)
        session_service.py   — UniqueSessionService (history load/save + webhook helpers)
        mcp_manager.py       — McpManager (JSON-RPC over Streamable HTTP)
        crm_tools.py         — CrmTools (4 query methods + get_customer_crm)
        portfolio_tools.py   — PortfolioTools (4 query methods)
        data_loader.py       — JsonDataLoader + BaseDataTools (shared base)
    data/
        portfolio.json       — 5 sample customers (full finance/banking detail)
        crm.json             — 5 sample customers (full CRM history)
tests/
    test_app.py          — API-level integration tests
    test_health.py       — health endpoint test
    test_orchestrator.py — orchestrator unit tests (monkeypatched completions)
```

---

## Agentic loop flow

1. **History load** — prior turns are loaded from Unique AI for the session/chat (or from
   `chat_history` in the request as a fallback).
2. **Fresh session check** — `HistoryManager.has_no_loop_messages()` detects a new thread.
3. **History seeding** — system prompt (with per-tool hints) and user question added.
4. **Planning step** — `UniqueToolkit.plan_with_tools()` calls
   `unique_sdk.ChatCompletion.create` with the 8 tool definitions (+ any `mcp__*` tools).
   The LLM decides which tool(s) to call.
5. **Tool execution** — tool calls are deduplicated, capped per iteration, and executed
   concurrently via `asyncio.gather`. Every triggered tool is logged.
6. **Manager update** — HistoryManager records tool-call + result messages,
   ReferenceManager extracts `content_chunks`, DebugInfoManager harvests `debug_info`.
7. **Control / last iteration** — a `takes_control()` tool exits early; the final
   iteration disables tools and forces an answer.
8. **Post-loop** — EvaluationManager runs `FinancialSafetyEvaluation`; PostprocessorManager
   applies `FinancialDisclaimerPostprocessor`.
9. **Persistence** — for the REST path the turn is saved back to Unique AI (the webhook
   path writes the assistant reply into the pre-created placeholder instead).

> **Deterministic safety net:** the Unique `api_version 2023-12-06` cannot force
> `tool_choice="required"`. If the LLM returns zero tool calls on the first pass, a
> keyword router selects the specific granular tool(s) so data is always fetched. This is
> a last-resort fallback only.

---

## Install with uv

```powershell
uv venv
.venv\Scripts\activate
uv pip install -e .
uv pip install -e ".[unique]"
uv pip install -e ".[dev]"
```

---

## Environment variables

Copy `.env.example` and fill in your Unique credentials before running non-test workloads.

### Core

| Variable | Required | Default | Description |
|---|---|---|---|
| `UNIQUE_APP_ID` | Yes | — | Unique application ID |
| `UNIQUE_APP_KEY` | Yes | — | Unique application key (`UNIQUE_API_KEY` also accepted as alias) |
| `UNIQUE_AUTH_COMPANY_ID` | Yes | — | Default company ID for SDK calls (overridable per webhook event) |
| `UNIQUE_AUTH_USER_ID` | Yes | — | Default user ID for SDK calls (overridable per webhook event) |
| `UNIQUE_MODEL_NAME` | No | `AZURE_GPT_4o_2024_1120` | LLM model identifier |
| `UNIQUE_API_BASE_URL` | No | — | Override SDK base URL |
| `UNIQUE_API_VERSION` | No | `2023-12-06` | SDK API version |
| `UNIQUE_AGENT_MAX_ITERATIONS` | No | `3` | Maximum orchestrator loop iterations |
| `UNIQUE_MAX_TOOL_CALLS_PER_ITERATION` | No | `3` | Tool call cap per iteration |
| `UNIQUE_MAX_HISTORY_TOKENS` | No | `6000` | Token budget for HistoryManager Loop Token Reducer |

### Session / history & webhook

| Variable | Required | Default | Description |
|---|---|---|---|
| `UNIQUE_ASSISTANT_ID` | For history/webhook | — | Unique assistant (space) ID; required to create chats and persist messages |
| `UNIQUE_DEFAULT_SESSION_ID` | No | `poc-demo-session-001` | chatId used when a request omits `session_id`. Set to a real `chat_...` id to keep history across restarts |
| `UNIQUE_WEBHOOK_ENDPOINT_SECRET` | For webhook | *(empty)* | HMAC secret Unique signs webhooks with. **Empty skips verification (POC only)** |
| `UNIQUE_DEFAULT_CUSTOMER_ID` | No | `CUST-1001` | customer_id fallback when a webhook event carries no `configuration.customerId` |

### Logging

| Variable | Required | Default | Description |
|---|---|---|---|
| `LOG_LEVEL` | No | `DEBUG` | Logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `LOG_FILE` | No | `logs/app.log` | Rotating log file path. Empty disables file logging (console only) |
| `LOG_MAX_BYTES` | No | `10485760` | Rotate when the file reaches this size (10 MB) |
| `LOG_BACKUP_COUNT` | No | `5` | Number of rotated files to keep |

### App

| Variable | Required | Default | Description |
|---|---|---|---|
| `APP_NAME` | No | `relationship-manager-agentic-rag-poc` | Application name |
| `APP_ENV` | No | `local` | Environment label |
| `ENV_FILE` | No | `/usr/local/config/.env` | dotenv file path; the repository `.env` is used as a local fallback |

MCP variables are documented in the [MCP integration](#mcp-integration-orchestrator-level) section.

---

## Run

```powershell
uv run uvicorn app.main:app --reload --app-dir src
```

For production, the application loads `/usr/local/config/.env` automatically. To expose
the service outside the local machine, bind Uvicorn to all interfaces:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --app-dir src
```

After the server starts:

- **Health check:** `GET /health`
- **Models:** `GET /models`
- **Orchestrator query:** `POST /relationship-manager/query`
- **Unique webhook:** `POST /relationship-manager/webhook`
- **Portfolio data:** `GET /portfolio/` · `GET /portfolio/{id}` · `/performance` · `/compliance`
- **CRM data:** `GET /crm/` · `GET /crm/{id}` · `/interactions` · `/advisory`

Example orchestrator request:

```json
{
    "customer_id": "CUST-1001",
    "question": "Share portfolio and CRM summary for this customer",
    "session_id": ""
}
```

Optional request fields: `session_id` (Unique chatId for history), `chat_history`
(fallback turns), `persist_turn` (default `true`), and `auth_user_id` /
`auth_company_id` / `assistant_id` (identity overrides; the webhook sets these).

Example response shape:

```json
{
    "customer_id": "CUST-1001",
    "question": "...",
    "routing_decision": ["portfolio", "crm"],
    "final_answer": "...",
    "agent_answers": [
        { "agent_name": "portfolio", "tool_name": "portfolio_snapshot", "summary": "...", "retrieved_context": {} }
    ],
    "debug_info": { "total_iterations": 2, "triggered_tool_names": ["portfolio_snapshot", "crm_profile"], "reference_chunk_count": 8 },
    "evaluation_results": [{ "name": "financial_safety", "is_positive": true, "value": "PASS", "reason": "..." }]
}
```

---

## Unique AI webhook (space integration)

The app can be integrated as an **external module** inside a Unique AI space. When the
module is chosen, Unique posts an event to `POST /relationship-manager/webhook`.

Flow (`unique.chat.external-module.chosen`):

1. **Verify** the HMAC signature via `unique_sdk.Webhook.construct_event` using
   `X-Unique-Signature` + `X-Unique-Created-At` and `UNIQUE_WEBHOOK_ENDPOINT_SECRET`.
2. **Parse** the event: `payload.userMessage.text`, `payload.chatId`,
   `payload.assistantMessage.id`, `event.userId`, `event.companyId`, `payload.assistantId`.
3. **Run** the orchestrator with `session_id = chatId` (a real Unique chat, so history
   loads natively) and `persist_turn=False`.
4. **Write** the answer back by updating the pre-created assistant message placeholder via
   `unique_sdk.Message.modify`.
5. **Always return HTTP 200** — Unique marks any non-2xx/404 delivery as expired.

`unique.chat.user-message.created` is also accepted. The endpoint always returns 200 with a
JSON body indicating whether the event was handled.

### Identity precedence (client event → env var)

For webhook-driven requests, identity comes from the **event** and falls back to env vars
when absent:

| Value | From event | Fallback env var |
|---|---|---|
| user id | `event.userId` | `UNIQUE_AUTH_USER_ID` |
| company id | `event.companyId` | `UNIQUE_AUTH_COMPANY_ID` |
| assistant id | `payload.assistantId` | `UNIQUE_ASSISTANT_ID` |
| customer id | `payload.configuration.customerId` | `UNIQUE_DEFAULT_CUSTOMER_ID` |

The **API key stays env-only** (a secret, never carried in the event). The `/query` REST
path sends no overrides, so it always uses the env vars.

---

## Conversation history persistence

History is stored via the Unique **chat** APIs (`UniqueSessionService`):

- A `session_id` maps to a real Unique `chatId`. Ids starting with `chat_` are used as-is;
  anything else is bound (once per process) to a chat created via
  `unique_sdk.Space.create_chat` and cached in memory.
- Load: `unique_sdk.Message.list`. Save: `unique_sdk.Message.create` (USER then ASSISTANT).
- Requires `UNIQUE_ASSISTANT_ID`. On first run the created `chat_...` id is logged — set
  `UNIQUE_DEFAULT_SESSION_ID` to it to keep the same thread across restarts.

> A made-up chatId that does not exist in Unique's DB returns `404 Chat not found`; that is
> why chats must be created before messages are written.

---

## Logging

Structured logging is configured once at startup ([logging_config.py](src/app/logging_config.py)):

- **Console** handler (stdout) — always on.
- **Rotating file** handler — enabled when `LOG_FILE` is set (default `logs/app.log`),
  size-based rotation via `LOG_MAX_BYTES` / `LOG_BACKUP_COUNT`.

Every tool trigger is logged (`TOOL TRIGGERED — <name>`, plus `>>> TOOL CALL INPUT` /
`<<< TOOL CALL OUTPUT`), and the orchestrator records `debug_info["triggered_tool_names"]`.
The `logs/` directory is git-ignored.

---

## Run tests

```powershell
python -m pytest tests/ -v
```

All tests use a monkeypatched `UniqueAIClient.create_completion` — no live Unique
credentials needed.

---

## Sample customer IDs

| ID | Name | Segment | Risk Profile |
|---|---|---|---|
| CUST-1001 | Rajesh Kumar | HNI — Private Banking | Moderate |
| CUST-2002 | Priya Sharma | Affluent — Retail | Conservative |
| CUST-3003 | Ananya Reddy | Ultra-HNI — Discretionary | Aggressive |
| CUST-4004 | Arjun Mehta | Mass Affluent — Young Professional | Moderate-Aggressive |
| CUST-5005 | Kavita Nambiar | HNI — NRI Wealth (UAE) | Moderate |

---

## Portfolio API — `/portfolio`

All endpoints are read-only GET. (These REST routes share the same methods the
`portfolio_*` tools call.)

| Endpoint | Description |
|---|---|
| `GET /portfolio/` | All customers — AUM, YTD return, alpha, alerts (RM dashboard view) |
| `GET /portfolio/{id}` | Holdings + asset allocation + full P&L for one customer |
| `GET /portfolio/{id}/performance` | Returns, risk metrics (Sharpe/Sortino/alpha), sector & geo exposure, upcoming events |
| `GET /portfolio/{id}/compliance` | Line of credit, tax summary (STCG/LTCG), and active alerts |

Returns `HTTP 404` when the customer ID is not found.

---

## CRM API — `/crm`

All endpoints are read-only GET.

| Endpoint | Description |
|---|---|
| `GET /crm/` | All customers — segment, NPS, churn risk, pending follow-ups (pipeline view) |
| `GET /crm/{id}` | Full profile — contact details, account metadata, relationship manager info |
| `GET /crm/{id}/interactions` | Conversation history (filterable by `channel`, `sentiment`, `limit`) + open service requests |
| `GET /crm/{id}/advisory` | Suggestions (all + pending), compliance flags, and action alerts |

**Query parameters for `/interactions`:**

| Parameter | Type | Description |
|---|---|---|
| `channel` | string | `phone` \| `email` \| `in_person` \| `video_call` \| `app_chat` |
| `sentiment` | string | `positive` \| `neutral` \| `negative` |
| `limit` | integer 1–50 | Cap on the number of conversations returned |

Returns `HTTP 404` when the customer ID is not found.

---

## Unique SDK / Toolkit alignment

| Aspect | Implementation |
|---|---|
| SDK package | `unique-sdk` — imported as `unique_sdk` |
| Toolkit package | `unique_toolkit` — used for `LanguageModelService` when installed |
| SDK configuration | `unique_sdk.api_key`, `unique_sdk.app_id` set before every call |
| LLM completion | `unique_sdk.ChatCompletion.create` with OpenAI-style messages |
| Tool pattern | `Tool` abstract class (mirrors `unique_toolkit.agentic.tools.tool.Tool`) |
| Tool output | `ToolCallResponse` with `content`, `content_chunks`, `debug_info`, `error_message` |
| History management | `HistoryManager` with Loop Token Reducer + Unique chat persistence |
| Citation tracking | `ReferenceManager.extract_referenceable_chunks()` |
| Quality checks | `EvaluationManager` + `Evaluation` abstract class |
| Response enrichment | `PostprocessorManager` + `Postprocessor` abstract class |
| Debug exposure | `DebugInfoManager.add() / get()` |
| Webhooks | `unique_sdk.Webhook.construct_event`, `Message.modify`, `Space.create_chat` |

---

## MCP integration (orchestrator level)

The app can discover and call tools from an external **MCP (Model Context Protocol)**
server. This is the **standard, LLM-driven** MCP pattern: tools are discovered from the
live server, their `inputSchema` is exposed to the LLM verbatim, and the **LLM generates
the arguments** — no hardcoding.

MCP is handled by the **orchestrator**, not the CRM agent. It is **fully optional**: when
no MCP server URL is configured, the app behaves exactly as described above.

### How it works

```
RelationshipManagerOrchestrator (first request)
    │  _ensure_mcp_tools_loaded()
    │     ├─ McpManager.list_tools()   → tools/list  (discover ALL tools)
    │     └─ wrap each as McpToolWrapper (name: mcp__<toolName>)
    ▼
Planning step exposes mcp__* tools to the LLM alongside the 8 data tools
    │  LLM decides which to call and produces schema-compliant arguments
    ▼
McpToolWrapper.run() → McpManager.call_tool() → tools/call
    │  result flows into history + ReferenceManager (content_chunks)
    ▼
ToolCallResponse (content + content_chunks + debug_info)
```

`McpManager` ([mcp_manager.py](src/app/services/mcp_manager.py)) speaks JSON-RPC 2.0 over
the **Streamable HTTP** transport using only the standard library (`urllib`) — **no new
runtime dependency**. It handles `application/json` and `text/event-stream` (SSE) and
performs the MCP handshake (`initialize` → `notifications/initialized`) once, caching the
session.

### Configuration

Only `MCP_SERVER_URL` is required to enable MCP:

| Variable | Required | Default | Description |
|---|---|---|---|
| `MCP_SERVER_URL` | **Yes** (to enable) | *(empty)* | Full URL + path of your MCP endpoint, e.g. `http://localhost:8000/mcp` |
| `MCP_ENABLED` | No | `auto` | `auto` enables MCP whenever a URL is set. Force with `true`/`false` |
| `MCP_AUTH_HEADER` | No | *(empty)* | Auth header name, e.g. `Authorization`. Omit if no auth |
| `MCP_AUTH_VALUE` | No | *(empty)* | Auth header value, e.g. `Bearer <token>`. Omit if no auth |
| `MCP_TIMEOUT_SECONDS` | No | `30` | Per-request timeout |
| `MCP_PROTOCOL_VERSION` | No | `2025-06-18` | MCP protocol version sent during `initialize` |

**Local, no-auth example:**

```bash
MCP_SERVER_URL=http://localhost:8000/mcp
```

### Notes

- **You do NOT redefine your tools here.** They are discovered at runtime via `tools/list`.
- **Different tools work automatically** — add/remove tools on your server with zero code
  changes; each appears to the LLM as `mcp__<toolName>`.
- **Arguments are LLM-generated** from each tool's own `inputSchema` — no hardcoded
  argument names or per-tool special casing.
- **MCP results are excluded from `agent_answers`** (only `portfolio`/`crm` domains
  contribute there); they flow into the answer via history and citations.

### Fail-soft guarantee

Any MCP failure (server down, timeout, bad response) is caught, logged, and the request
continues without MCP tools — a misconfigured or unreachable MCP server can never break a
request.

### Where each piece lives

| Concern | File |
|---|---|
| MCP client / protocol | `src/app/services/mcp_manager.py` (`McpManager`) |
| MCP tool wrapper (LLM-facing) | `src/app/agents/mcp_tool_wrapper.py` (`McpToolWrapper`) |
| MCP config (URL, auth, timeout) | `src/app/settings.py` |
| MCP error type | `src/app/errors.py` (`McpIntegrationError`) |
| Discovery + wiring | `src/app/agents/relationship_manager.py` (`_ensure_mcp_tools_loaded`), `src/app/main.py` |
