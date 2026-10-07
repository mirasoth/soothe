# RFC-606: DeepAgents CLI TUI Migration Specification

**RFC**: 606
**Title**: DeepAgents CLI TUI Migration
**Status**: Draft
**Kind**: Architecture Design + Implementation Interface Design
**Created**: 2026-04-13
**Authors**: Soothe Team
**Updated**: 2026-04-13
**Depends on**: RFC-302, RFC-500, RFC-501, RFC-203, RFC-303
**Related**: RFC-600

## Abstract

This RFC specifies the migration of deepagents-cli's sophisticated Textual TUI into Soothe, replacing Soothe's basic custom TUI (~1053 lines) with deepagents' mature UI implementation (~5069 lines + 20 widget files). The migration uses a **Full Copy & Deep Integration** strategy, copying deepagents TUI code into Soothe's codebase and connecting it to Soothe's backend infrastructure (daemon WebSocket, protocol orchestration, thread persistence) via adapter layers. All Soothe-specific features (protocols, jobs dashboard, CLI commands) are preserved through integration hooks and custom widgets. This provides immediate access to deepagents' advanced features (thread selector, autocomplete, approval UI, diff viewer) while eliminating maintenance duplication.

## Problem Statement

Soothe's current TUI has significant limitations compared to upstream deepagents-cli:

1. **Feature gap** - Lacks sophisticated UI components:
   - Thread resume UI with filtering/search (deepagents: thread_selector.py - 65KB)
   - Advanced ChatInput with autocomplete, multi-line, history (deepagents: chat_input.py - 69KB)
   - Interactive tool approval UI (deepagents: approval.py - 17KB)
   - Inline diff visualization (deepagents: diff.py - 7KB)
   - 20+ polished widgets (status, loading, messages, welcome, autocomplete)

2. **Maintenance burden** - Custom TUI code (~1053 lines) requires ongoing development for features that deepagents already provides

3. **Duplication** - Both packages independently implement similar Textual patterns, creating divergent maintenance paths

4. **Integration complexity** - Connecting Soothe's protocol orchestration events (`soothe.*`) to custom TUI requires extensive custom event handling that deepagents already solved for similar event types

## Design Goals

1. **Immediate feature access** - Get all deepagents TUI features without reimplementation
2. **Backend integration** - Connect deepagents TUI to Soothe's daemon WebSocket, protocols, and thread persistence
3. **Protocol event rendering** - Visualize Soothe-specific protocol events (`soothe.*`) in deepagents widgets
4. **CLI preservation** - Keep Soothe's Typer command structure unchanged
5. **Jobs integration** - Preserve Soothe's jobs dashboard as alternate screen mode
6. **Feature parity** - All deepagents TUI features work (autocomplete, approval, diff, thread selector)
7. **No regression** - Existing Soothe workflows continue unchanged
8. **Clean integration** - Adapter layers separate concerns, modifications clearly marked

## Guiding Principles

1. **Copy over Import** - deepagents-cli not designed as library; copying provides control and avoids tight coupling to SDK version
2. **Adapter Pattern** - Bridge deepagents TUI expectations to Soothe backend realities through clean abstraction layers
3. **Explicit Modifications** - Mark all changes with `# SOOTHE: ...` comments for future maintenance and upstream sync
4. **Preserve Soothe Identity** - Keep protocol events, loop-rail, daemon architecture; these are Soothe's core value
5. **Verbosity Alignment** - Apply RFC-501 filtering to protocol events in TUI rendering
6. **Widget Reuse** - Use deepagents widgets where applicable (status, loading), create Soothe-specific widgets where needed (protocol events)

## Architecture

### Overall Stack After Migration

```
CLI Layer (Typer - unchanged)
  src/soothe/ux/cli/main.py
    └─ soothe, daemon, thread, config, health, status, jobs commands

Execution Layer (unchanged)
  src/soothe/ux/cli/execution/
    ├─ launcher.py → decides TUI/headless/daemon modes
    ├─ daemon.py → daemon client connection
    └─ headless.py → non-TUI execution

TUI Layer (MIGRATED from soothe_deepagents)
  src/soothe_cli/tui/
    ├─ app.py (copied from soothe_deepagents_cli/app.py, modified)
    ├─ widgets/ (copied from soothe_deepagents_cli/widgets/, 20 files)
    ├─ input.py, sessions.py, theme.py (copied supporting modules)
    ├─ jobs_screen.py (kept from Soothe)
    ├─ jobs_dashboard.py (kept from Soothe)
    ├─ soothe_backend_adapter.py (NEW - daemon stream adapter)
    ├─ thread_backend_bridge.py (NEW - thread persistence bridge)
    └─ widgets/protocol_event.py (NEW - protocol visualization)

Backend Layer (unchanged)
  src/soothe/ux/client/session.py → DaemonClient (WebSocket)
  src/soothe/daemon/ → WebSocket server
  src/soothe/runner/ → SootheRunner + protocols
```

### Component Flow

