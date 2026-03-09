# MCP Server for ServiceNow

An [MCP (Model Context Protocol)](https://modelcontextprotocol.io/) server that enables AI agents to act as **ServiceNow Developers** — building integrations, managing configurations, resolving incidents, and operating across multiple instances.

Built with [FastMCP](https://github.com/jlowin/fastmcp) and designed as a [Claude Skill](https://docs.anthropic.com/en/docs/claude-code).

## Features

- **38 tools** across 5 categories: Configuration, Read/Inspect, Operations, CI/CD, and Knowledge Base
- **Multi-instance support** — target sandbox, dev, test, or production with every tool call
- **3-tier table access** — PII tables blocked, operational tables (incident, sc_task) accessible, metadata tables unrestricted
- **Update Set workflow** — create, track, validate, export to Git, and promote through CI/CD pipeline
- **Safety-first** — scheduled jobs always `active=false`, no hardcoded credentials, blocked PII tables

## Quick Start

### 1. Clone & Install

```bash
git clone https://github.com/dhravesh07/mcp-servicenow.git
cd mcp-servicenow
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure

Copy the example configs:

```bash
cp .mcp.json.example .mcp.json
cp sn_instances.json.example sn_instances.json
```

Edit `.mcp.json` with your ServiceNow credentials:

```json
{
  "mcpServers": {
    "servicenow-dev": {
      "type": "stdio",
      "command": ".venv/bin/python",
      "args": ["server.py"],
      "env": {
        "SN_INSTANCE": "https://your-instance.service-now.com",
        "SN_USER": "your-service-account",
        "SN_PASSWORD": "your-password"
      }
    }
  }
}
```

Edit `sn_instances.json` for multi-instance support (optional).

### 3. Connect to Claude

Add the MCP server to your Claude Desktop or Claude Code configuration, pointing to your `.mcp.json`.

### 4. Instance Roles

Your ServiceNow service account needs:

| Role | Purpose |
|------|---------|
| `rest_api_explorer` | Table API access |
| `personalize_dictionary` | Add columns via sys_dictionary |
| `admin` or `update_set_admin` | Manage Update Sets |

## Tool Categories

### Configuration & Build (8 tools)
Create Update Sets, tables, columns, REST messages, scripted REST APIs, scheduled jobs, and inbound email actions.

### Read & Inspect (17 tools)
Read records, query tables, count records, describe schemas, inspect scheduled jobs, Script Includes, Business Rules, REST messages, application scopes, and deep-investigate any artifact from a URL.

### Operations (4 tools)
Update records, resolve incidents, close tasks, and batch-update records.

### CI/CD Pipeline (6 tools)
Validate and export Update Sets to Git, promote through instances, deploy promotion helpers, and check promotion status.

### Knowledge Base (3 tools)
List KBs and categories, create articles (auto-categorized, always draft), and search existing articles.

## Safety Model

### Table Access Tiers

| Tier | Tables | Access |
|------|--------|--------|
| **Blocked** | sys_user, sn_hr_core_case, fm_expense_line, alm_asset, cmn_location | Never accessible — PII/sensitive |
| **Operational** | incident, sc_task, change_request, problem, sys_user_group, cmdb_ci, task_sla | Full access via tools |
| **Unrestricted** | u_* custom tables, sys_dictionary, sys_transform_map, etc. | Full access |

### Other Safety Measures

- Scheduled jobs always created with `active=false`
- No credentials hardcoded in REST Messages or scripts
- Multi-instance config uses env var references, not inline passwords
- Bulk updates limited to 200 records per call

## Multi-Instance Configuration

Configure `sn_instances.json` with your pipeline:

```json
{
  "service_account": "ai_config_bot",
  "pipeline": ["sandbox", "dev", "test", "prod"],
  "instances": {
    "sandbox": {
      "url": "https://your-sandbox.service-now.com",
      "password_env": "SN_SANDBOX_PASSWORD"
    }
  }
}
```

Set passwords via environment variables (in `.env` or shell):

```bash
export SN_SANDBOX_PASSWORD=your-password
export SN_PROD_PASSWORD=your-password
```

Then target any instance with every tool call:

```
describe_table(table="incident", instance="prod")
resolve_incident(sys_id="abc123", close_code="Solved Remotely", ..., instance="prod")
```

## License

MIT
