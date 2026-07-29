# agent-client-protocol 0.11.1 — API reference for Ziggy implementers

Ground truth: the installed SDK at `.venv/lib/python3.12/site-packages/acp/`.
Read it directly when in doubt. Full generated model field dump: `sdk-schema-fields.txt`.

**Hard rule (REQ-001):** SDK types may be imported ONLY inside `src/ziggy/acp/`.
Everything outside `ziggy.acp` uses Ziggy-native types.

## Module map

| Module | Contents |
|--------|----------|
| `acp` (`__init__`) | Re-exports everything below + helper constructors |
| `acp.interfaces` | `Client` and `Agent` typing Protocols (the two roles) |
| `acp.client.connection` | `ClientSideConnection` — drive an agent (Ziggy-as-client) |
| `acp.agent.connection` | `AgentSideConnection` — serve a client (`ziggy serve`) |
| `acp.connection` | `Connection` (JSON-RPC over NDJSON), `StreamObserver`, `StreamEvent`, `StreamDirection` |
| `acp.stdio` | `stdio_streams()`, `spawn_agent_process()`, `spawn_client_process()`, `spawn_stdio_connection()` |
| `acp.transports` | `spawn_stdio_transport()`, `default_environment()`, `DEFAULT_INHERITED_ENV_VARS` |
| `acp.core` | `run_agent(agent)` (serve loop), `connect_to_agent(client, w, r)`, `DEFAULT_STDIO_BUFFER_LIMIT_BYTES` (50 MiB) |
| `acp.meta` | `AGENT_METHODS`, `CLIENT_METHODS` (py-name → wire-name), `PROTOCOL_VERSION = 1` |
| `acp.exceptions` | `RequestError` (JSON-RPC error, with classmethod constructors) |
| `acp.schema` | ~262 generated pydantic models (camelCase aliases) |
| `acp.helpers` | `text_block()`, `update_agent_message_text()`, `start_tool_call()`, etc. |
| `acp.contrib` | Optional session-state/tool-call/permission trackers (do not rely on) |

## Driving an agent (Ziggy as ACP client)

```python
from acp import spawn_agent_process, ClientSideConnection, text_block
from acp.schema import ClientCapabilities, FileSystemCapabilities, Implementation

class MyClient:            # implements acp.interfaces.Client (structural Protocol)
    async def session_update(self, session_id, update, **kwargs) -> None: ...
    async def request_permission(self, session_id, tool_call, options, **kwargs): ...
    async def read_text_file(self, session_id, path, line=None, limit=None, **kwargs): ...
    async def write_text_file(self, session_id, path, content, **kwargs): ...
    async def create_terminal(self, session_id, command, args=None, env=None,
                              cwd=None, output_byte_limit=None, **kwargs): ...
    async def terminal_output(self, session_id, terminal_id, **kwargs): ...
    async def release_terminal(self, session_id, terminal_id, **kwargs): ...
    async def wait_for_terminal_exit(self, session_id, terminal_id, **kwargs): ...
    async def kill_terminal(self, session_id, terminal_id, **kwargs): ...
    # unsupported surfaces: raise acp.RequestError.method_not_found(...)
    def on_connect(self, conn): self.conn = conn

async with spawn_agent_process(MyClient(), "npx", "claude-agent-acp",
                               env={...explicit...}, cwd=str(workdir)) as (conn, process):
    init = await conn.initialize(protocol_version=1,
        client_capabilities=ClientCapabilities(
            fs=FileSystemCapabilities(read_text_file=True, write_text_file=True),
            terminal=True),
        client_info=Implementation(name="ziggy", version="0.1.0"))
    # init: InitializeResponse(protocol_version, agent_capabilities, auth_methods, agent_info)
    sess = await conn.new_session(cwd=str(workdir), mcp_servers=[])
    resp = await conn.prompt(session_id=sess.session_id, prompt=[text_block(prompt_text)])
    # resp.stop_reason ∈ {'end_turn','max_tokens','max_turn_requests','refusal','cancelled'}
    await conn.cancel(session_id=...)   # notification, for Ctrl-C/timeout
```

- `spawn_agent_process(to_client, command, *args, env=, cwd=, transport_kwargs=, **connection_kwargs)`
  is an async context manager yielding `(ClientSideConnection, asyncio.subprocess.Process)`.
  `to_client` may be a `Client` instance or a callable `(Agent) -> Client`.
  On exit it closes stdin, waits `shutdown_timeout=2.0`s, then terminate → kill
  (single process only — Ziggy must additionally handle the process GROUP for tree teardown;
  pass `transport_kwargs={"limit": ...}` to raise the 64 KiB stream limit; use
  `start_new_session` via… NOT exposed — Ziggy launches its own subprocess when it
  needs process-group control, using `spawn_stdio_transport` semantics as reference,
  or wraps the yielded process's pid with `os.killpg` after `start_new_session` —
  see decision in `process-lifecycle.md`).
- Env: `spawn_stdio_transport` merges `default_environment()` (POSIX:
  HOME, LOGNAME, PATH, SHELL, TERM, USER) with the `env=` mapping. Ziggy passes its
  fully-computed env explicitly.
- **Raw frame capture**: `conn._conn` is the low-level `Connection`;
  `connection_kwargs={"observers": [cb]}` reaches `Connection(observers=...)`.
  Observer signature: `def cb(event: StreamEvent) -> None | Awaitable[None]`,
  `event.direction ∈ {INCOMING, OUTGOING}`, `event.message` = deep-copied dict frame.
- Errors from the far side surface as `acp.RequestError` (with `.code`, `.data`);
  connection loss rejects in-flight futures with `ConnectionError`.

