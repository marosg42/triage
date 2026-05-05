# triage

Workflow failure analysis for the `sqa-cloud-deployment-pipeline`. The `/analyze-workflow-failures` skill diagnoses GitHub Actions run failures using logs from Swift object storage.

## Setup

This repo is meant to be used inside an **LXD container**. The source checkouts below are
bind-mounted into the container from the host — they are read-only reference material for
stack trace lookups and are not modified by the skill.

**Required bind mounts:**
- `~/sqa-cloud-deployment-pipeline/` — GitHub Actions workflow definitions
- `~/cpe/foundation/` — FCE Python library

**Required local directories** (created on first run, or create manually):
- `outputs/` — analysis reports written here as `<UUID>-analysis.md`
- `tmp/` — scratch space during analysis

## MCP server

The skill requires the `swift-mcp` MCP server for all artifact retrieval. Configure it by
creating a `.mcp.json` file in this directory:

```json
{
  "mcpServers": {
    "swift-mcp": {
      "type": "sse",
      "url": "http://<swift-mcp-host>:8000/sse"
    }
  }
}
```

The server runs as an SSE service. Replace `<swift-mcp-host>` with the host running `swift-mcp`.

If the MCP server is unavailable, the skill will stop immediately — no partial analysis.

## Usage

```
/analyze-workflow-failures <solutions-run-uuid>
```

or

```
analyze workflow <UUID>
```
