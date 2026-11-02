# Token Dashboard × HeliosDB-Nano — integration proposals

Ten candidate features that pair Token Dashboard's local prompt-history
analytics with HeliosDB-Nano's vector / graph-rag / code-graph / MCP
capabilities. Each item lists the HeliosDB feature(s) used, the
user-visible benefit, and a rough effort estimate so a partial adoption
plan can be assembled.

Background: HeliosDB-Nano is an embedded SQL/vector/graph database
(Postgres-wire compatible) that ships an in-process embedder
(`code-embed` — fastembed BGE-Small, 384-dim), HNSW vector search with
centrality bias and in-descent prefilter, a universal graph-rag schema
(`_hdb_graph.{nodes,edges}`), Docling-based document ingestion, a code
index (`_hdb_code.{symbols,files,refs}`), and an MCP 2024-11-05 endpoint
that Claude Code can talk to over Streamable HTTP. The reference build
is **v3.19.1** (April 2026).

The goal is not to replace Token Dashboard's stdlib-only ergonomics
wholesale — most items below are **additive**. Item #6 is the only
proposal that touches the existing SQLite store directly.

---

## Search & Q&A over prompt history

### 1. Vector index over prompts

**Uses:** `code-embed` (BGE-Small, in-process — no out-of-process embed
service), `body_vec VECTOR(384)` column on a new `prompts_corpus` table,
HNSW index with centrality bias.

**What:** During each scan, embed every user-prompt and assistant-final
response into a vector column. Top-K nearest-neighbour query becomes a
single SQL statement.

**Benefit:**
- Natural-language search box on the dashboard:
  "when did I last debug Postgres connection pooling?",
  "show me prompts about auth middleware".
- Duplicate-work detection: "you have asked this 4 times before in 3
  different projects."
- Same query path the rest of the analytics already use — no extra
  service to deploy, no extra latency budget.

**Effort:** **M** (~1 day). New `prompts_corpus` table; embed on backfill
+ on each new prompt during scan; new `/api/search` route returning
top-K with cosine distance; new search bar on the Overview tab.

---

### 2. RAG-style "explain this prompt" panel

**Uses:** graph-rag `WITH CONTEXT` query primitive, `_hdb_graph.nodes` /
`_hdb_graph.edges`.

**What:** For each expensive prompt, retrieve the surrounding context —
parent and sibling turns, related prompts in the same project, similar
prompts elsewhere — via `WITH CONTEXT` and surface a structured
explanation panel.

**Benefit:**
- Today: clicking an expensive prompt shows the prompt text.
- Tomorrow: a paragraph saying *"this turn cost 287k tokens because Claude
  re-Read the same 14k-line file 6 times across 3 tool calls — see prompt
  #X from yesterday where the same pattern occurred."*
- Turns the dashboard from a passive cost report into an active diagnostic
  tool.

**Effort:** **M-L** (~2-3 days). Depends on #1. Adds graph edges
(prompt → file, prompt → similar-prompt, prompt → tool-call), and a
`/api/explain/{prompt_id}` route that walks the graph.

---

### 3. Semantic clustering of work

**Uses:** vector embeddings from #1 + cluster mining (k-means in plpgsql
or external).

**What:** Group prompts into themes — "auth refactor", "deploy fixes",
"writing docs" — automatically. Project page shows a treemap of effort
by cluster, not just by file.

**Benefit:**
- "Where is my time and money actually going?" answered semantically,
  not just by file path.
- Surfaces themes that span multiple projects (the user spent 18 hours
  on RBAC across four repos last quarter).