```
User Input (TUI ChatInput)
    ↓
app.py sends input
    ↓
SootheBackendAdapter.stream_messages()
    ↓
DaemonClient.send_input() → WebSocket
    ↓
SootheDaemon receives input
    ↓
SootheRunner.astream() → protocol orchestration
    ↓
Daemon yields (namespace, mode, data) tuples
    ↓
DaemonClient.receive_events() → WebSocket stream
    ↓
SootheBackendAdapter parses events
    ├─→ Protocol events → _adapt_protocol_event() → filtering
    └─→ Regular events → pass through
    ↓
app.py receives stream
    ├─→ soothe.plan.* → PlanTree widget update
    ├─→ soothe.context/memory/policy → StatusBar protocol queue
    └─→ messages/updates → deepagents message handling
    ↓
Widgets render (ConversationPanel, StatusBar, PlanTree)
```

## Section 1: File Migration Specification

### Files to Copy from soothe_deepagents-cli

**Source**: `/Users/xiamingchen/Workspace/mirasurf/deepagents/libs/cli/deepagents_cli/`
**Target**: `src/soothe_cli/tui/`

**Core TUI Files** (4 files):
| Source File | Target File | Size | Purpose |
|-------------|-------------|------|---------|
| `app.py` | `app.py` | 5069 lines | Main Textual application, modified for Soothe backend |
| `input.py` | `input.py` | ~500 lines | Input handling utilities |
| `sessions.py` | `sessions.py` | ~600 lines | Session state management |
| `theme.py` | `theme.py` | ~700 lines | Color schemes, styling |

**Widget Files** (20 files → `widgets/` subdirectory):
| Widget | Size | Purpose |
|--------|------|---------|
| `chat_input.py` | 69KB | Advanced input with autocomplete, multi-line, history |
| `thread_selector.py` | 65KB | Sophisticated thread resume UI with filtering/search |
| `messages.py` | 60KB | Rich message rendering (user, assistant, tool calls) |
| `model_selector.py` | 34KB | Model selection UI (not used in Soothe, optional) |
| `autocomplete.py` | 23KB | Slash command autocomplete |
| `message_store.py` | 22KB | Message state management |
| `approval.py` | 17KB | **Not migrated** — interactive approval menu removed; file previews are non-blocking |
| `ask_user.py` | 14KB | User question prompts |
| `status.py` | 12KB | Status bar (modified for protocol events) |
| `welcome.py` | 12KB | Welcome banner |
| `mcp_viewer.py` | 11KB | MCP server info display |
| `tool_widgets.py` | 9KB | Tool call visualization |
| `diff.py` | 7KB | Inline code diff viewer |
| `history.py` | 6KB | Conversation history navigation |
| `loading.py` | 5KB | Loading indicators |
| `tool_renderers.py` | 4KB | Tool output formatting |
| `notification_settings.py` | 4KB | Notification configuration |
| `theme_selector.py` | 4KB | Theme selection UI |
| `_links.py` | 2KB | Link handling utilities |
| `__init__.py` | 0.2KB | Widget module init |

**Supporting Modules** (7 files):
| Module | Purpose |
|--------|---------|
| `tools.py` | Tool integration helpers |
| `tool_display.py` | Tool output formatting utilities |
| `clipboard.py` | Clipboard operations (copy/paste) |
| `editor.py` | External editor integration |
| `file_ops.py` (partial) | File operation UI helpers only |
| `media_utils.py` | Image/media handling in TUI |
| `offload.py` | Background task offloading |

**Total**: ~25-30 files copied

### Files to Delete (Old Soothe TUI)

Delete from `src/soothe_cli/tui/`:
| File | Size | Reason |
|------|------|--------|
| `app.py` | 1053 lines | Replaced by deepagents app.py |
| `widgets.py` | ~300 lines | Replaced by widgets/ directory |
| `renderer.py` | ~200 lines | Rendering now in deepagents widgets |
| `state.py` | ~150 lines | State management in deepagents message_store |
| `utils.py` | ~100 lines | Utilities in deepagents supporting modules |
| `modals.py` | ~150 lines | Modals in deepagents approval/ask_user |
| `commands.py` | ~50 lines | Slash commands in deepagents autocomplete |

**Total**: ~7 files deleted (~2053 lines removed)

### Files to Keep (Soothe-Specific)

Keep from `src/soothe_cli/tui/`:
| File | Purpose |
|------|---------|
| `jobs_screen.py` | Jobs dashboard screen (RFC-203) |
| `jobs_dashboard.py` | Jobs widgets (GoalProgressWidget, ExecutionQueueWidget) |
| `__init__.py` | Module init (needs update for new imports) |

### Files to Create (Integration Layer)

Create in `src/soothe_cli/tui/`:
| File | Purpose |
|------|---------|
| `soothe_backend_adapter.py` | Adapter bridging daemon to deepagents TUI interface |
| `thread_backend_bridge.py` | Bridge connecting thread selector to Soothe persistence |
| `widgets/protocol_event.py` | Widget for rendering Soothe protocol events |

### Migration Summary

- **Net change**: +18-23 files in `src/soothe_cli/tui/`
- **Code volume**: ~5069 lines (deepagents app.py) + ~200KB widgets > ~1053 lines (old Soothe TUI)
- **Feature gain**: Thread selector, autocomplete, approval UI, diff viewer, 20+ widgets

