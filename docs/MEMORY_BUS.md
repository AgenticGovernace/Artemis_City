<p><a target="_blank" href="https://app.eraser.io/workspace/obOCznjMqNpj2mv9zAvw" id="edit-in-eraser-github-link"><img alt="Edit in Eraser" src="https://firebasestorage.googleapis.com/v0/b/second-petal-295822.appspot.com/o/images%2Fgithub%2FOpen%20in%20Eraser.svg?alt=media&amp;token=968381c8-a7e7-472a-8ed6-4a6626da5501"></a></p>

# Memory Bus Specification
## Overview
The Memory Bus provides unified, synchronized access to heterogeneous storage backends (Obsidian vaults and vector databases). It implements write-through semantics, hierarchical read strategies, and conflict resolution to maintain consistency across explicit and semantic memory layers.

## Architecture
### Components
**Memory Bus Coordinator:**

- Central synchronization point for all memory operations
- Maintains consistency invariants
- Routes read requests through hierarchy
- Ensures write-through propagation
- Manages backpressure and queuing
**Storage Backends:**
1. **Obsidian Vault (Primary)**
    - Authoritative store for explicit knowledge
    - YAML frontmatter for metadata (weights, timestamps, embeddings)
    - Full-text indexing via Obsidian plugins
    - Sub-second latency, high reliability

2. **Vector Store (Secondary)**
    - Semantic search capability via embeddings
    - k-NN queries for similar memories
    - Metadata filtering (Hebbian scores, dates)
    - Approximately 100-300ms latency for indexing

## Write Protocol
### Write-Through Semantics
All memory mutations follow strict write-through ordering:

```
1. Validate write request
   ├─ Schema compliance
   ├─ Access control check
   └─ Conflict detection
2. Acquire write lock (distributed)
3. Persist to Obsidian (primary)
   └─ Atomic YAML update
   └─ Fsync to disk
   └─ Confirm persistence
4. Trigger async propagation to vector store
   └─ Serialize metadata
   └─ Queue embedding job
   └─ Enqueue to message broker
5. Return confirmation to caller
   └─ Ack includes write timestamp
   └─ Includes data hash for verification
6. Background sync
   └─ Vector store index update
   └─ Metadata sync within 200ms (p95)
   └─ Confirmation logged
```
### Write Request Format
```json
{
  "operation": "write|update|delete",
  "content_id": "uuid",
  "vault": "obsidian_vault_id",
  "document": {
    "path": "path/to/document.md",
    "content": "markdown content",
    "frontmatter": {
      "hebbian_weights": {
        "agent_id": 0.75
      },
      "created_at": "2026-02-21T10:30:00Z",
      "last_modified": "2026-02-21T10:35:00Z",
      "embedding_metadata": {
        "model": "text-embedding-3-large",
        "dimensions": 3072,
        "hash": "sha256_hash"
      }
    }
  },
  "metadata": {
    "source_agent": "agent_uuid",
    "priority": "high|normal|low",
    "requires_sync": true,
    "conflict_resolution": "last_write_wins|abort|merge"
  }
}
```
### Write Confirmation Response
```json
{
  "status": "success|conflict|timeout",
  "write_id": "uuid",
  "timestamp": "2026-02-21T10:30:00.123Z",
  "latency_ms": 145,
  "content_hash": "sha256_hash",
  "sync_pending": true,
  "estimated_sync_completion": "2026-02-21T10:30:00.300Z"
}
```

### Raw Exo artifacts and compressed context

Long Exo generations use two related writes:

1. The complete normalized provider text is written under `Agent Outputs/` with
   `artifact_kind: raw_exo_output`, its length, SHA-256, model, task, and parent
   provenance ID. This write sets `embed=False` so oversized raw text is not
   inserted into semantic recall.
2. A Hebbian-routed summarizer produces bounded follow-on context. The normal
   agent report is written through the Memory Bus and may be embedded for
   retrieval.

`output_compression` links both layers with the raw path/hash, durability state,
chosen summarizer, full routing decision, learning scope, and lengths. If both
raw-artifact and report persistence fail, the caller retains `raw_output`; the
system never discards the only remaining copy.

Provider failure/degraded-baseline results are still available to run
provenance, but their `learning_eligible: false` classification prevents them
from changing Hebbian or trust stores.
## Read Protocol
### Hierarchical Read Strategy
Reads use a cascading approach to balance latency and accuracy:

**Level 1: Exact Match (Obsidian)**

