"""Scenario constants shared by the mock agents and the tests that drive them.

Single source of truth: fixtures emit exactly these values and tests assert
against exactly these values. Everything here is inert data — no imports
beyond the stdlib, so ``raw_agent.py`` stays SDK-free when it loads this
module as a plain top-level import in subprocess mode.

All secret values below are seeded FAKES: format-valid for the corresponding
token family so redaction patterns must fire, but not real credentials.
"""

from __future__ import annotations

# --- scenario names -------------------------------------------------------

HELLO = "hello"
TOOL_CALLS = "tool_calls"
PERMISSION = "permission"
PERMISSION_READ = "permission_read"
PERMISSION_OUTSIDE = "permission_outside"
FS_OPS = "fs_ops"
MALFORMED = "malformed"
CRASH_MID_TURN = "crash_mid_turn"
IGNORE_CANCEL = "ignore_cancel"
SLOW_STREAM = "slow_stream"
SECRET_LEAK = "secret_leak"
ENV_ECHO = "env_echo"
ECHO_PROMPT = "echo_prompt"
SCRIPTED_JSON = "scripted_json"
PLAN_PROBE = "plan_probe"

ALL_SCENARIOS: tuple[str, ...] = (
    HELLO,
    TOOL_CALLS,
    PERMISSION,
    PERMISSION_READ,
    PERMISSION_OUTSIDE,
    FS_OPS,
    MALFORMED,
    CRASH_MID_TURN,
    IGNORE_CANCEL,
    SLOW_STREAM,
    SECRET_LEAK,
    ENV_ECHO,
    ECHO_PROMPT,
    SCRIPTED_JSON,
    PLAN_PROBE,
)

#: Scenarios the SDK-backed fixture (sdk_agent.py) supports.
SDK_SCENARIOS: tuple[str, ...] = (HELLO, SLOW_STREAM, PERMISSION)

# --- agent identity -------------------------------------------------------

PROTOCOL_VERSION = 1
RAW_AGENT_NAME = "ziggy-mock-raw"
SDK_AGENT_NAME = "ziggy-mock-sdk"
AGENT_VERSION = "0.1.0"
RAW_AGENT_TITLE = "Ziggy Raw Mock Agent"

#: Agent-originated JSON-RPC request ids start here so they can never collide
#: with client-originated ids (clients count up from small integers).
AGENT_REQUEST_ID_START = 10_000

# --- hello ----------------------------------------------------------------

HELLO_CHUNKS: tuple[str, ...] = ("Hello", " from", " mock")

# --- tool_calls -----------------------------------------------------------

TOOL_CALLS_INTRO = "running a tool now"
EXEC_TOOL_CALL_ID = "call-exec-1"
EXEC_TOOL_TITLE = "run mock command"
EXEC_TOOL_RAW_OUTPUT: dict[str, object] = {"stdout": "mock tool output\n", "exitCode": 0}
DIFF_TOOL_CALL_ID = "call-diff-1"
DIFF_TOOL_TITLE = "edit fix.py"
DIFF_PATH = "fix.py"
DIFF_OLD_TEXT = "bug = True\n"
DIFF_NEW_TEXT = "bug = False\n"

# --- permission -----------------------------------------------------------

PERMISSION_TOOL_CALL_ID = "call-perm-1"
PERMISSION_TOOL_TITLE = "dangerous operation"
PERMISSION_ALLOW_OPTION_ID = "allow_once"
PERMISSION_REJECT_OPTION_ID = "reject_once"
PERMISSION_APPROVED_TEXT = "permission approved, proceeding"
PERMISSION_DENIED_TEXT = "denied acknowledged"

# --- permission_read ------------------------------------------------------
# A read permission for a file INSIDE the session cwd: the guarded policy
# ceiling ALLOWS this, so a serving Ziggy must forward the request to the
# connecting ACP client (REQ-012 permission bridge).

PERMISSION_READ_TOOL_CALL_ID = "call-perm-read-1"
PERMISSION_READ_TOOL_TITLE = "read project notes"
PERMISSION_READ_FILE_NAME = "notes.txt"
PERMISSION_READ_APPROVED_TEXT = "read approved, proceeding"
PERMISSION_READ_DENIED_TEXT = "read denied, stopping"

# --- permission_outside ---------------------------------------------------
# An edit permission for a path OUTSIDE any workspace: the policy ceiling
# must deny locally — the upstream client is never asked, even when it is
# scripted to approve (approval cannot exceed the trusted user ceiling).