## Section 2: Backend Integration Specification

### SootheBackendAdapter Protocol

**Purpose**: Present deepagents-like interface to TUI while connecting to Soothe daemon backend.

**Interface**:

```python
class SootheBackendAdapter:
    """Adapter for deepagents TUI to connect to Soothe daemon.

    Replaces: deepagents agent.astream() with daemon WebSocket stream
    Implements: Message streaming, thread management, metadata provision
    """

    def __init__(
        self,
        daemon_client: DaemonClient,
        config: SootheConfig,
        thread_manager: ThreadContextManager,
    ):
        """Initialize adapter with Soothe backend components."""
        ...

    async def stream_messages(self, user_input: str) -> AsyncIterator[Tuple]:
        """Stream messages from daemon in deepagents format.

        Mimics: agent.astream(stream_mode=["messages", "updates", "custom"])
        Returns: AsyncIterator of (namespace, mode, data) tuples
        Protocol events: Filtered by verbosity tier (RFC-501)
        """
        ...

    def _parse_daemon_event(self, event: dict) -> Tuple[str, str, dict]:
        """Parse WebSocket event into (namespace, mode, data) format."""
        ...

    def _adapt_protocol_event(self, event: dict) -> Optional[dict]:
        """Filter protocol events by verbosity tier.

        quiet: suppress all protocol events
        minimal: show only soothe.plan.* events
        normal: show plan + context + memory events
        detailed: show all protocol events
        """
        ...

    async def get_thread_history(self, thread_id: str) -> List[dict]:
        """Load thread messages for resume.

        Uses: ThreadContextManager backend
        Returns: deepagents message format
        """
        ...

    async def list_threads(self) -> List[dict]:
        """List available threads for resume UI."""
        ...

    def get_agent_metadata(self) -> dict:
        """Provide agent info for UI display.

        Returns: model name, available tools, subagents
        """
        ...
```

### app.py Modifications

**Modification locations**: 5 primary modifications in `app.py`

**Modification 1: Backend initialization** (lines ~200-250):

```python
# deepagents original:
# from soothe_deepagents_cli.textual_adapter import TextualUIAdapter
# self.agent = create_agent(...)
# self.ui_adapter = TextualUIAdapter(self.agent, ...)

# SOOTHE: Replace with Soothe adapter
from soothe.ux.tui.soothe_backend_adapter import SootheBackendAdapter

self.backend_adapter = SootheBackendAdapter(
    daemon_client=self.daemon_client,
    config=self.config,
    thread_manager=self.thread_manager,
)
```

**Modification 2: Stream invocation** (lines ~800-900):

```python
# deepagents original:
# async for msg in self.agent.astream(user_input, stream_mode=[...]):

# SOOTHE: Use backend adapter stream
async for namespace, mode, data in self.backend_adapter.stream_messages(user_input):
    await self._handle_stream_event(namespace, mode, data)
```

**Modification 3: Protocol event handling** (new method):

```python
# SOOTHE: Add protocol event handler
async def _handle_protocol_event(self, event: dict):
    """Route Soothe protocol events to appropriate widgets."""
    event_type = event.get("type", "")

    if event_type.startswith("soothe.plan."):
        # Update plan tree widget
        self.plan_widget.update_from_event(event)
    elif event_type.startswith("soothe.context."):
        # Show context operation in status bar
        self.status_bar.show_protocol_event(event)
    elif event_type.startswith("soothe.memory."):
        # Show memory operation in status bar
        self.status_bar.show_protocol_event(event)
    elif event_type.startswith("soothe.policy."):
        # Show policy check in status bar (detailed mode only)
        self.status_bar.show_protocol_event(event)
```

**Modification 4: Event routing** (in `_handle_stream_event`):

```python
async def _handle_stream_event(self, namespace: str, mode: str, data: dict):
    """Route stream events to handlers."""
    # deepagents original: message/update handling
    if mode == "messages":
        await self._handle_message_event(namespace, data)
    elif mode == "updates":
        await self._handle_update_event(namespace, data)
    elif mode == "custom":
        # SOOTHE: Check for protocol events
        event_type = data.get("type", "")
        if event_type.startswith("soothe."):
            await self._handle_protocol_event(data)
        else:
            # deepagents custom events (subagent progress, etc.)
            await self._handle_custom_event(namespace, data)
```

**Modification 5: Thread backend connection** (in thread selector invocation):

```python
# deepagents original (in thread selector widget):
# from soothe_deepagents_cli.sessions import SessionManager
# threads = await self.session_manager.list_threads()

# SOOTHE: Use thread backend bridge
from soothe.ux.tui.thread_backend_bridge import ThreadBackendBridge

self.thread_bridge = ThreadBackendBridge(self.thread_manager)
threads = await self.thread_bridge.list_threads_for_ui()
```

### Modification Marking Convention

All modifications use comment marker: `# SOOTHE: <description>`

**Purpose**: Enable future maintainers to:
1. Identify Soothe-specific changes vs deepagents original code
2. Sync future deepagents improvements by checking modification markers
3. Understand integration points clearly

