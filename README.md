
# Relationship Manager Agentic RAG — POC

A backend-only FastAPI application that uses the **Unique AI SDK and Unique Toolkit** to answer relationship-manager questions by orchestrating two specialised sub-agents (Portfolio and CRM) through a fully-managed agentic loop.

---

## What it does

- Accepts a natural-language question via a FastAPI endpoint.
- Runs an iterative orchestrator loop that plans, dispatches tool calls to sub-agents, and finalises an answer — aligned with the Unique Toolkit orchestrator pattern.
- Retrieves business context from JSON-backed tools (Portfolio and CRM data).
- Wires all five Unique Toolkit managers: **HistoryManager**, **ReferenceManager**, **DebugInfoManager**, **EvaluationManager**, and **PostprocessorManager**.
- Exposes additional read-only REST endpoints for raw portfolio and CRM data.
- Applies structured logging to every manager action, tool call, and iteration so every step is visible in the application logs.

---

## Architecture

```
POST /relationship-manager/query
           │
           ▼
 RelationshipManagerOrchestrator
  ├─ HistoryManager       — token-window-aware conversation history (Loop Token Reducer)
  ├─ ReferenceManager     — content-chunk extraction and sequential citation numbering
  ├─ DebugInfoManager     — per-tool debug trace store
  ├─ EvaluationManager    — FinancialSafetyEvaluation (compliance check)
  └─ PostprocessorManager — FinancialDisclaimerPostprocessor (response enrichment)
           │
           ▼ plan_with_tools → unique_sdk.ChatCompletion.create
           │
     ┌─────┴──────┐
     │            │
PortfolioAgent  CrmAgent        ← both extend the abstract Tool base class
     │            │
PortfolioTools  CrmTools        ← JSON-backed data retrieval
     │            │
 portfolio.json  crm.json
```

Each sub-agent returns a `ToolCallResponse` with:
- `content` — LLM-generated summary (sent to HistoryManager)
- `content_chunks` — referenceable data sections (sent to ReferenceManager)
- `debug_info` — execution metadata (sent to DebugInfoManager)

---

## Project structure