## Serving a client (`ziggy serve`, Ziggy as ACP agent)

```python
from acp import run_agent
from acp.interfaces import Agent  # structural: implement methods, no subclassing needed

class ZiggyAgent:
    async def initialize(self, protocol_version, client_capabilities=None,
                         client_info=None, **kw) -> InitializeResponse: ...
    async def new_session(self, cwd, additional_directories=None,
                          mcp_servers=None, **kw) -> NewSessionResponse: ...
    async def prompt(self, session_id, prompt, **kw) -> PromptResponse: ...
    async def set_config_option(self, config_id, session_id, value, **kw): ...
    async def cancel(self, session_id, **kw) -> None: ...
    async def authenticate(self, method_id, **kw): ...
    # Unsupported (load_session, fork/resume/close/list, set_session_mode, ext_*):
    #   raise RequestError.method_not_found(...)
    def on_connect(self, conn): self.conn = conn   # conn: AgentSideConnection

await run_agent(ZiggyAgent())      # binds real stdio, runs until EOF, then closes
```

- `AgentSideConnection` gives the serving side: `session_update(session_id, update)`,
  `request_permission(session_id, tool_call, options) -> RequestPermissionResponse`.
- `run_agent` returns when client stdio reaches EOF (disconnect) — Ziggy's
  disconnect-cancels-run semantics hook there (`finally` + own teardown).
- Router dispatch: unknown methods raise `RequestError.method_not_found`;
  handler `ValidationError` → `invalid_params`; other exceptions → `internal_error`.
  Handlers returning pydantic models are dumped `by_alias=True, exclude_none=True, exclude_unset=True`.

## Session update variants (`SessionNotification.update` discriminated by `sessionUpdate`)

| `session_update` literal | Model | Payload |
|---|---|---|
| `user_message_chunk` | UserMessageChunk | `content: ContentBlock` |
| `agent_message_chunk` | AgentMessageChunk | `content: ContentBlock` |
| `agent_thought_chunk` | AgentThoughtChunk | `content: ContentBlock` |
| `tool_call` | ToolCallStart | `tool_call_id, title, kind?, status?, content?, locations?, raw_input?, raw_output?` |
| `tool_call_update` | ToolCallProgress | same fields, all optional except id |
| `plan` | AgentPlanUpdate | `entries: [PlanEntry(content, priority, status)]` |
| `plan_update` / `plan_removed` | AgentPlanContentUpdate / AgentPlanRemovedUpdate | v2-ish (SDK literal is `plan_update`, not `plan_content_update`); normalize to generic plan events |
| `available_commands_update` | AvailableCommandsUpdate | `available_commands` |
| `current_mode_update` | CurrentModeUpdate | `current_mode_id` |
| `config_option_update` | ConfigOptionUpdate | `config_options` |
| `session_info_update` | SessionInfoUpdate | `title?, updated_at?` |
| `usage_update` | UsageUpdate | `used, size, cost?` |

Content blocks: `TextContentBlock(type='text', text)`, Image/Audio/Resource/EmbeddedResource.
Tool-call content: `ContentToolCallContent(type='content')`,
`FileEditToolCallContent(type='diff', path, new_text, old_text?)`, `TerminalToolCallContent(type='terminal', terminal_id)`.

## Permissions

- Inbound request: `request_permission(session_id, tool_call: ToolCallUpdate, options: [PermissionOption])`.
- `PermissionOption.kind ∈ {allow_once, allow_always, reject_once, reject_always}`.
- Respond `AllowedOutcome(outcome='selected', option_id=...)` (also used to select
  reject options!) or `DeniedOutcome(outcome='cancelled')` when nothing applies
  (e.g., cancellation or no matching option).

## Wire methods actually used by Ziggy v0.1

Agent-bound: `initialize`, `session/new`, `session/prompt`, `session/cancel`,
`session/set_config_option` (server mode only), `authenticate` (surface auth errors only).
Client-bound: `session/update`, `session/request_permission`, `fs/read_text_file`,
`fs/write_text_file`, `terminal/create|output|release|wait_for_exit|kill`.
Everything else (elicitation, mcp/*, nes/*, document/*, providers/*, session/load|list|fork|resume|delete|close,
logout, set_mode) is **declared unsupported**: not advertised in capabilities and answered
with `method_not_found` where reachable.

## Gotchas

1. Import name is `acp`; Ziggy's wrapper package `ziggy.acp` must use absolute
   imports (`from acp import ...`) — never relative confusion.
2. All wire models: camelCase aliases; construct with snake_case field names,
   serialize `by_alias=True`.
3. `conn.cancel()` is a **notification** (no response). The in-flight `prompt()`
   should then resolve with `stop_reason='cancelled'` per spec — but a
   misbehaving agent may never resolve; Ziggy enforces its own grace timeout.
4. The SDK receive loop silently skips malformed JSON lines; Ziggy's observers see
   only parseable frames. Non-JSON garbage on stdout is invisible to observers —
   protocol failure is detected by response timeout / EOF (`ConnectionError`).
5. `StreamObserver` callbacks must be fast/non-blocking; async observers are fired
   as tasks (unordered) — Ziggy uses a sync observer appending to an asyncio.Queue.
6. `spawn_*` shutdown handles only the direct child. Process-GROUP teardown
   (descendants) is Ziggy's job (`process-lifecycle.md`).
7. `Client`/`Agent` are typing Protocols — no base class; implement methods
   structurally. Missing optional methods on the serving side produce
   `AttributeError`-driven internal errors, so explicitly implement + raise
   `RequestError.method_not_found` for unsupported surfaces.
