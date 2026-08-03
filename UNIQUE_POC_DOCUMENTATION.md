# Relationship Manager Agentic RAG — POC Summary

**Author:** Ravuru Manoj
**Component:** Relationship Manager Assistant (Backend)
**Platform:** Unique AI (SDK + Toolkit)

*This proof-of-concept was designed and implemented by Ravuru Manoj to validate the
Unique AI agentic approach for relationship-manager use cases.*

---

## 1. Executive Summary

This proof-of-concept is a backend service that answers Relationship Manager (RM)
questions in natural language. A single question is automatically routed to the right
specialised assistants — a **Portfolio assistant** and a **CRM assistant** — which fetch
the relevant customer data, and the platform then composes one clear, compliance-checked
answer.

The purpose of the POC was to prove that the **Unique AI agentic framework** can be used
end-to-end for this scenario: understanding a question, deciding which data sources to
consult, retrieving grounded business data, and returning a trustworthy answer with a compliance check, and a regulatory disclaimer.

---

## 2. What the Solution Does

- Accepts a natural-language question about a specific customer.
- Automatically decides which assistants are needed to answer it (portfolio, CRM, or both).
- Retrieves the relevant business data behind each assistant.
- Composes a single consolidated answer from the retrieved information.
- Runs an automated compliance check on the answer before it is returned.
- Adds a standard regulatory disclaimer to every response.

---

## 3. Business Value & Outcomes

| Outcome | Why it matters |
|---------|----------------|
| **Single-question, multi-source answers** | An RM gets portfolio and relationship context together, instead of switching between systems. |
| **Grounded** | Every answer references the underlying data, which builds trust and supports audit. |
| **Built-in compliance guardrail** | Unsafe "guaranteed return" style language is automatically flagged before the answer reaches the user. |
| **Automatic regulatory disclaimer** | Every response carries the required disclaimer with no manual step. |
| **Extensible by design** | New assistants or new external data sources can be added without reworking the core. |


---

## 4. Objectives & Status

| # | Objective | Status |
|---|-----------|--------|
| 1 | Implement the Unique AI agentic orchestration flow (understand → route → retrieve → answer) | Achieved |
| 2 | Build two domain assistants (Portfolio and CRM) on the Unique AI tool pattern | Achieved |
| 3 | Use the Unique AI framework's supporting managers to make answers grounded, safe, and traceable | Achieved |
| 4 | Ground every answer in realistic portfolio and CRM data | Achieved |
| 5 | Add an automated financial-safety compliance check and a regulatory disclaimer | Achieved |
| 6 | Expose the capability through a clean, documented service interface | Achieved |

---

# Technical Detail

## 5. Technology Stack

| Layer | Technology | Purpose |
|-------|-----------|---------|
| Language | Python 3.14+ | Core implementation language |
| Web framework | FastAPI | Service interface and request/response handling |
| ASGI server | Uvicorn | Runtime server for the application |
| Data validation | Pydantic v2 | Typed request/response models and schemas |
| AI SDK | Unique AI SDK (`unique-sdk`) | Language-model boundary (`ChatCompletion.create`) |
| AI Toolkit | Unique Toolkit (`unique_toolkit`) | Agentic framework patterns (managers, tools, orchestration) |
| Language model | `AZURE_GPT_4o_2024_1120` (configurable) | Underlying model accessed through Unique AI |
| Packaging | `uv` + `pyproject.toml` | Environment and dependency management |
| Connector | Model Context Protocol (JSON-RPC 2.0 over Streamable HTTP) | External tool discovery and enrichment |
| Sample data | JSON datasets (portfolio + CRM) | Realistic business data behind the assistants |


