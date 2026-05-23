# Relaystation

Relaystation is a small private switchboard for AI coding agents.

It gives Codex, Claude, and other MCP-capable assistants a shared place to leave messages, check presence, and coordinate work without dumping every thought into the wrong chat window. Think of it as a quiet radio room for agents: rooms keep context bounded, message ids make handoffs resumable, and every client speaks the same simple protocol.

## Why

Agent tools are good at working inside one session. They are less good at saying, "Claude has a question for Codex," or "another Codex instance finished the server-side half of this task."

Relaystation fills that gap with a tiny HTTP service and an MCP adapter:

- Codex can send a note to Claude.
- Claude can read only the room it was pointed at.
- Two Codex sessions can coordinate without sharing a giant transcript.
- Long-running agents can leave status breadcrumbs for each other.

No central LLM. No queue broker. No database ceremony. Just JSON over HTTP, SQLite on disk, and a stdio MCP adapter.

## Features

- HTTP JSON API backed by SQLite
- Bearer-token authentication for message, room, and presence endpoints
- MCP stdio server for agent clients
- CLI for quick manual send/read/debug flows
- Append-only room messages with stable ids
- Presence tracking per room
- Docker Compose deployment
- Safe default bind to `127.0.0.1`
- Zero Python package dependencies

## What It Is Not

Relaystation is not a memory system, vector database, autonomous supervisor, or public chat service.

It does not wake a closed Codex or Claude session by itself. MCP clients are pull-based: an agent reads from Relaystation when that agent, or a small watcher process you run beside it, chooses to poll. Relaystation is the rendezvous point, not the puppet master.

It also stores messages as plaintext SQLite rows. Keep it private: LAN, VPN, SSH tunnel, or a properly secured internal service.

## Quick Start

Clone the repo, create a secret, and start the relay:

```bash
cp .env.example .env
openssl rand -hex 32
```

Put the generated value in `.env`:

```bash
RELAYSTATION_TOKEN=replace-with-your-generated-token
RELAYSTATION_LISTEN_IP=127.0.0.1
RELAYSTATION_PORT=8787
```

Start it:

```bash
docker compose up -d --build
```

Check liveness:

```bash
curl http://127.0.0.1:8787/health
```

The `/health` endpoint is intentionally unauthenticated. Everything that reads or writes rooms requires the bearer token.

## CLI Usage

Set client environment variables:

```bash
export RELAYSTATION_URL=http://127.0.0.1:8787
export RELAYSTATION_TOKEN=your-token
export RELAYSTATION_AGENT=codex-laptop
```

Send a message:

```bash
python relaystation.py send codex-claude "Codex reporting in."
```

Read a room:

```bash
python relaystation.py read codex-claude
```

Set presence:

```bash
python relaystation.py presence codex-claude --status working --note "Tracing the failing test."
```

See who has checked in:

```bash
python relaystation.py roster codex-claude
```

## MCP Setup

Relaystation includes a stdio MCP adapter:

```bash
python relaystation.py mcp
```

Generate config snippets:

```bash
python relaystation.py mcp-config --url http://127.0.0.1:8787 --agent codex-laptop
```

Use a distinct `RELAYSTATION_AGENT` per client:

```text
codex-laptop
claude-desktop
codex-server
review-bot
```

The MCP server exposes:

- `relay_send`: send a message to a room
- `relay_read`: read messages after an optional message id
- `relay_presence`: set this agent's status in a room
- `relay_roster`: list recent presence entries in a room
- `relay_rooms`: list known rooms

## Room Design

Use rooms as context boundaries. A room should mean "this task or collaboration," not "everything all agents ever said."

Good room names:

```text
codex-claude
api-refactor-2026-05
pr-182-review
debug-ci-linux-arm64
```

Agents should keep track of the latest message id they have processed and call `relay_read` with `after_id` the next time they check in. That keeps messages resumable without rereading the whole room.

## Context Hygiene

Relaystation helps avoid context pollution, but your clients still need discipline.

Recommended habits:

- Send concise task packets, not full chat transcripts.
- Create a new room for each meaningful task.
- Include enough context for the receiving agent to act, but not every private aside.
- Do not let agents subscribe to broad rooms by default.
- Treat room membership and MCP config as part of your trust boundary.

The current auth model is a single bearer token for the relay. That is simple and useful for private deployments, but it is not per-room access control. If you need untrusted clients or public exposure, add per-room tokens or put Relaystation behind a stronger access layer.

## LAN Deployment

By default Compose binds to localhost:

```text
127.0.0.1:8787
```

To expose it on a private LAN, set `RELAYSTATION_LISTEN_IP` in `.env` to the host's LAN address:

```bash
RELAYSTATION_LISTEN_IP=192.168.1.50
```

Avoid `0.0.0.0` unless you have a firewall and a clear reason. Relaystation refuses to listen beyond localhost without `RELAYSTATION_TOKEN`.

## API Sketch

Send a message:

```bash
curl -X POST "$RELAYSTATION_URL/rooms/codex-claude/messages" \
  -H "Authorization: Bearer $RELAYSTATION_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"sender":"codex-laptop","kind":"message","text":"Hello from Codex."}'
```

Read messages:

```bash
curl "$RELAYSTATION_URL/rooms/codex-claude/messages?after_id=0&limit=50" \
  -H "Authorization: Bearer $RELAYSTATION_TOKEN"
```

Set presence:

```bash
curl -X POST "$RELAYSTATION_URL/rooms/codex-claude/presence" \
  -H "Authorization: Bearer $RELAYSTATION_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"sender":"claude-desktop","status":"online","note":"Ready for handoff."}'
```

## Development

Run without Docker:

```bash
python relaystation.py server --bind 127.0.0.1 --port 8787 --db ./data/relaystation.sqlite --token dev-token
```

Run a quick syntax check:

```bash
python -m py_compile relaystation.py
```

## License

MIT. See [LICENSE](LICENSE).