**Example**:
```python
# deepagents original code (unchanged)
self.message_store.add_message(...)

# SOOTHE: Also add protocol event to status queue
if is_protocol_event:
    self.status_bar.show_protocol_event(event)
```

## Section 3: Protocol Event Rendering Specification

### Protocol Events to Render

**From RFC-500**:
| Event Type | Fields | Display |
|------------|--------|---------|
| `soothe.thread.started` | thread_id, protocols | Status: thread active |
| `soothe.thread.ended` | thread_id, summary | Status: thread completed |
| `soothe.context.projected` | entries, tokens, source | One-liner: "Context: N entries (M tokens)" |
| `soothe.context.ingested` | entries, tokens | One-liner: "Context ingested" |
| `soothe.memory.recalled` | count, query | One-liner: "Memory: recalled N items" |
| `soothe.memory.stored` | id, content_preview | One-liner: "Memory stored" |
| `soothe.plan.created` | goal, steps | Plan tree: show full plan |
| `soothe.plan.step_started` | step_id, description | Status: "Starting: <description>" |
| `soothe.plan.step_completed` | step_id, success | Status: "Completed" or "Failed" |
| `soothe.goal.batch_started` | goal_ids, step_count | Jobs dashboard: show goals |
| `soothe.goal.report` | completed, failed, summary | Jobs dashboard: update progress |
| `soothe.policy.checked` | action, verdict | Status (detailed): "Policy: <action> → <verdict>" |
| `soothe.policy.denied` | action, reason | Alert: "Policy denied: <reason>" |

### ProtocolEventWidget Specification

**File**: `src/soothe_cli/tui/widgets/protocol_event.py`

**Purpose**: Compact one-liner indicator for protocol events, similar to deepagents tool call indicators.

**Interface**:

```python
class ProtocolEventWidget(Widget):
    """Widget for rendering protocol events as compact indicators.

    Style: Icon + brief message + optional status
    Placement: Status bar activity queue, plan tree panel
    """

    def render_event(self, event: dict) -> str:
        """Render protocol event as Rich markup string.

        Args:
            event: Protocol event dict with type and fields

        Returns:
            Rich markup string for display
        """
        event_type = event.get("type", "")

        # Route to specialized renderer
        if event_type.startswith("soothe.plan"):
            return self._render_plan_event(event)
        elif event_type.startswith("soothe.context"):
            return self._render_context_event(event)
        elif event_type.startswith("soothe.memory"):
            return self._render_memory_event(event)
        elif event_type.startswith("soothe.policy"):
            return self._render_policy_event(event)
        elif event_type.startswith("soothe.goal"):
            return self._render_goal_event(event)
        else:
            return self._render_generic_event(event)

    def _render_plan_event(self, event: dict) -> str:
        """Plan events: step progress."""
        ...

    def _render_context_event(self, event: dict) -> str:
        """Context events: projection/ingestion."""
        ...

    def _render_memory_event(self, event: dict) -> str:
        """Memory events: recall/store."""
        ...

    def _render_policy_event(self, event: dict) -> str:
        """Policy events: approval/denial."""
        ...
```

**Rendering style**:
- Icons: 📋 (plan), 🔍 (context), 💭 (memory), 🔒 (policy), 🎯 (goal)
- Colors: cyan (plan), magenta (context), yellow (memory), red (policy denied), green (success)
- Format: `[icon] [color]Action:[/color] details`

**Example outputs**:
```
📋 [cyan]Starting:[/cyan] Analyze code structure
✅ [green]Completed[/green]
🔍 [magenta]Context[/magenta]: 15 entries (2340 tokens)
💭 [yellow]Memory[/yellow]: recalled 5 items
🔒 [red]Policy denied[/red]: file_write to /etc/passwd
```

### StatusBar Integration

**File**: `src/soothe_cli/tui/widgets/status.py` (modified from soothe_deepagents)

**Modification**: Add protocol event queue to status bar.

**Interface addition**:

```python
class StatusBar(Widget):
    """Status bar showing activity.

    deepagents original: tool calls, loading, model info
    SOOTHE addition: protocol events queue
    """

    # SOOTHE: Add protocol event queue
    _protocol_events: deque[dict] = deque(maxlen=5)

    def show_protocol_event(self, event: dict):
        """Queue a protocol event for display.

        Args:
            event: Protocol event to display
        """
        self._protocol_events.append(event)
        self._update_display()

    def _render_activity(self) -> str:
        """Render recent activity (tool calls + protocol events).

        Returns:
            Multi-line string with last 5 activity items
        """
        lines = []

        # Tool calls (deepagents original)
        for tool_call in self._recent_tool_calls[-3:]:
            lines.append(self._render_tool_call(tool_call))

        # SOOTHE: Protocol events
        for event in self._protocol_events[-2:]:
            widget = ProtocolEventWidget()
            lines.append(widget.render_event(event))

        return "\n".join(lines)
```

**Display behavior**:
- Last 3 tool calls + last 2 protocol events shown
- Protocol events scroll out as new events arrive
- Max queue size: 5 events
- Events filtered by verbosity before queueing (in adapter)