## 6. Service Interface (API)

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/health` | GET | Service health check |
| `/relationship-manager/query` | POST | Main agentic query — routes to the assistants and returns the composed answer |
| `/portfolio/` | GET | All customers — AUM, YTD return, alpha, alerts |
| `/portfolio/{id}` | GET | Holdings, asset allocation, and P&L for one customer |
| `/portfolio/{id}/performance` | GET | Returns, risk metrics, sector/geo exposure, upcoming events |
| `/portfolio/{id}/compliance` | GET | Line of credit, tax summary, active alerts |
| `/crm/` | GET | All customers — segment, NPS, churn risk, follow-ups |
| `/crm/{id}` | GET | Full CRM profile and relationship-manager metadata |
| `/crm/{id}/interactions` | GET | Interaction history (filterable) and open service requests |
| `/crm/{id}/advisory` | GET | Suggestions, compliance flags, and action alerts |


## 7. Unique AI Components Used

The POC is built on the core building blocks of the Unique AI agentic framework.

### 7.1 Language-model boundary
- **`unique_sdk.ChatCompletion.create`** — the single, documented boundary for all model
  calls. The rest of the application never talks to the model directly; every call goes
  through one adapter.
- **`LanguageModelService` (Unique Toolkit)** — used when the Toolkit is installed, with a
  clean fallback to the SDK path when it is not.

### 7.2 Tool (assistant) pattern
- **`Tool` base class** and **`BaseToolConfig`** — the Unique AI abstraction for a callable
  assistant. Both the Portfolio and CRM assistants are implemented on this contract and
  publish a machine-readable schema the model uses to decide when to call them.
- **`ToolCallResponse`** — the standard result envelope each assistant returns, carrying
  the summary text, the citable data sections, and a debug trace.

### 7.3 Supporting managers — what we used and why it matters here

The POC wires in the Unique AI framework's five managers. Each plays a concrete role in
making the RM answer grounded, safe, and traceable:

| Manager | Role in the Unique AI framework | How it is useful in this POC |
|---------|--------------------------------|------------------------------|
| **HistoryManager** | Maintains the working conversation and keeps it within the model's context budget (Loop Token Reducer) | Lets the orchestrator run several planning-and-retrieval rounds for one RM question without exceeding the model's limits — older exchanges are trimmed while the core instructions are always kept. |
| **ReferenceManager** | Collects the data sections returned by each assistant and numbers them as citable sources | Ensures every figure in the final answer (a portfolio return, a CRM interaction) can be traced back to its source record — essential for trust in a financial advisory context. |
| **DebugInfoManager** | Captures a per-assistant execution trace | Gives developers and reviewers full visibility into how each answer was assembled, returned alongside the response for inspection. |
| **EvaluationManager** | Runs pluggable quality/compliance checks on the drafted answer | Runs a **financial-safety check** that flags unsafe "guaranteed returns" style language before the answer is released — a guardrail for regulated advice. |
| **PostprocessorManager** | Applies final transformations to the answer | Appends the standard **regulatory disclaimer** to every response, and keeps that disclaimer out of the model's memory so it does not distort later reasoning. |

### 7.4 Orchestrator
- **`RelationshipManagerOrchestrator`** — implements the Unique AI orchestration loop:
  detects a fresh request, seeds the conversation, plans which assistants to call,
  executes them concurrently, updates every manager with the results, exits early when an
  assistant signals it has taken control, and forces a clean final answer on the last
  round.

## 8. How It Works — Architecture & Request Flow

```
Question about a customer  (POST /relationship-manager/query)
           │
           ▼
 RelationshipManagerOrchestrator
  ├─ HistoryManager       — keeps the working conversation within the model's budget
  ├─ ReferenceManager     — numbers retrieved data sections as citable sources
  ├─ DebugInfoManager     — records how each answer was assembled
  ├─ EvaluationManager    — runs the financial-safety compliance check
  └─ PostprocessorManager — appends the regulatory disclaimer
           │
           ▼ plan which assistants to call → Unique AI ChatCompletion
           │
     ┌─────┴──────┐
     │            │
 Portfolio      CRM assistant     ← both built on the Unique AI Tool pattern
 assistant        │
     │            └─ (optional) external data via Model Context Protocol
     │            │
 portfolio.json  crm.json          ← grounded business data
```

**Request flow:**
1. The orchestrator receives a customer ID and a question.
2. It seeds the conversation with instructions and the question.
3. It asks the model which assistants to call, then runs them — concurrently when more
   than one is needed.
4. Each assistant fetches its data and returns a summary, citable sources, and a debug
   trace.
5. The managers record the results (history, debug).
6. On the final round the model composes one consolidated answer.
7. The answer is compliance-checked and a disclaimer is appended before it is returned.

Each request runs with a fresh set of managers, matching the Unique AI per-request
convention, and every step is logged so the full decision path is visible.