**Effort:** **M** (~1 day, layered on #1).

---

## Code & document grounding

### 4. Project doc ingestion via Docling

**Uses:** `graph_rag_ingest_pdf` / `_office` / `_audio` / `_image`,
`_hdb_graph.nodes` (DocSection + DocChunk), CONTAINS edges. Idempotent
via `source_ref` keys.

**What:** Ingest each project's `CLAUDE.md`, `README.md`, ADRs, design
PDFs into HeliosDB once. Link to the prompts that referenced them.

**Benefit:**
- "Show me prompts where Claude used outdated info" — prompts whose
  timestamp predates a doc update they should have honoured.
- "Which prompts cited this ADR?" — useful for refactor postmortems.
- Better Tips: "this prompt did not reference `CLAUDE.md` when it
  arguably should have."

**Effort:** **M** (~1-2 days). Needs a docling-serve sidecar container
(out-of-process by design — keeps Nano embedded) + an `ingest` command
+ a join column on prompts.

---

### 5. Code-symbol resolution for prompts

**Uses:** `_hdb_code.{symbols,refs,files}`, `helios_lsp_definition`,
type-aware resolution (resolution = exact / heuristic / unresolved).

**What:** When a prompt mentions a symbol like `tool_token_breakdown` or
`parse_record`, resolve it via the code-graph and link the prompt to the
exact symbol node — not just the file path the scanner already captures.

**Benefit:**
- "Show me everything Claude has done to `mcp_summary` across all
  sessions."
- Detect "prompts that asked about a function that was renamed three
  weeks ago" — those are stale prompts to learn from, not duplicate.
- Cross-link prompt history to the actual code being worked on, even
  after refactors move it.

**Effort:** **L** (~3 days). Requires the user's code repos to be
indexed into HeliosDB first (`heliosdb-nano code-graph index`), plus a
per-prompt symbol-resolution step in the scanner.

---

## Persistence & multi-source

### 6. Replace SQLite with HeliosDB as the dashboard's primary store

**Uses:** Postgres wire (5432), full SQL surface, materialized views with
incremental refresh, audit log, encryption-by-default.

**What:** Drop the local SQLite cache. Ingest JSONL → HeliosDB directly.
Materialized views replace the per-query aggregations in `db.py`
(`project_summary`, `mcp_per_tool`, etc.) and refresh incrementally on
insert rather than re-aggregating on every API hit.

**Benefit:**
- **Multi-machine:** index from laptop A and laptop B into the same DB
  — currently the SQLite cache is per-machine.
- **Encrypted at rest by default** — the SQLite file is plaintext.
- **Audit log** of who-read-what (relevant if the dashboard is shared
  inside a team).
- **Real-time aggregations** — no scan-throttling needed; MV-incremental
  refresh keeps `/api/overview` fresh without re-scanning.

**Effort:** **L** (~4-6 days, the biggest swing). A static-analysis
compatibility check has been run against the dashboard's current SQL —
the scope is roughly: ~30 `?` placeholders → `$N` (Postgres style),
`INSERT OR REPLACE` → `ON CONFLICT DO UPDATE`, `PRAGMA` calls →
`information_schema`, `sqlite_master` → `pg_tables`, `executescript`
loop. The scan loop now writes over the wire instead of in-process.

---

### 7. Branch-based time travel

**Uses:** HeliosDB branches (per-call override, snapshot semantics).

**What:** Snapshot the dashboard's DB state on a schedule
(daily/weekly). The user can flip to "view as of last Monday" without
losing current state.

**Benefit:**
- "How did my token spend look two weeks ago, before the MCP migration?"
  answered without any custom history-tracking code.
- Useful for audits and for blog-post / before-after demos.

**Effort:** **S-M** (~½ to 1 day if #6 is done; otherwise infeasible
without HeliosDB).

---

## Real-time & streaming

### 8. Live "currently expensive" alerts

**Uses:** HeliosDB triggers + Postgres `LISTEN/NOTIFY`, MCP-endpoint SSE
pairing (the dashboard already opens an SSE channel for diff streaming).

**What:** A trigger on the prompts table fires when a single prompt
exceeds a configurable threshold (say 100k tokens). The dashboard's
existing SSE stream surfaces it as a banner: "session X is over budget,
click to investigate."

**Benefit:**
- Catch runaway sessions in real time, not after the fact.
- Useful when leaving Claude Code running on a long task — turn the
  dashboard into a budget alarm rather than a postmortem viewer.

**Effort:** **S** (~½ day if #6 is done; otherwise SQLite triggers +
polling, which is workable but less elegant — call it M).

---

## Tool-result intelligence

### 9. Embed tool-results — detect Claude re-reading the same content

**Uses:** `code-embed` over `_tool_result` bodies; HNSW with in-descent
prefilter for size-bucket filtering.

**What:** Embed every tool-result over a size threshold (say >2k
tokens). The vector index lets the dashboard spot "Claude saw this
exact content 6 times" even when the file paths differ — common with
rendered config files, generated docs, or read-again-after-Edit loops.

**Benefit:**
- Concrete cache-hygiene tip: "your `CLAUDE.md` got `cache_create`'d
  four times this week instead of `cache_read` — here is why."
- Detect Read-loops: when the assistant reads variations of the same
  file again and again across a session.
- Quantifies the *real* upper bound on what cache-hit could achieve.

**Effort:** **M** (~1-2 days).

---

## Audit & honesty

### 10. MCP receipt persistence with audit grade

**Uses:** Encryption (default), audit log, UUID literal round-trip (the
v3.19.1 fix).

**What:** Persist every MCP call's `_meta.helios` block plus the
dashboard's replay receipts in HeliosDB with audit-trail rows. The
existing dashboard logic stays; HeliosDB just acts as the verifiable
receipt store.

**Benefit:**
- Proves the "tokens saved" number for compliance, blog-post, or customer
  demo: a cryptographic audit log of every MCP call and its measured
  savings, signed against the HeliosDB binary's sha256.
- The savings number stops being "the dashboard says..." and becomes
  "...and here are the receipts, with timestamps that match the
  immutable audit log."

**Effort:** **S** (~½ day).

---

## How to choose

Three coherent adoption paths, depending on the goal.

### Path A — "I want to ask questions of my history"

Pick **1 + 2 + 3**. Pure additions, no migration risk. ~4 days. Best
single demo: a search bar that answers "when did I work on X?" with a
ranked list, plus an "explain this prompt" panel. This is the most
visible product upgrade for the least platform risk.

### Path B — "I want to unify the dashboard with the rest of my stack"

Start with **6** (the SQLite → HeliosDB migration). Then **7** and **8**
are nearly free additions. **1**, **4**, and **5** become natural
follow-ups because the platform is already there. Total ~10 days. The
dashboard becomes a showcase deployment for the platform — encrypted,
multi-machine, audit-trail, time-travel — without losing any of its
existing UX.

### Path C — "Make the savings number sharper"

Pick **9 + 10 + a slim version of 1** (prompts only, no clustering).
~2 days. Same UI shell; the numbers behind the headline get more
trustworthy. Best path if the dashboard is already in use and the
priority is credibility, not new features.

---

## Compatibility & migration notes

- All ten items are compatible with v3.19.1 and require no breaking
  changes to Token Dashboard's existing schema.
- Items 1, 4, 9 use `code-embed` (v3.19's local inference path); first
  run downloads ~30 MB to `$XDG_CACHE_HOME/.fastembed_cache`. The Nano
  binary itself stays at ~35 MiB.
- Item 6 is the only proposal that requires a real porting pass; the
  other nine sit alongside SQLite. A measured-effort plan for #6 is
  available on request.
- Items 2, 7, 8 depend on at least one earlier item being in place.
- The MCP plumbing the dashboard needs is already shipped (v3.18+ MCP
  endpoint, v3.19's HTTP+SSE pairing). The dashboard already runs the
  MCP 2024-11-05 conformance handshake against its configured endpoint
  via the Verify button.