PERMISSION_OUTSIDE_TOOL_CALL_ID = "call-perm-outside-1"
PERMISSION_OUTSIDE_TOOL_TITLE = "edit system hosts file"
PERMISSION_OUTSIDE_PATH = "/etc/hosts"
PERMISSION_OUTSIDE_APPROVED_TEXT = "outside edit approved (ceiling breach)"
PERMISSION_OUTSIDE_DENIED_TEXT = "outside edit denied"

# --- fs_ops ---------------------------------------------------------------

FS_READ_NAME = "hello.txt"
FS_WRITE_NAME = "out.txt"
FS_WRITE_CONTENT = "written by mock agent\n"
FS_READ_PREFIX = "read: "
FS_DENIED_TEXT = "fs denied"

# --- malformed ------------------------------------------------------------

MALFORMED_FIRST_TEXT = "chunk before garbage"
#: Deliberately truncated JSON — must never parse.
MALFORMED_GARBAGE_LINE = '{"jsonrpc": "2.0", "method": "session/update", "params": {'

# --- crash_mid_turn -------------------------------------------------------

CRASH_FIRST_TEXT = "about to crash"
CRASH_EXIT_CODE = 3

# --- ignore_cancel --------------------------------------------------------

CHILD_PID_PREFIX = "child_pid:"
IGNORE_CANCEL_TICK_PREFIX = "still running "
IGNORE_CANCEL_TICK_SECONDS = 0.5

# --- slow_stream ----------------------------------------------------------

SLOW_STREAM_TICK_PREFIX = "tick "
SLOW_STREAM_DELAY_SECONDS = 0.2
SLOW_STREAM_CHUNK_COUNT = 25

# --- secret_leak ----------------------------------------------------------

SEEDED_SECRETS: dict[str, str] = {
    "anthropic_api_key": "sk-ant-api03-MockFakeSeed" + "0" * 81 + "AA",
    "openai_api_key": "sk-MOCKFAKEmockfake0123456789MOCKFAKEmockfake012345",
    "github_token": "ghp_MockFakeSeed012345678901234567890123",
    "env_value": "MOCK-ENV-SECRET-VALUE-12345",
}

#: The OpenAI-style key is split mid-token across two adjacent chunks to
#: exercise chunk-boundary redaction (REQ-006 bounded look-behind).
OPENAI_SPLIT_AT = 10
_OPENAI = SEEDED_SECRETS["openai_api_key"]

SECRET_LEAK_PRE_TOOL_CHUNKS: tuple[str, ...] = (
    "anthropic key: " + SEEDED_SECRETS["anthropic_api_key"],
    "openai key: " + _OPENAI[:OPENAI_SPLIT_AT],
    _OPENAI[OPENAI_SPLIT_AT:] + " end of key",
)
SECRET_LEAK_TOOL_CALL_ID = "call-secret-1"
SECRET_LEAK_TOOL_TITLE = "leaky tool"
SECRET_LEAK_RAW_OUTPUT: dict[str, object] = {
    "stdout": "token=" + SEEDED_SECRETS["github_token"] + "\n",
    "exitCode": 0,
}
SECRET_LEAK_POST_TOOL_CHUNKS: tuple[str, ...] = ("env secret: " + SEEDED_SECRETS["env_value"],)

# --- env_echo -------------------------------------------------------------

ENV_ECHO_SEPARATOR = ","

# --- echo_prompt ----------------------------------------------------------

#: The received prompt text is echoed back verbatim as agent_message_chunk
#: updates of at most this many characters each, so workflow tests can assert
#: the EXACT composed prompt a downstream step received (delimiters included).
ECHO_PROMPT_CHUNK_CHARS = 4096

# --- scripted_json --------------------------------------------------------
# Mock ORCHESTRATOR PLANNER: the first prompt turn is answered with the
# literal text of $MOCK_PLAN_JSON, every later prompt turn (the repair turn)
# with $MOCK_PLAN_JSON_2 (falling back to $MOCK_PLAN_JSON when unset). The
# payloads reach the subprocess through the agent's trusted-config literal
# ``env`` table — the planning profile forwards no other parent variables.

MOCK_PLAN_JSON_ENV = "MOCK_PLAN_JSON"
MOCK_PLAN_JSON_2_ENV = "MOCK_PLAN_JSON_2"

# --- plan_probe -----------------------------------------------------------
# Planning-isolation probe: emits ONE compact-JSON object describing what the
# planner subprocess can actually observe — its cwd, the entries of that cwd,
# and its sorted environment variable NAMES (never values). The object is not
# a valid plan, so a probing run ends OrchestratorPlanInvalid after repair.

PLAN_PROBE_CWD_KEY = "cwd"
PLAN_PROBE_ENTRIES_KEY = "cwd_entries"
PLAN_PROBE_ENV_KEYS_KEY = "env_keys"