### Plan Tree Widget

**File**: Keep existing `PlanTree` widget from Soothe (in `widgets/` or jobs_dashboard).

**Integration**: Add to TUI layout as collapsible panel.

**Behavior**:
- Receives `soothe.plan.*` events via app event bus
- Renders hierarchical plan structure (steps, substeps)
- Toggleable with Ctrl+T (preserves Soothe shortcut)
- Auto-shows when `soothe.plan.created` event received
- Auto-hides when plan completes or becomes inactive

**Layout integration** (in `app.py compose()`):

```python
def compose(self) -> ComposeResult:
    """Compose TUI layout.

    Layout order:
    1. ConversationPanel (messages - deepagents)
    2. PlanTree (Soothe addition - collapsible)
    3. ChatInput (advanced input - deepagents)
    4. StatusBar (status + protocol events - deepagents modified)
    """
    yield ConversationPanel()  # deepagents
    yield PlanTree(id="plan-tree")  # SOOTHE: plan visualization
    yield ChatInput()  # deepagents
    yield StatusBar()  # deepagents (modified)
```

### Verbosity Filtering (RFC-501)

**Implementation**: In `SootheBackendAdapter._adapt_protocol_event()`

**Filtering rules**:

| Verbosity Tier | Protocol Events Shown |
|----------------|----------------------|
| `quiet` | None (suppress all) |
| `minimal` | `soothe.plan.*` only |
| `normal` | `soothe.plan.*`, `soothe.context.*`, `soothe.memory.*` |
| `detailed` | All (`plan`, `context`, `memory`, `policy`, `thread`, `goal`) |

**Implementation**:

```python
def _adapt_protocol_event(self, event: dict) -> Optional[dict]:
    """Filter protocol event by verbosity tier.

    Args:
        event: Protocol event dict

    Returns:
        Filtered event dict, or None if suppressed
    """
    verbosity = self.config.verbosity  # RFC-501 tier
    event_type = event.get("type", "")

    # Quiet: suppress all protocol events
    if verbosity == "quiet":
        return None

    # Minimal: show only plan events
    if verbosity == "minimal":
        if not event_type.startswith("soothe.plan"):
            return None

    # Normal: show plan + context + memory
    if verbosity == "normal":
        if event_type.startswith("soothe.policy"):
            return None  # Policy too detailed for normal mode

    # Detailed: show all
    return event
```

## Section 4: Thread Management Integration Specification

### ThreadBackendBridge Protocol

**Purpose**: Connect deepagents thread_selector UI to Soothe's ThreadContextManager persistence backends.

**File**: `src/soothe_cli/tui/thread_backend_bridge.py`

**Interface**:

```python
class ThreadBackendBridge:
    """Bridge for thread_selector to use Soothe persistence.

    Mimics: deepagents SessionManager interface
    Uses: Soothe ThreadContextManager backend (RFC-303)
    """

    def __init__(self, thread_manager: ThreadContextManager, daemon_client: DaemonClient):
        """Initialize with Soothe thread components."""
        self.thread_manager = thread_manager
        self.daemon_client = daemon_client

    async def list_threads_for_ui(self) -> List[dict]:
        """List threads in deepagents thread_selector format.

        Returns:
            List of thread metadata dicts with fields:
            - id: str
            - created_at: datetime
            - updated_at: datetime
            - message_count: int
            - preview: str (first user message, 100 chars)
            - tags: List[str]
            - has_plan: bool (SOOTHE addition)
            - status: str (SOOTHE: running/completed/archived)
        """
        threads = await self.thread_manager.list_threads()

        return [
            {
                "id": t.id,
                "created_at": t.created_at,
                "updated_at": t.last_activity,
                "message_count": len(t.messages),
                "preview": t.messages[0].content[:100] if t.messages else "",
                "tags": t.tags or [],
                # SOOTHE: Additional metadata
                "has_plan": t.has_active_plan,
                "status": t.status,  # running/completed/archived
            }
            for t in threads
        ]

    async def load_thread_messages(self, thread_id: str) -> List[dict]:
        """Load thread messages in deepagents format.

        Args:
            thread_id: Thread ID to load

        Returns:
            List of message dicts with fields:
            - role: str (user/assistant)
            - content: str
            - timestamp: datetime
            - has_protocol_events: bool (SOOTHE addition)
        """
        thread_data = await self.thread_manager.load_thread(thread_id)

        messages = []
        for msg in thread_data.messages:
            messages.append({
                "role": msg.role,
                "content": msg.content,
                "timestamp": msg.timestamp,
                # SOOTHE: Track protocol events in preview
                "has_protocol_events": len(msg.protocol_events) > 0,
            })

        return messages

    async def resume_thread(self, thread_id: str) -> dict:
        """Resume thread via daemon.

        Args:
            thread_id: Thread ID to resume

        Returns:
            Resume status dict

        Raises:
            ThreadResumeError: If daemon fails to load thread
        """
        # Send resume command to daemon
        await self.daemon_client.send_command({
            "type": "resume_thread",
            "thread_id": thread_id,
        })

        # Wait for daemon confirmation
        response = await self.daemon_client.receive_response()

        if response.get("status") == "success":
            return {"thread_id": thread_id, "status": "resumed"}
        else:
            raise ThreadResumeError(response.get("error", "Unknown error"))
```