- Direct path lookup in Obsidian vault
- Condition: Known document path
- Latency SLA: <50ms (p95)
- Return: Exact document with parsed metadata
**Level 2: Keyword Match (Obsidian Metadata)**
- Full-text search across Obsidian vault
- Condition: Title/tag-based search, document not found
- Latency SLA: <150ms (p95)
- Return: Ranked results by relevance metadata
**Level 3: Semantic Match (Vector Store)**
- Vector similarity search
- Condition: No exact/keyword matches, semantic required
- Latency SLA: <300ms (p95)
- Return: Top-k results by cosine similarity
### Read Request Format
```json
{
  "operation": "read",
  "query_type": "exact|keyword|semantic",
  "exact_path": "optional/path/to/document.md",
  "keyword_search": {
    "terms": ["term1", "term2"],
    "fields": ["title", "tags", "content"],
    "match_mode": "all|any"
  },
  "semantic_search": {
    "query_text": "natural language query",
    "embedding": "pre-computed_vector",
    "top_k": 10,
    "filters": {
      "hebbian_weight_min": 0.3,
      "created_after": "2026-01-01T00:00:00Z"
    }
  },
  "metadata": {
    "use_cache": true,
    "timeout_ms": 500,
    "consistency_level": "eventual|strong"
  }
}
```
### Read Response Format
```json
{
  "status": "success|not_found|timeout",
  "matches": [
    {
      "content_id": "uuid",
      "path": "path/to/document.md",
      "content": "markdown content",
      "frontmatter": {
        "hebbian_weights": {},
        "created_at": "2026-02-21T10:00:00Z"
      },
      "relevance_score": 0.95,
      "source_level": 1,
      "latency_ms": 45
    }
  ],
  "total_matches": 1,
  "search_latency_ms": 45,
  "note": "Served from Obsidian (exact match)"
}
```
## Conflict Resolution
### Detection
Conflicts arise when simultaneous writes target the same document or related semantic entities.

**Detection Methods:**

- Write timestamp comparison (last-write-wins default)
- Content hash mismatches
- Vector embedding delta > threshold
- Concurrent write attempt detection via locks
### Resolution Strategies
**1. Last-Write-Wins (Default)**

- Obsidian timestamp governs
- Earlier write discarded
- Discarded writes logged for audit
- Suitable for non-critical metadata
**2. Abort**
- Both writes rejected
- Caller receives conflict error
- Caller must retry with explicit merge
- Suitable for high-consistency requirements
**3. Merge**
- Attempt automatic merge of content
- Frontmatter metadata merged (union of keys)
- Content diffs computed and merged (trivial 3-way)
- Manual resolution if merge fails
- Suitable for collaborative scenarios
### Example: Content Merge
```yaml
Agent A writes:
---
hebbian_weights:
  agent_1: 0.8
---
Task data A

Agent B writes (concurrent):
---
hebbian_weights:
  agent_2: 0.7
---
Task data B

Result (merge):
---
hebbian_weights:
  agent_1: 0.8
  agent_2: 0.7
---
Task data A [MERGE CONFLICT: human review needed]
```
## Latency SLAs
### Write Operations
| Percentile | Latency | Notes |
| ----- | ----- | ----- |
| p50 | 30ms | Direct Obsidian write |
| p95 | 200ms | Includes distributed lock overhead |
| p99 | 400ms | With retries for transient failures |
### Sync Operations (Obsidian → Vector Store)
| Percentile | Latency | Notes |
| ----- | ----- | ----- |
| p50 | 50ms | Quick embedding + indexing |
| p95 | 300ms | Includes batch processing |
| p99 | 600ms | Busy system or large document |
### Read Operations
| Operation | p50 | p95 | p99 |
| ----- | ----- | ----- | ----- |
| Exact match | 5ms | 50ms | 100ms |
| Keyword search | 40ms | 150ms | 250ms |
| Semantic search | 150ms | 300ms | 500ms |
## Prometheus Metrics
### Write Metrics
```asciidoc
artemis_memory_write_latency_ms (histogram)
  Labels: operation_type, status, conflict_resolution
artemis_memory_write_count (counter)
  Labels: operation, status, conflict_detected
artemis_memory_write_bytes (counter)
  Labels: storage_backend, operation_type
artemis_memory_sync_lag_ms (gauge)
  Labels: storage_backend
  Current lag between primary and secondary stores
```
### Read Metrics
```asciidoc
artemis_memory_read_latency_ms (histogram)
  Labels: query_type, result_count, cache_hit
artemis_memory_read_count (counter)
  Labels: query_type, status, cache_hit
artemis_memory_cache_hit_ratio (gauge)
  Labels: cache_level
artemis_memory_read_hierarchy_escalation (counter)
  Labels: from_level, to_level
  Times cascaded to deeper read levels
```
### Consistency Metrics
```
artemis_memory_conflicts_detected (counter)
  Labels: resolution_strategy, auto_resolved
artemis_memory_consistency_checks (counter)
  Labels: check_type, status
artemis_memory_desync_duration_seconds (histogram)
  Labels: storage_backend
  Time to recover from desynchronization
```
## Obsidian Integration
### Frontmatter Schema
```yaml
---
# Hebbian learning weights (agent_id -> weight)
hebbian_weights:
  agent_uuid_1: 0.75
  agent_uuid_2: 0.45

# Timestamps
created_at: 2026-02-21T10:00:00Z
last_modified: 2026-02-21T10:35:00Z

# Vector embedding metadata
embedding:
  model: text-embedding-3-large
  dimensions: 3072
  hash: sha256_hash
  created_at: 2026-02-21T10:00:00Z

# Task execution metadata
execution_history:
  - agent: agent_uuid_1
    status: success
    timestamp: 2026-02-21T10:15:00Z
    duration_ms: 250

# Tags for keyword search
tags:
  - task-type-analysis
  - high-priority
  - agent-1-successful

# Decay metadata (for archival)
decay_score: 0.92  # decayed from 1.0 over 30 days
archived: false
archival_candidate_at: 2026-08-21T10:00:00Z
---
```
### Write Sync Flow
```
Agent → Memory Bus → Obsidian (atomic update)
└→ Extract metadata
   ├→ Compute embeddings
   └→ Queue to vector store
      └→ Async indexing
```
## Vector Store Integration
### Embedding Configuration
- **Model**: text-embedding-3-large (OpenAI, or equivalent)
- **Dimensions**: 3072
- **Batch Size**: 100 documents
- **Indexing**: FAISS or similar (configurable)
### Metadata Filters
All vector search supports metadata filtering:

