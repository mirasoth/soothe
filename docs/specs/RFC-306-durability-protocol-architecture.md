# RFC-306: DurabilityProtocol Architecture

**RFC**: 306
**Title**: DurabilityProtocol: Thread Lifecycle & Metadata Management
**Status**: Draft
**Kind**: Architecture Design
**Created**: 2026-04-17
**Authors**: Soothe Team
**Updated**: 2026-04-17
**Depends on**: RFC-000, RFC-001
**Related**: RFC-303, RFC-803
**Note**: Reclassified from 4xx to 3xx segment (protocol specs consolidation).

---

## Abstract

Defines DurabilityProtocol, Soothe's thread lifecycle persistence interface for durability-by-default design. Provides thread creation, resume, suspend, archive, and metadata management operations. Manages thread lifecycle state; StrangeLoop checkpoint persistence (CheckpointEnvelope) is handled separately in RFC-803.

---

## Protocol Interface

```python
class DurabilityProtocol(Protocol):
    """Thread lifecycle persistence protocol."""

    async def create_thread(
        self,
        metadata: ThreadMetadata,
        thread_id: str | None = None,
    ) -> ThreadInfo:
        """Create new thread with metadata."""
        ...

    async def resume_thread(self, thread_id: str) -> ThreadInfo:
        """Resume suspended thread."""
        ...

    async def suspend_thread(self, thread_id: str) -> None:
        """Suspend active thread (checkpoint state)."""
        ...

    async def archive_thread(self, thread_id: str) -> None:
        """Archive thread (read-only state)."""
        ...

    async def update_thread_metadata(
        self,
        thread_id: str,
        metadata: dict[str, Any] | ThreadMetadata,
    ) -> None:
        """Update thread metadata."""
        ...

    async def list_threads(
        self,
        thread_filter: ThreadFilter | None = None,
    ) -> list[ThreadInfo]:
        """List threads by filter criteria."""
        ...
```

---

## Data Models

### ThreadInfo

```python
class ThreadInfo(BaseModel):
    """Thread lifecycle information."""
    thread_id: str
    """Unique thread identifier."""
    status: Literal["active", "suspended", "archived"]
    """Thread lifecycle status."""
    created_at: datetime
    """Creation timestamp."""
    updated_at: datetime
    """Last update timestamp."""
    metadata: ThreadMetadata
    """Thread metadata."""
```

### ThreadMetadata

```python
class ThreadMetadata(BaseModel):
    """Thread metadata for lifecycle management."""
    tags: list[str] = []
    """Tags for categorization."""
    plan_summary: str | None
    """Plan summary for thread."""
    policy_profile: str | None
    """Policy profile name."""
    labels: dict[str, str] = {}
    """Custom labels."""
    priority: int = 50
    """Thread priority (0-100)."""
    category: str | None
    """Thread category."""
```

### ThreadFilter

```python
class ThreadFilter(BaseModel):
    """Filter criteria for thread listing."""
    status: Literal["active", "suspended", "archived"] | None
    """Filter by status."""
    tags: list[str] | None
    """Filter by tags."""
    created_after: datetime | None
    """Filter by creation time."""
    created_before: datetime | None
    """Filter by creation time."""
```

---

## Design Principles

### 1. Durable by Default

All agent state persistable and resumable:
- Thread lifecycle managed by DurabilityProtocol
- Conversation state managed by langgraph checkpointer
- StrangeLoop checkpoint managed by CheckpointEnvelope (RFC-803)
- Context ledger managed by ContextProtocol (RFC-302)

**Separation**: DurabilityProtocol manages thread metadata + lifecycle, NOT execution state (which is CheckpointEnvelope in Layer 2).

### 2. Thread Lifecycle States

**Active**: Thread executing (running state)
**Suspended**: Thread paused (checkpoint saved, can resume)
**Archived**: Thread completed/terminated (read-only history)

**State transitions**:
```
Created → Active → Executing → Suspended → Resumed → Active
Created → Active → Executing → Archived (terminal)
```

### 3. Metadata Persistence

Thread metadata persists independently:
- Tags for categorization
- Plan summary for context
- Policy profile for permission tracking
- Labels for custom metadata
- Priority for scheduling

### 4. Thread Filtering