```text
src/app/
    main.py              — FastAPI application bootstrap
    settings.py          — environment-driven Settings dataclass
    logging_config.py    — structured logging with extra-field formatter
    errors.py            — AppError hierarchy (ValidationError, DataAccessError, …)
    schemas.py           — Pydantic schemas: Unique Toolkit types first, then API schemas
    api/
        routes.py            — orchestrator query + health endpoints
        portfolio_routes.py  — 4 read-only Portfolio GET endpoints
        crm_routes.py        — 4 read-only CRM GET endpoints
    agents/
        base_tool.py         — abstract Tool + BaseToolConfig (mirrors unique_toolkit)
        portfolio_agent.py   — PortfolioAgent(Tool) — returns ToolCallResponse
        crm_agent.py         — CrmAgent(Tool) — returns ToolCallResponse
        prompts.py           — system prompt strings for sub-agents
        relationship_manager.py — RelationshipManagerOrchestrator (all 5 managers)
    services/
        managers.py          — all 5 managers + FinancialSafetyEvaluation + FinancialDisclaimerPostprocessor
        unique_client.py     — UniqueAIClient (wraps unique_sdk.ChatCompletion.create)
        unique_toolkit.py    — UniqueToolkit facade (tries unique_toolkit, falls back to SDK)
        crm_tools.py         — CrmTools (4 merged query methods + get_customer_crm)
        portfolio_tools.py   — PortfolioTools (4 merged query methods)
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

1. **Fresh session check** — `HistoryManager.has_no_loop_messages()` detects a new request.
2. **History seeding** — system prompt (with per-tool hints) and user question added to HistoryManager.
3. **Planning step** — `UniqueToolkit.plan_with_tools()` calls `unique_sdk.ChatCompletion.create` with tool definitions derived from each Tool's `tool_description()`.
4. **Tool execution** — tool calls are deduplicated, capped per iteration, and executed concurrently via `asyncio.gather`.
5. **Manager update** — after each tool execution:
   - HistoryManager records the assistant tool-call message and tool result messages.
   - ReferenceManager extracts `content_chunks` for source citations.
   - DebugInfoManager harvests `debug_info` traces.
6. **Control check** — if any tool returns `takes_control()=True` the loop exits early.
7. **Last iteration** — tools disabled; LLM forced to produce a final answer.
8. **Post-loop** — EvaluationManager runs `FinancialSafetyEvaluation`; PostprocessorManager applies `FinancialDisclaimerPostprocessor`.
9. **Persistence** — `HistoryManager.extract_message_tools()` persists tool call records.
10. **Completion signal** — equivalent of `set_completed_at=True` logged.

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

| Variable | Required | Default | Description |
|---|---|---|---|
| `UNIQUE_APP_ID` | Yes | — | Unique application ID |
| `UNIQUE_APP_KEY` | Yes | — | Unique application key (`UNIQUE_API_KEY` also accepted as alias) |
| `UNIQUE_AUTH_COMPANY_ID` | Yes | — | Company ID sent with every SDK call |
| `UNIQUE_AUTH_USER_ID` | Yes | — | User ID sent with every SDK call |
| `UNIQUE_MODEL_NAME` | No | `AZURE_GPT_4o_2024_1120` | LLM model identifier |
| `UNIQUE_API_BASE_URL` | No | — | Override SDK base URL |
| `UNIQUE_API_VERSION` | No | `2023-12-06` | SDK API version |
| `UNIQUE_AGENT_MAX_ITERATIONS` | No | `3` | Maximum orchestrator loop iterations |
| `UNIQUE_MAX_TOOL_CALLS_PER_ITERATION` | No | `3` | Tool call cap per iteration |
| `UNIQUE_MAX_HISTORY_TOKENS` | No | `6000` | Token budget for HistoryManager Loop Token Reducer |
| `APP_NAME` | No | `relationship-manager-agentic-rag-poc` | Application name |
| `APP_ENV` | No | `local` | Environment label |
| `LOG_LEVEL` | No | `INFO` | Logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |

---

## Run

```powershell
uv run uvicorn app.main:app --reload --app-dir src
```

After the server starts:

- **Health check:** `GET /health`
- **Orchestrator query:** `POST /relationship-manager/query`
- **Portfolio data:** `GET /portfolio/` · `GET /portfolio/{customer_id}` · `/performance` · `/compliance`
- **CRM data:** `GET /crm/` · `GET /crm/{customer_id}` · `/interactions` · `/advisory`

Example orchestrator request:


```json
{
    "customer_id": "CUST-1001",
    "question": "Share portfolio and CRM summary for this customer"
}
```

Example response shape:

```json
{
    "customer_id": "CUST-1001",
    "question": "...",
    "routing_decision": ["portfolio", "crm"],
    "final_answer": "...",
    "agent_answers": [...],
    "debug_info": { "total_iterations": 2, "reference_chunk_count": 8, ... },
    "evaluation_results": [{ "name": "financial_safety", "is_positive": true, "value": "PASS", "reason": "..." }]
}
```

---

## Run tests

```powershell
python -m pytest tests/ -v
```

All tests use a monkeypatched `UniqueAIClient.create_completion` — no live Unique credentials needed.

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

All endpoints are read-only GET.

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
| History management | `HistoryManager` with Loop Token Reducer — mirrors `unique_toolkit` |
| Citation tracking | `ReferenceManager.extract_referenceable_chunks()` |
| Quality checks | `EvaluationManager` + `Evaluation` abstract class |
| Response enrichment | `PostprocessorManager` + `Postprocessor` abstract class |
| Debug exposure | `DebugInfoManager.add() / get()` |

---

## MCP integration (CRM agent)

The **CRM agent** can enrich its answer with data pulled from an external **MCP (Model
Context Protocol) server** — for example a local server you run separately. This mirrors
the Unique Toolkit *Tool Manager → MCP source* concept: MCP tools are discovered and
called alongside the built-in tools, and their output flows into the same
`ToolCallResponse` pipeline (summary + citations + debug info).

MCP is **fully optional and additive**. When no MCP server URL is configured, the CRM
agent behaves exactly as before.

### How it works

```
CrmAgent.run()
    │  1. fetch local CRM data (crm.json)                     ← unchanged
    │  2. _augment_with_mcp(customer_id, question)            ← NEW, only if MCP configured
    │        ├─ McpManager.list_tools()   → tools/list  (discover ALL tools on your server)
    │        └─ McpManager.call_tool(...) → tools/call  (invoke each discovered tool)
    │  3. merge MCP output into the LLM summarization context
    │  4. add MCP results as citable ContentChunks + debug_info
    ▼
