# Hybrid Memory Architectures for Agent Systems

> Research document for the Intelligent Session Routing initiative.
> Covers six retrieval architectures with an eye toward hex's actual infrastructure.

---

## Approaches Evaluated

1. [FTS5 Keyword Search](#1-fts5-keyword-search) ← current hex approach
2. [Semantic Embeddings + Vector Similarity](#2-semantic-embeddings--vector-similarity)
3. [Hybrid: Keyword + Semantic with Reciprocal Rank Fusion](#3-hybrid-keyword--semantic-with-rrf)
4. [Graph Memory](#4-graph-memory)
5. [Tiered Memory (Hot / Warm / Cold)](#5-tiered-memory)
6. [Context-Window-Aware Retrieval](#6-context-window-aware-retrieval)

---

## 1. FTS5 Keyword Search

**How it works:** SQLite's FTS5 virtual table tokenizes text into indexed terms using Porter stemming and allows BM25-ranked full-text queries. Queries are matched against an inverted index, returning documents sorted by term frequency × inverse document frequency. Hex currently uses this via `behavioral_memory.py` (`behavioral_patterns_fts`) for correction pattern lookup and is the sole retrieval strategy in `memory_search.py`.

| Property | Assessment |
|---|---|
| **Strengths** | Sub-millisecond queries; zero dependencies; exact-term recall near-perfect; already in hex |
| **Weaknesses** | Fails on synonyms, paraphrases, and semantic similarity; case changes and typos reduce recall; no understanding of intent |
| **Latency** | <1ms on corpus of tens of thousands of records |
| **Storage** | ~20–30% of raw corpus size for the FTS index |
| **Dependencies** | SQLite only — ships with Python stdlib, no external packages |
| **Prior art** | Used by: hex (behavioral_memory), MemOS (paired with vectors), LangChain SQLite memory, most production search pipelines as a baseline |

**Verdict for hex:** Fast and already deployed, but degrades badly on natural-language queries like "What did Mike decide about X?" where the words in the query don't appear verbatim in the stored text.

---

## 2. Semantic Embeddings + Vector Similarity

**How it works:** Each document chunk is converted to a dense vector (typically 384–1536 dimensions) by running it through an embedding model. Queries are embedded the same way and compared by cosine similarity against all stored vectors. The top-k most similar vectors are returned. Storage backends range from in-memory NumPy arrays to dedicated ANN (Approximate Nearest Neighbor) indexes via `sqlite-vec`, `Chroma`, `Qdrant`, or `Pinecone`. For hex's use case, `fastembed` (ONNX Runtime, BAAI/bge-small-en-v1.5, 384 dimensions) running entirely on CPU is the viable local option, processing ~700 chunks/second at ingestion time.

| Property | Assessment |
|---|---|
| **Strengths** | Handles paraphrases, synonyms, and intent well; returns relevant results even when no keywords match; good for "soft" memory queries |
| **Weaknesses** | Higher latency than FTS5; misses exact terminology and proper nouns that embeddings generalise away; index must be rebuilt or updated on write; 384-dim float32 vectors add ~1.5KB per chunk |
| **Latency** | Embedding a query: ~10–50ms (CPU, bge-small). Search across 10k chunks: ~5–20ms with sqlite-vec ANN; linear scan is <5ms for corpora <10k chunks |
| **Storage** | 384 × 4 bytes = 1.5KB/chunk; 10k chunks ≈ 15MB in sqlite-vec (plus FTS overhead) |
| **Dependencies** | `fastembed` (ONNX Runtime, models download on first run, ~60MB for bge-small); `sqlite-vec` (loadable SQLite extension, pre-compiled) — no GPU, no cloud API |
| **Prior art** | Mem0, LangChain VectorStore, AgentMemory (ChromaDB/Qdrant), vstash (sqlite-vec + fastembed), MemOS, agent-memory-store |

**Verdict for hex:** Meaningfully better than FTS5 for natural-language queries but strictly worse for exact-term lookups. Not enough to stand alone — best paired with FTS5.

---

## 3. Hybrid: Keyword + Semantic with RRF

**How it works:** Run FTS5 BM25 search and vector similarity search in parallel against the same corpus. Independently rank each result list. Combine using Reciprocal Rank Fusion (RRF): `score = w_bm25/(k + rank_bm25) + w_vec/(k + rank_vec)` where `k=60` is a smoothing constant. Documents appearing in both lists get a natural boost. The merged list is the final ranking. This is implementable entirely in SQLite with a CTE join — no external process needed.

This is the current state of the art for local, offline agent memory. The 2025 vstash paper (arXiv 2604.15484) demonstrates NDCG@10 = 0.7263 with BGE-small, up to +21.4% over pure vector search on BEIR benchmarks. The agent-memory-store LongMemEval benchmark shows hybrid at **92.1% Recall@5** vs. semantic-only **86.1%** and BM25-only **92.0%** — hybrid beats or matches both on every metric.

```sql
-- Sketch of hybrid RRF in SQLite
WITH vec_matches AS (
  SELECT rowid, row_number() OVER (ORDER BY distance) AS rank
  FROM vec_chunks WHERE embedding MATCH :query_vec AND k = 20
),
fts_matches AS (
  SELECT rowid, row_number() OVER (ORDER BY rank) AS rank
  FROM fts_chunks WHERE text MATCH :query_text
),
fused AS (
  SELECT
    COALESCE(f.rowid, v.rowid) AS rowid,
    COALESCE(0.4 / (60.0 + f.rank), 0) + COALESCE(0.6 / (60.0 + v.rank), 0) AS score
  FROM fts_matches f FULL OUTER JOIN vec_matches v ON f.rowid = v.rowid
)
SELECT rowid, score FROM fused ORDER BY score DESC LIMIT 10;
```

| Property | Assessment |
|---|---|
| **Strengths** | Best recall of any single-database approach; each method compensates the other's blind spots; SQLite-native implementation possible; proven on real benchmarks |
| **Weaknesses** | Requires both FTS index and vector index maintained in sync; embedding at write time adds latency (~1.5ms/chunk CPU); weight tuning needed per corpus |
| **Latency** | ~10–60ms end-to-end (dominated by query embedding); search itself <5ms |
| **Storage** | FTS index + vector index ≈ 50–70% overhead on raw corpus |
| **Dependencies** | `fastembed` + `sqlite-vec` (same as approach 2); both CPU-only, no cloud |
| **Prior art** | vstash, agent-memory-store, MemOS OpenClaw Plugin, FastMemory, this SQLite issue thread from sqlite-vec maintainer asg017 |

**Verdict for hex:** **Recommended primary retrieval approach.** Highest recall, fully local, SQLite-native — fits directly on top of hex's existing `memory.db`. Would require adding `sqlite-vec` extension and `fastembed` as new dependencies.

---

## 4. Graph Memory

**How it works:** Entities (people, projects, concepts) and relationships between them are stored as nodes and edges in a knowledge graph. Retrieval involves traversal: "What does Mike know about crossposting?" navigates `Mike → crossposting-tool → decision → [node contents]`. Systems like Mem0 Graph Memory, Zep (temporal knowledge graph), and Cognee maintain these graphs by running LLM extraction passes over each new session, extracting entity mentions and relationships, and upserting them into a graph store (NetworkX, Neo4j, or in-memory dicts). Zep adds temporal modeling — each fact carries a `valid_at` timestamp, enabling "what was true last Tuesday?" queries.

| Property | Assessment |
|---|---|
| **Strengths** | Excellent for multi-hop relational queries; naturally models people, projects, and their connections; temporal graphs support "as of" queries; precise for structured knowledge |
| **Weaknesses** | Requires LLM calls per session to extract graph updates (~$0.001–0.01/session); graph construction is lossy — what the LLM doesn't extract, disappears; higher query latency for traversal; graph maintenance is an ongoing operational cost |
| **Latency** | Graph traversal: 5–50ms; entity extraction (LLM): 500ms–2s per session |
| **Storage** | Graph edges are compact; scales with entity count, not document size |
| **Dependencies** | Mem0/Zep/Cognee: cloud APIs or self-hosted. DIY: NetworkX + SQLite adjacency table. LLM for extraction (OpenRouter/local) |
| **Prior art** | Mem0 Graph Memory, Zep, Cognee, MemOS, Oracle AIDB, CARA (arXiv 2512.12818) — four-way parallel retrieval including graph |

**Verdict for hex:** High value for entity-relationship queries (who works on what, what decisions are linked to which projects), but the LLM extraction cost and complexity make it a Phase 2 addition, not a Phase 1 foundation. A lightweight DIY graph in SQLite (entity table + edge table) is feasible without Mem0/Zep.

---

## 5. Tiered Memory

**How it works:** Memory is split into tiers based on recency and access frequency, mirroring how human memory works:

- **Hot (working memory / L1):** Current session context, last few thousand tokens, in-memory only. Zero latency. Always included in context.
- **Warm (episodic / L2):** Last 7 days of session summaries, active project contexts, recently accessed learnings. Loaded per-query via FTS5 or vector search.
- **Cold (archival / L3):** Old transcripts compressed to summaries, inactive projects, historical decisions. Only loaded when explicitly relevant.

Access frequency determines promotion: a cold fact that gets queried repeatedly promotes to warm. A warm fact not accessed in 30 days demotes to cold. Aura (Rust library) implements a 4-level hierarchy with decay and background consolidation. The AgentMemory project uses 3 tiers with auto-compression: when working memory exceeds a token threshold, the oldest messages are summarized and pushed to episodic memory.

| Property | Assessment |
|---|---|
| **Strengths** | Keeps the context window lean; high-frequency information always fast; matches human intuition about memory importance; compression reduces storage costs |
| **Weaknesses** | Requires a background process for promotion/demotion; compression loses detail; tier boundaries are heuristic and may misclassify; adds architectural complexity |
| **Latency** | Hot: 0ms. Warm: <5ms (FTS/vector). Cold: 10–100ms (with additional fetch or decompression) |
| **Storage** | Significantly reduced — archived tiers are summaries, not raw text |
| **Dependencies** | No new dependencies if implemented over SQLite; tiering logic is pure Python |
| **Prior art** | Aura, AgentMemory, MemGPT (paging memory in/out of context), LangChain's ConversationSummaryBufferMemory |

**Verdict for hex:** The tier concept maps naturally onto hex's existing structure: hot = active session checkpoint, warm = recent me/learnings.md + active project contexts, cold = old transcripts + archived decisions. Should be implemented as a retrieval policy layer on top of approach 3, not as a standalone system.

---

## 6. Context-Window-Aware Retrieval

**How it works:** Rather than returning a fixed top-k, the retrieval system fills the available context budget. The caller specifies how many tokens remain (e.g., "I have 40,000 tokens left, fill 10,000 with relevant memory"). The retriever ranks all candidates and greedily includes them highest-to-lowest until the token budget is exhausted. This prevents both under-retrieval (missing relevant context) and over-retrieval (filling the window with noise). The vstash paper implements a token-budget-aware recall: retrieve top-n such that `sum(token_lengths) ≤ budget`. Zep's context API works similarly — `get_context(max_tokens=2000)` returns the most relevant memories up to the limit.

| Property | Assessment |
|---|---|
| **Strengths** | Never wastes context budget; adapts automatically to model window size; naturally pairs with tiered memory (prefer hot → warm → cold until budget full) |
| **Weaknesses** | Requires token counting per candidate (adds a few ms); greedy packing may include a less-relevant document over a more-relevant one that's slightly longer |
| **Latency** | Adds <10ms overhead for token counting; otherwise same as underlying retrieval approach |
| **Storage** | No additional storage — it's a retrieval policy |
| **Dependencies** | Token counting: `tiktoken` or simple approximation (4 chars/token) — no LLM call needed |
| **Prior art** | vstash, Zep context API, AgentMemory `get_context(max_tokens=...)`, MemGPT's paging architecture |

**Verdict for hex:** This should be the retrieval API contract: `retrieve(query, token_budget)` rather than `retrieve(query, k)`. Decouples the caller (model with variable context headroom) from retrieval implementation. Low-effort, high-impact change to the retrieval interface.

---

## Comparison Matrix

| Approach | Recall Quality | Latency | Complexity | GPU/Cloud? | Recommended Role |
|---|---|---|---|---|---|
| FTS5 keyword | Medium | <1ms | Trivial | No | Fallback / exact-match |
| Semantic embeddings | High | ~50ms | Low | No (CPU ONNX) | Standalone prototype |
| **Hybrid FTS5+Vector (RRF)** | **Highest** | **~60ms** | **Medium** | **No** | **Primary retrieval** |
| Graph memory | High (relational) | ~500ms+ | High | LLM required | Phase 2 / entity queries |
| Tiered memory | Depends on retrieval | Variable | Medium | No | Retrieval policy layer |
| Context-window-aware | Depends on retrieval | +<10ms | Low | No | API contract |

---

## Recommendation for Hex

**Phase 1 target:** Hybrid FTS5 + sqlite-vec (approach 3) with context-window-aware retrieval API (approach 6) and soft tiering as a retrieval policy (approach 5).

**Dependencies to add:**
- `fastembed` — ONNX-based embeddings, CPU-only, ~60MB model download, ~700 chunks/sec ingestion
- `sqlite-vec` — loadable SQLite extension, pre-compiled binary, ~500KB

**What stays:** `behavioral_memory.py`'s FTS5 implementation is already the right foundation. The upgrade path is: add a `vec_chunks` virtual table alongside the existing `behavioral_patterns_fts`, embed on write, and add an RRF query path.

**Phase 2 (future):** Add a lightweight graph (entity + edge table in SQLite, LLM-extracted per session during dreaming) for multi-hop relational queries across people, projects, and decisions.

---

## Prior Art Summary

| System | Approach | Key Insight |
|---|---|---|
| vstash (arXiv 2604.15484, 2025) | FTS5 + sqlite-vec + fastembed + RRF | Single SQLite file, CPU-only, +21% NDCG over pure vector |
| agent-memory-store (LongMemEval) | FTS5 + sqlite-vec + RRF | 92.1% Recall@5 hybrid vs 86.1% semantic-only |
| MemOS | FTS5 + vector + multi-agent | Hybrid search + skill memory, SQLite-backed |
| Mem0 | Vector + graph | Graph for relationships, cloud or self-hosted |
| Zep | Temporal knowledge graph | `valid_at` timestamps, context-budget API |
| Aura | Hierarchical decay + offline | Rust, no LLM needed, 4-tier with consolidation |
| AgentMemory | 3-tier + auto-compress | Hot/warm/cold + token-budget-aware `get_context` |
| FastMemory | FTS5 + BGE-large + RRF | Zero-cost offline, ~80% precision vs LLM judge's 90–95% |
| MemGPT / Letta | Paging + OS metaphor | Moves memory in/out of context like virtual memory pages |

---

*Generated: 2026-04-27 | Initiative: hex-system | Task: t-1*
