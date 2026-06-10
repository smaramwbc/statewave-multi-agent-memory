<!-- statewave:begin (auto-managed — edits here are overwritten) -->
# Statewave project memory

This workspace has a Statewave “project brain” exposed over MCP (server id: `statewave`). It holds this project's summary, conventions, documentation, git history, code structure and remembered facts, scoped to subject `repo:smaramwbc.statewave-multi-agent-memory`.

## Read it first

Before answering questions about THIS project — conventions, architecture, history, structure, prior decisions, “how/why does X work”, “what changed” — call the `statewave_get_context` MCP tool (subject `repo:smaramwbc.statewave-multi-agent-memory`, `query` = the user's ask) and ground your answer in what it returns. Prefer it over guessing or re-deriving from scratch.

## Persist durable facts

When the user states a durable preference, decision, or fact worth remembering across sessions (e.g. “my favorite color is red”, “we use X for Y”, “always do Z here”), call the `statewave_ingest_episode` MCP tool (subject `repo:smaramwbc.statewave-multi-agent-memory`, kind `chat.note`, text = the fact). Capture stable facts/preferences/decisions only — not transient chatter, and not code you are about to write. The user sees and can approve each tool call.

Immediately after a successful `statewave_ingest_episode`, call the `statewave_compile_subject` MCP tool (subject `repo:smaramwbc.statewave-multi-agent-memory`). An ingested episode is raw and not yet retrievable; compiling distils it into the durable memory that `statewave_get_context` returns next time.

Never invent Statewave results; if the tool is unavailable, say so plainly.
<!-- statewave:end -->