ToolCallResponse (content + content_chunks + debug_info)
```

The MCP client lives in a **separate service**, `src/app/services/mcp_manager.py`
(`McpManager`). It speaks JSON-RPC 2.0 over the **Streamable HTTP** transport using only
the Python standard library (`urllib`), so it adds **no new runtime dependency**. It
handles both `application/json` and `text/event-stream` (SSE) responses and performs the
MCP handshake (`initialize` → `notifications/initialized`) once, caching the session.

### Configuration

Set these environment variables (in `.env` or the shell). **Only `MCP_SERVER_URL` is
required** — that is the one place you point the app at your MCP server:

| Variable | Required | Default | Description |
|---|---|---|---|
| `MCP_SERVER_URL` | **Yes** (to enable MCP) | *(empty)* | Full URL + path of your MCP endpoint, e.g. `http://localhost:8000/mcp` |
| `MCP_ENABLED` | No | `auto` | `auto` enables MCP whenever a URL is set. Force with `true`/`false` |
| `MCP_AUTH_HEADER` | No | *(empty)* | Auth header name, e.g. `Authorization`. Omit if your server has no auth |
| `MCP_AUTH_VALUE` | No | *(empty)* | Auth header value, e.g. `Bearer <token>`. Omit if your server has no auth |
| `MCP_TIMEOUT_SECONDS` | No | `30` | Per-request timeout |
| `MCP_PROTOCOL_VERSION` | No | `2025-06-18` | MCP protocol version sent during `initialize` |

**Local, no-auth example** (leave the auth vars unset — no auth header is sent):

```bash
MCP_SERVER_URL=http://localhost:8000/mcp
```

### Working with your MCP tools

- **You do NOT redefine your tools here.** They are discovered at runtime via
  `tools/list`. Your tool definitions stay in one place: your MCP server.
- **Different tools work automatically.** Add/remove tools on your server and they are
  picked up with zero code changes.
- **Argument filtering.** The CRM agent passes `{"customer_id", "question"}` to each
  tool. `McpManager` automatically drops any argument a tool's `inputSchema` does not
  declare, so a tool that only wants `customer_id` won't break on the extra `question`.
  > If your tools expect a different key name (e.g. `id` or `client_id`), that key must
  > be provided — otherwise the filter drops `customer_id` and the tool receives no
  > useful argument.
- **Call all vs. one tool.** By default every discovered tool is called. To pin the CRM
  agent to a single tool, set `mcp_tool_name` on `CrmAgentConfig` in
  `src/app/agents/crm_agent.py` (e.g. `mcp_tool_name = "get_customer_risk"`).

### Fail-soft guarantee

Any MCP failure (server down, timeout, bad response) is caught inside the CRM agent,
logged, and reported via `debug_info["mcp_error"]`. The core CRM answer is always
returned — a misconfigured or unreachable MCP server can never break the request.

### Where each piece lives

| Concern | File |
|---|---|
| MCP client / protocol | `src/app/services/mcp_manager.py` (`McpManager`) |
| MCP config (URL, auth, timeout) | `src/app/settings.py` |
| MCP error type | `src/app/errors.py` (`McpIntegrationError`) |
| CRM enrichment logic | `src/app/agents/crm_agent.py` (`_augment_with_mcp`) |
| Manager wiring (startup) | `src/app/main.py` |

### Related debug fields

When MCP runs, the response `debug_info` includes: `mcp_enabled`,
`mcp_discovered_tools`, `mcp_called_tools`, and `mcp_error` (null on success).


