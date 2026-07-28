Ziggy: Meta-Harness for AI Agents

Ziggy is a meta-harness for AI agents. It provides a single interface to run multiple agents that uses ACP (Agent Client Protocol - https://zed.dev/acp) to interact with the agents. Ziggy is designed to be a robust and flexible framework for orchestrating AI agents, enabling them to work together in workflows, manage and produce structured results.

Requirements:

- Core:
  - Python engine
  - CLI
  - Ziggy also needs to support ACP (Agent Client Protocol) so it can be use in clients that support ACP.
  - Structured Results:
    - Every run produces a `RunResult` — transcript, tool calls, file diffs, permission rejections, timing, and typed errors — with secrets redacted and optional on-disk persistence.
  - Config:
    - Configuration should be managed through a single config file (~/.anvil/config.toml or ./.anvil/config.toml) that can be overridden by environment variables. The config should include agent settings, workflow settings, and orchestrator settings.
  - Observability: 
    - All runs should be logged to a structured log file (~/.anvil/logs) with timestamps, agent names, and run IDs. 
    - The logs should include the structured results of each run, including transcripts, tool calls, file diffs, permission rejections, timing, and typed errors. 

- Agents:
  - One agent interface that speaks ACP (Agent Client Protocol)
  - Built-in Agents:
  	- Devin
  	- OpenCode
  	- Codex (https://github.com/agentclientprotocol/codex-acp)
  	- Claude (https://github.com/agentclientprotocol/claude-agent-acp)
  - Custom agents can be added by implementing the ACP interface and registering them with Ziggy.

- Workflows: 
  - A workflow runs several agent steps as a dependency graph: independent steps run concurrently, dependent steps wait for their inputs, and one step's output can be threaded into another's prompt. Workflows are defined either in declarative YAML or directly in Python with the workflow engine. 
  - When defined in YAML they should be stored in `~/.anvil/workflows` or `./.anvil/workflows` or a direct path at execution. Workflows can also call scripts directly (Python or shell) and can trigger skills directly in the harness.
  - Workflows should be able to also call scripts directly (Python, shell). This is important for building more deterministic controls into the workflows.

- Orchestrator: 
  - The orchestrator is an optional agent that decides what to do based on the prompts. It can either use a single agent to respond or build a workflow of multiple agents to respond. 
  - Any of the registered agents can be used as the orchestrator. 