Query threads by criteria:
- Status filtering (active/suspended/archived)
- Tag-based filtering
- Time-based filtering
- Metadata search

---

## Implementation

### DefaultBackend (LangGraph Integration)

```python
class LangGraphDurabilityBackend(DurabilityProtocol):
    """Langgraph checkpointer-based durability."""

    def __init__(self, checkpointer: BaseCheckpointSaver) -> None:
        self._checkpointer = checkpointer
        self._thread_metadata_store: dict[str, ThreadMetadata] = {}

    async def create_thread(
        self,
        metadata: ThreadMetadata,
        thread_id: str | None = None,
    ) -> ThreadInfo:
        tid = thread_id or generate_thread_id()
        self._thread_metadata_store[tid] = metadata
        return ThreadInfo(
            thread_id=tid,
            status="active",
            created_at=datetime.now(),
            updated_at=datetime.now(),
            metadata=metadata,
        )

    async def suspend_thread(self, thread_id: str) -> None:
        # Langgraph checkpoint automatically saved on thread switch
        # Metadata update to mark suspended
        metadata = self._thread_metadata_store[thread_id]
        self._thread_metadata_store[thread_id] = ThreadMetadata(
            **metadata.model_dump(),
            tags=metadata.tags + ["suspended"],
        )

    async def resume_thread(self, thread_id: str) -> ThreadInfo:
        # Langgraph checkpoint automatically loaded
        metadata = self._thread_metadata_store[thread_id]
        return ThreadInfo(
            thread_id=thread_id,
            status="active",
            metadata=metadata,
            ...
        )
```

**Backend**: LangGraph BaseCheckpointSaver + metadata store
**Integration**: Langgraph handles conversation state, DurabilityProtocol handles metadata

---

## Persistence Directory Layout

```text
$SOOTHE_HOME/
  durability/
    data/
      thread_{thread_id}.json  # Thread metadata
      thread_index.json        # Thread listing index
```

---

## Configuration

```yaml
protocols:
  durability:
    enabled: true
    backend: langgraph  # langgraph | sqlite | postgres
    persist_dir: $SOOTHE_HOME/durability/
```

---

## Integration Points

### SootheRunner Integration

```python
# Pre-stream: Thread lifecycle
async def _pre_stream(thread_id: str | None):
    if thread_id:
        # Resume existing thread
        thread_info = await durability.resume_thread(thread_id)
        context.restore(thread_id)
        # StrangeLoop checkpoint loaded separately (RFC-803)
    else:
        # Create new thread
        thread_info = await durability.create_thread(
            metadata=ThreadMetadata(tags=["user_query"]),
        )

# Post-stream: Thread lifecycle
async def _post_stream(thread_id: str):
    await durability.update_thread_metadata(
        thread_id,
        metadata={"plan_summary": plan.summary},
    )
    context.persist(thread_id)
    # StrangeLoop checkpoint saved separately (RFC-803)
```

---

## Implementation Status

- ✅ DurabilityProtocol interface
- ✅ ThreadInfo/ThreadMetadata/ThreadFilter models
- ✅ Thread lifecycle management (create/resume/suspend/archive)
- ✅ Metadata persistence
- ✅ Thread filtering
- ✅ LangGraph durability backend
- ⚠️ SQLite backend (RFC-801)
- ⚠️ PostgreSQL backend (future)

---

## References

- RFC-000: System Conceptual Design (§5 Durable by default)
- RFC-803: StrangeLoop Checkpoint Backend (CheckpointEnvelope — Layer 2 implementation)
- RFC-302: ContextProtocol (context persistence)
- RFC-001: Core Modules Architecture (original Module 5)

---

## Changelog

### 2026-04-17
- Consolidated RFC-001 Module 5 (DurabilityProtocol) with thread lifecycle management
- Separated from CheckpointEnvelope (Layer 2 implementation in RFC-803) to avoid overlap
- Defined thread lifecycle states (active/suspended/archived) and metadata persistence
- Clarified separation: DurabilityProtocol = thread metadata + lifecycle, CheckpointEnvelope = StrangeLoop execution state
- Maintained durable-by-default design principle

---

*DurabilityProtocol thread lifecycle persistence interface for metadata management and lifecycle states. Execution checkpoint persistence handled separately in RFC-803 (CheckpointEnvelope).*