### thread_selector.py Modifications

**File**: `src/soothe_cli/tui/widgets/thread_selector.py` (copied from soothe_deepagents)

**Modification**: Replace backend connection with ThreadBackendBridge.

```python
# deepagents original (lines ~100-150):
# from soothe_deepagents_cli.sessions import SessionManager
# self.session_manager = SessionManager(...)
# threads = await self.session_manager.list_threads()

# SOOTHE: Use thread backend bridge
from soothe.ux.tui.thread_backend_bridge import ThreadBackendBridge

self.thread_bridge = ThreadBackendBridge(
    thread_manager=self.thread_manager,
    daemon_client=self.daemon_client,
)
threads = await self.thread_bridge.list_threads_for_ui()

# Rest of thread_selector UI logic unchanged
```

### Thread Metadata Display Enhancements

**Additions to thread rendering** (in `_render_thread_row` method):

```python
def _render_thread_row(self, thread: dict) -> Panel:
    """Render thread row with Soothe-specific indicators."""
    # Base row (deepagents style)
    row_text = f"{thread['id'][:8]} | {thread['preview']}"

    # SOOTHE: Add indicators
    indicators = []

    if thread.get("has_plan"):
        indicators.append("📋")  # Has active plan

    if thread.get("status") == "running":
        indicators.append("▶️")  # Currently running

    if thread.get("tags"):
        indicators.append(f"🏷️ {len(thread['tags'])}")  # Has tags

    if indicators:
        row_text += " " + " ".join(indicators)

    return Panel(row_text, subtitle=f"{thread['message_count']} messages")
```

### Thread Actions Extension

**deepagents thread_selector actions**:
| Key | Action | deepagents Behavior |
|-----|--------|-------------------|
| `enter` | `resume_thread` | Load thread and switch to chat |
| `d` | `delete_thread` | Delete thread permanently |
| `v` | `view_messages` | Preview thread messages |

**Soothe additional actions**:
| Key | Action | Soothe Behavior |
|-----|--------|----------------|
| `a` | `archive_thread` | Mark thread as archived (hide from active list) |
| `e` | `export_thread` | Export thread as JSON/Markdown file |
| `t` | `edit_tags` | Add/edit thread tags |

**Implementation** (in `thread_selector.py`):

```python
# SOOTHE: Extend key bindings
KEY_BINDINGS = {
    "enter": "resume_thread",  # deepagents original
    "d": "delete_thread",      # deepagents original
    "v": "view_messages",      # deepagents original
    # SOOTHE additions:
    "a": "archive_thread",
    "e": "export_thread",
    "t": "edit_tags",
}

def action_archive_thread(self):
    """Archive selected thread."""
    # SOOTHE: Mark thread as archived via ThreadContextManager
    ...

def action_export_thread(self):
    """Export thread to file."""
    # SOOTHE: Export via ThreadContextManager
    ...

def action_edit_tags(self):
    """Edit thread tags."""
    # SOOTHE: Update tags via ThreadContextManager
    ...
```

## Section 5: Jobs Dashboard Integration Specification

### Jobs Screen Mode

**Purpose**: Preserve Soothe's jobs dashboard (RFC-203) as alternate screen in deepagents TUI.

**Files**:
- Keep: `src/soothe_cli/tui/jobs_screen.py`
- Keep: `src/soothe_cli/tui/jobs_dashboard.py`
- Modify: `src/soothe_cli/tui/app.py` to support screen switching

### Screen Switching Architecture

**Implementation** (in `app.py`):

```python
from soothe.ux.tui.jobs_screen import JobsScreen

class SootheApp(App):  # Modified from soothe_deepagents App

    # SOOTHE: Add jobs mode flag
    _jobs_mode: bool = False

    def __init__(self, jobs_mode: bool = False, **kwargs):
        """Initialize app with mode selection."""
        super().__init__(**kwargs)
        self._jobs_mode = jobs_mode

    def on_mount(self):
        """Mount app with initial screen.

        deepagents original: push ChatScreen
        SOOTHE: push JobsScreen if jobs mode
        """
        if self._jobs_mode:
            # SOOTHE: Launch jobs dashboard
            self.push_screen(JobsScreen())
        else:
            # deepagents default: chat mode
            self.push_screen(ChatScreen())

    def action_switch_to_jobs(self):
        """Switch from chat to jobs dashboard."""
        # SOOTHE: Push jobs screen
        self.push_screen(JobsScreen())

    def action_switch_to_chat(self):
        """Switch from jobs back to chat."""
        # SOOTHE: Pop back to chat screen
        self.pop_screen()
```

### Jobs Screen Composition

**File**: `src/soothe_cli/tui/jobs_screen.py` (modified)

**Reuse deepagents widgets where applicable**:

```python
from textual.screen import Screen

# SOOTHE: Import deepagents widgets for reuse
from soothe.ux.tui.widgets.status import StatusBar  # deepagents status bar
from soothe.ux.tui.widgets.loading import LoadingWidget  # deepagents loading

# Keep Soothe-specific jobs widgets
from soothe.ux.tui.jobs_dashboard import (
    GoalProgressWidget,
    PlanTreeWidget,
    ExecutionQueueWidget,
)

class JobsScreen(Screen):
    """Jobs dashboard screen.

    Layout:
    1. GoalProgressWidget (Soothe-specific)
    2. PlanTreeWidget (Soothe-specific)
    3. ExecutionQueueWidget (Soothe-specific)
    4. StatusBar (deepagents reuse)
    """

    def compose(self) -> ComposeResult:
        yield GoalProgressWidget(id="goal-progress")
        yield PlanTreeWidget(id="plan-tree")
        yield ExecutionQueueWidget(id="execution-queue")
        yield StatusBar(id="status")  # SOOTHE: reuse deepagents
```

### Job Event Stream

**Modification**: Add job-specific event streaming in `SootheBackendAdapter`.

```python
async def stream_job_events(self) -> AsyncIterator[Tuple]:
    """Stream job-specific events.

    Used by: JobsScreen instead of chat message stream
    Source: SootheRunner in loop-rail mode (RFC-203)
    """
    async for namespace, mode, data in self.daemon_client.receive_events():
        event_type = data.get("type", "")

        # Route job-specific events
        if event_type.startswith("soothe.goal"):
            yield ("jobs", "goal_update", data)
        elif event_type.startswith("soothe.executor"):
            yield ("jobs", "queue_update", data)
        elif event_type.startswith("soothe.plan"):
            yield ("jobs", "plan_update", data)
        # Tool calls still relevant in jobs mode
        elif mode == "messages" and "tool_call" in str(data):
            yield (namespace, mode, data)
```

### Jobs Dashboard Widgets

**GoalProgressWidget** (Soothe-specific, kept unchanged):
- Shows progress bars for executing goal batch
- Updates on `soothe.goal.batch_started` and `soothe.goal.report` events
- Display: completed/failed count, summary

**ExecutionQueueWidget** (Soothe-specific, kept unchanged):
- Shows execution queue (pending/executing/completed steps)
- Updates on `soothe.executor.queue_updated` events
- Display: queue items with status indicators

### CLI Flag Integration

**Launcher modification** (in `src/soothe/ux/cli/execution/launcher.py`):

```python
def run_tui(
    cfg: SootheConfig,
    *,
    jobs_mode: bool = False,  # SOOTHE: new flag
    thread_id: str | None = None,
    initial_prompt: str | None = None,
):
    """Launch TUI with mode selection.

    Args:
        cfg: Soothe configuration
        jobs_mode: Launch jobs screen instead of chat
        thread_id: Resume specific thread
        initial_prompt: Auto-submit prompt on launch
    """
    from soothe.ux.tui.app import SootheApp

    app = SootheApp(
        config=cfg,
        jobs_mode=jobs_mode,  # SOOTHE: pass mode flag
        thread_id=thread_id,
        initial_prompt=initial_prompt,
    )
    app.run()
```

**CLI command integration** (in `src/soothe/ux/cli/commands/jobs_cmd.py`):

```python
@app.command("jobs")
def jobs_cmd(
    action: str,  # "run"
    task: str,
    headless: bool = False,
):
    """Autonomous goal execution mode.

    CLI: 'soothe jobs run "task"'
    TUI: Launches JobsScreen
    """
    if headless:
        run_jobs_headless(task)
    else:
        # SOOTHE: Launch TUI in jobs mode
        run_tui(config=cfg, jobs_mode=True, initial_prompt=task)
```

## Section 6: CLI Command Structure Specification

### CLI Entry Point (Unchanged)

**File**: `src/soothe/ux/cli/main.py`

**Preservation**: Keep Typer app structure entirely unchanged.

**Commands**:
| Command | Behavior | TUI Integration |
|---------|----------|----------------|
| `soothe` | Launch TUI | Deepagents TUI with daemon backend |
| `soothe -p "prompt"` | Headless single-shot | Headless execution (unchanged) |
| `soothe --no-tui` | Stream to stdout | Headless execution (unchanged) |

### Subcommands (Unchanged)

**Daemon commands** (`src/soothe/ux/cli/commands/daemon_cmd.py`):
| Action | Behavior |
|--------|----------|
| `daemon start` | Start daemon process |
| `daemon stop` | Stop daemon process |
| `daemon status` | Check daemon status |
| `daemon restart` | Restart daemon |

**Thread commands** (`src/soothe/ux/cli/commands/thread_cmd.py`):
| Action | Behavior | UI Enhancement |
|--------|----------|----------------|
| `thread list` | List threads | Launch thread_selector TUI if terminal |
| `thread show ID` | Show thread details | Plain text output |
| `thread continue ID` | Resume thread | Launch main TUI with thread loaded |
| `thread archive ID` | Archive thread | Plain text confirmation |
| `thread delete ID` | Delete thread | Plain text confirmation |
| `thread export ID` | Export thread | Save to file |
| `thread stats ID` | Thread stats | Plain text stats |
| `thread tag ID` | Edit tags | Plain text prompt |