```json
{
  "filters": {
    "hebbian_weight_min": 0.5,
    "created_after": "2026-01-01T00:00:00Z",
    "agent_successful": true,
    "tags": ["critical", "learned"]
  }
}
```
## Error Handling
### Transient Failures
**Write Timeout:**

- Retry with exponential backoff (50ms, 100ms, 200ms)
- Max retries: 3
- Return error if all attempts fail
**Read Timeout:**
- Escalate to next read level if available
- Return partial results if semantic search times out
- Log timeout for monitoring
### Permanent Failures
**Obsidian Unavailable:**

- Queue writes to local buffer
- Persist buffer to local SQLite
- Sync when Obsidian recovers
- Alert monitoring system
**Vector Store Unavailable:**
- Disable semantic search
- Degrade to keyword search
- Continue write-through (async propagation queued)
- Backfill vector store on recovery
### Consistency Recovery
**Desynchronization Detected:**

1. Halt new writes
2. Rebuild vector store from Obsidian source-of-truth
3. Verify all documents re-indexed
4. Resume normal operation
5. Log incident with duration
## Backpressure & Queuing
If write throughput exceeds storage capacity:

1. **Queue Phase**: Buffer writes in memory (bounded queue, 10MB max)
2. **Spillover Phase**: Write queue to local SQLite (unbounded)
3. **Backpressure Response**: Return 503 Service Unavailable to new writes
4. **Recovery**: Drain queue as storage catches up
## Caching Strategy
**Read Cache (LRU, 100MB):**

- Cache hit on exact path lookups
- Invalidate on write operations
- TTL: 5 minutes for keyword/semantic results
- Metrics: Cache hit ratio by query type
**Write Deduplication:**
- Detect duplicate writes within 1-second window
- Return cached response
- Prevent duplicate vector embeddings
## Security
### Access Control
All read/write operations checked against:

- Agent permissions (registry entry)
- Path-based ACLs (Obsidian configuration)
- Capability tags (agent must have relevant capability)
### Audit Trail
All operations logged:

```
timestamp | operation | agent_id | content_id | status | latency_ms
2026-02-21T10:30:00Z | write | uuid_1 | uuid_2 | success | 145
```
## Configuration
### Environment Variables
```bash
ARTEMIS_OBSIDIAN_VAULT_PATH=/path/to/vault
ARTEMIS_VECTOR_STORE_URL=http://localhost:6333  # Qdrant example
ARTEMIS_MEMORY_WRITE_TIMEOUT_MS=200
ARTEMIS_MEMORY_SYNC_TIMEOUT_MS=300
ARTEMIS_MEMORY_CACHE_SIZE_MB=100
ARTEMIS_MEMORY_QUEUE_MAX_BYTES=10485760  # 10MB
ARTEMIS_EMBEDDING_MODEL=text-embedding-3-large
ARTEMIS_EMBEDDING_BATCH_SIZE=100
```




<!--- Eraser file: https://app.eraser.io/workspace/obOCznjMqNpj2mv9zAvw --->