**Config commands** (`src/soothe/ux/cli/commands/config_cmd.py`):
| Action | Behavior |
|--------|----------|
| `config show` | Show active config |
| `config init` | Create default config |
| `config validate` | Validate config |

**Health/Status commands** (`src/soothe/ux/cli/commands/health_cmd.py`, `status_cmd.py`):
| Command | Behavior |
|---------|----------|
| `health` | Check daemon, protocols, backends |
| `status` | Show system status |

**Jobs command** (`src/soothe/ux/cli/commands/jobs_cmd.py`):
| Action | Behavior |
|--------|----------|
| `jobs run "task"` | Launch jobs TUI |
| `jobs run "task" --headless` | Run headless |

### Slash Commands in TUI

**deepagents slash commands** (in `command_registry.py`):
| Command | Behavior |
|---------|----------|
| `/help` | Show command help |
| `/exit`, `/quit` | Exit TUI, stop thread |
| `/clear` | Clear conversation |
| `/resume` | Launch thread selector |
| `/agents` | List agents (deepagents - not used) |
| `/skills` | List skills (deepagents - not used) |

**Soothe additional slash commands**:
| Command | Behavior | Implementation |
|---------|----------|----------------|
| `/plan` | Show/hide plan tree | `self.plan_widget.toggle()` |
| `/memory` | Show memory stats | Query backend_adapter |
| `/context` | Show context stats | Query backend_adapter |
| `/policy` | Show active policy | Query backend_adapter |
| `/detach` | Detach TUI, leave daemon running | Exit with daemon running |

**Registration** (in `app.py`):

```python
# SOOTHE: Register additional slash commands
self.command_registry.register("/plan", self.action_show_plan)
self.command_registry.register("/memory", self.action_show_memory)
self.command_registry.register("/context", self.action_show_context)
self.command_registry.register("/policy", self.action_show_policy)
self.command_registry.register("/detach", self.action_detach)
```

## Implementation Phases

Five-day migration: (1) File copy — copy 25-30 files from soothe_deepagents-cli to `src/soothe_cli/tui/`, delete 7 old Soothe TUI files, update `__init__.py` imports; (2) Backend integration — create `soothe_backend_adapter.py` and `thread_backend_bridge.py`, modify `app.py` backend connections (5 locations), modify thread selector backend; (3) Protocol rendering — create `widgets/protocol_event.py`, modify `widgets/status.py` for protocol queue, add `_handle_protocol_event()` to `app.py`, integrate plan tree widget; (4) Feature integration — integrate jobs screen mode, register Soothe slash commands, add thread actions (archive, export, tags); (5) Testing & polish — `./scripts/verify_finally.sh`, lint, integration tests, IG documentation.

## Success Criteria

TUI launches with all deepagents widgets; thread resume UI connects to Soothe persistence and displays thread metadata; protocol events render in status bar + plan tree; jobs dashboard works as alternate screen receiving job events; CLI commands unchanged; RFC-501 verbosity filtering applied to protocol events; daemon WebSocket streaming seamless; `verify_finally.sh` passes; deepagents feature parity (autocomplete, approval UI, diff viewer); no regression in existing Soothe workflows.

## Risks and Mitigations

deepagents runtime assumptions (mitigated by `SootheBackendAdapter` mimicking `(namespace, mode, data)` stream format); protocol event rendering (mitigated by `ProtocolEventWidget` following deepagents one-liner indicator style); thread selector incompatibility (mitigated by `ThreadBackendBridge` field mapping with graceful missing-field handling); jobs screen integration (mitigated by deepagents `push_screen`/`pop_screen` lifecycle); breaking existing workflows (mitigated by unchanged CLI Typer structure, subcommands, flags, daemon lifecycle); future deepagents updates sync (mitigated by `# SOOTHE:` modification markers and documented fork point).

## Dependencies

### From deepagents-cli (Copy, Not Import)

- `app.py`, `input.py`, `sessions.py`, `theme.py`
- `widgets/*.py` (20 files)
- Supporting modules: `tools.py`, `tool_display.py`, `clipboard.py`, etc.

### Keep Soothe Dependencies

- **Typer** - CLI structure
- **SootheRunner** - Protocol orchestration
- **DaemonClient** - WebSocket connection
- **ThreadContextManager** - Thread persistence
- **Protocol backends** - Context, memory, planner, policy, durability

### External Dependencies

- **textual** (already used)
- **rich** (already used)
- **No new external dependencies** - All copied code uses existing deps

## References

- RFC-000: System Conceptual Design
- RFC-001: Core Modules Architecture
- RFC-302: Daemon Communication Protocol
- RFC-500: CLI TUI Architecture (current)
- RFC-501: VerbosityTier Unification
- RFC-203: Loop-Rail Mode
- RFC-303: Unified Thread Management
- RFC-600: Plugin Extension System
- Design draft: `docs/archive/drafts/2026-04-13-deepagents-cli-migration-design.md`