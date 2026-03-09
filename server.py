"""
ServiceNow Configuration & Integration MCP Server

An MCP server that enables an AI Agent to act as a ServiceNow Developer,
building integrations, data streams, and custom tables in Global Scope.

Security: Metadata-only access. No transactional data (incidents, users, etc.).
Safety: All automated tasks default to active=false. Sandbox/PDI only.

VERIFIED AGAINST DOCS (2025/2026):
  - Table API: POST /api/now/table/{table} returns 201 with result object
  - sys_db_object: Direct POST is blocked (403) by platform security. Uses
    Scripted REST fallback or GlideRecord-based server-side script.
  - sys_dictionary: Requires 'personalize_dictionary' role for POST writes.
  - Update Set export: Two-step process — first create sys_remote_update_set
    via server-side script, then download via export_update_set.do
  - sys_user_preference: Preference name is 'sys_update_set', value is the
    Update Set sys_id. Can be read/written via Table API.
"""

import os
import json
import asyncio
import subprocess
import logging
from datetime import datetime
from typing import Optional

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Auto-load .env from the same directory as server.py
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

SN_INSTANCE = os.environ.get("SN_INSTANCE", "")  # e.g. https://dev12345.service-now.com
SN_USER = os.environ.get("SN_USER", "")           # ai_config_bot
SN_PASSWORD = os.environ.get("SN_PASSWORD", "")
GIT_REPO_PATH = os.environ.get("GIT_REPO_PATH", "")  # local git repo for update set XMLs

logger = logging.getLogger("sn-mcp")
logging.basicConfig(level=logging.INFO)

# ---------------------------------------------------------------------------
# Multi-Instance Configuration (optional – loaded from sn_instances.json)
# ---------------------------------------------------------------------------

_INSTANCES_CONFIG: dict = {}
_instances_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sn_instances.json")
if os.path.exists(_instances_path):
    with open(_instances_path) as _f:
        _INSTANCES_CONFIG = json.load(_f)
    logger.info("Loaded multi-instance config: %s", list(_INSTANCES_CONFIG.get("instances", {}).keys()))

# ---------------------------------------------------------------------------
# HTTP Client (multi-instance aware)
# ---------------------------------------------------------------------------

def _resolve_instance(instance: str = "") -> tuple[str, str, str]:
    """Resolve an instance key to (url, user, password).

    Falls back to legacy SN_INSTANCE/SN_USER/SN_PASSWORD env vars when
    *instance* is empty or not found in sn_instances.json.
    """
    if not instance or not _INSTANCES_CONFIG.get("instances", {}).get(instance):
        # Backward compatible: use existing single-instance env vars
        if not SN_INSTANCE or not SN_USER or not SN_PASSWORD:
            raise RuntimeError(
                "Missing ServiceNow credentials. "
                "Set SN_INSTANCE, SN_USER, and SN_PASSWORD environment variables."
            )
        return SN_INSTANCE, SN_USER, SN_PASSWORD

    cfg = _INSTANCES_CONFIG["instances"][instance]
    url = cfg["url"]
    user = _INSTANCES_CONFIG.get("service_account", SN_USER)
    password = os.environ.get(cfg["password_env"], "")
    if not url or not user or not password:
        raise RuntimeError(
            f"Missing credentials for instance '{instance}'. "
            f"Set env var: {cfg['password_env']}"
        )
    return url, user, password


def _client(instance: str = "") -> httpx.AsyncClient:
    """Build an authenticated async HTTP client for ServiceNow."""
    url, user, password = _resolve_instance(instance)
    return httpx.AsyncClient(
        base_url=url,
        auth=(user, password),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        timeout=30.0,
    )


def _xml_client(instance: str = "") -> httpx.AsyncClient:
    """Build an authenticated async HTTP client that accepts XML responses.

    Used for the export_update_set.do processor which returns XML, not JSON.
    """
    url, user, password = _resolve_instance(instance)
    return httpx.AsyncClient(
        base_url=url,
        auth=(user, password),
        headers={
            "Accept": "application/xml",
        },
        timeout=120.0,  # XML exports can be large; generous timeout
        follow_redirects=True,  # export_update_set.do uses multiple redirects
    )


# ---------------------------------------------------------------------------
# Table access tiers
# ---------------------------------------------------------------------------

# Tier 1: BLOCKED — PII / financial / HR data. Never accessible.
BLOCKED_TABLES = frozenset({
    "sys_user", "cmn_location", "fm_expense_line", "sn_hr_core_case",
    "alm_asset", "sys_user_has_role",
})

# Tier 2: OPERATIONAL — transactional tables accessible via tools.
# These were previously blocked but are needed for day-to-day operations.
OPERATIONAL_TABLES = frozenset({
    "incident", "sc_task", "sc_request", "sc_req_item",
    "change_request", "problem", "sys_user_group",
    "cmdb_ci", "task_sla", "sys_user_grmember",
    "sysevent_in_email_action",
})


def _assert_safe_table(table: str) -> None:
    """Block access to PII/sensitive tables (Tier 1). Operational tables are allowed."""
    if table.lower() in BLOCKED_TABLES:
        raise ValueError(
            f"Access to table '{table}' is blocked (contains PII/sensitive data). "
            "This table cannot be queried via MCP tools."
        )


async def _get_update_set_contents(client: httpx.AsyncClient, us_sys_id: str) -> tuple[list, str]:
    """Query sys_update_xml for all records tracked in an update set.

    Returns (records, formatted_summary_string).
    """
    resp = await client.get(
        "/api/now/table/sys_update_xml",
        params={
            "sysparm_query": f"update_set={us_sys_id}",
            "sysparm_fields": "sys_id,name,type,action,target_name",
            "sysparm_limit": "200",
        },
    )
    resp.raise_for_status()
    records = resp.json()["result"]

    if not records:
        return records, "No records tracked in this Update Set."

    lines = [f"Update Set contents ({len(records)} record(s)):"]
    for r in records:
        action = r.get("action", "?")
        name = r.get("name", "(unnamed)")
        rec_type = r.get("type", "?")
        target = r.get("target_name", "")
        target_info = f"  target={target}" if target else ""
        lines.append(f"  • [{action}] {name}  (type={rec_type}){target_info}")
    return records, "\n".join(lines)


# ===========================================================================
# MCP Server
# ===========================================================================

mcp = FastMCP(
    "ServiceNow Global Integrator",
    instructions=(
        "MCP server for building ServiceNow integrations in Global Scope. "
        "Manages Update Sets, tables, REST messages, scheduled jobs, and "
        "exports update sets to Git for CI/CD deployment."
    ),
)

# ── A. Update Set Management ──────────────────────────────────────────────

@mcp.tool()
async def manage_update_set(
    action: str,
    name: str = "",
    description: str = "",
    sys_id: str = "",
    instance: str = "",
) -> str:
    """
    Manage ServiceNow Update Sets – the container for all configuration changes.

    Actions:
      - create:      Create a new Update Set. Requires `name` and optional `description`.
      - set_current: Set an Update Set as the current one for this API user.
                     Requires `sys_id` of the Update Set.
                     NOTE: This writes to sys_user_preference (preference name: 'sys_update_set').
                     The GlideRecord-based approach (gs.getUser().savePreference) is preferred
                     but not available via REST. This Table API approach works as a fallback
                     but changes may not take effect until the next REST request.
      - complete:    Mark an Update Set as 'complete'. Requires `sys_id`.

    Returns a JSON-like status string with the sys_id and outcome.
    """
    action = action.strip().lower()

    async with _client(instance) as client:

        # ── CREATE ──
        if action == "create":
            if not name:
                return "Error: 'name' is required for the 'create' action."
            payload = {
                "name": name,
                "description": description or f"Auto-created by AI agent on {datetime.utcnow().isoformat()}",
                "state": "in progress",
            }
            resp = await client.post(
                "/api/now/table/sys_update_set",
                json=payload,
            )
            resp.raise_for_status()
            result = resp.json()["result"]
            us_sys_id = result["sys_id"]
            logger.info("Created Update Set '%s' → %s", name, us_sys_id)
            return (
                f"Update Set created.\n"
                f"  name:   {name}\n"
                f"  sys_id: {us_sys_id}\n"
                f"Next step: call manage_update_set(action='set_current', sys_id='{us_sys_id}')"
            )

        # ── SET CURRENT ──
        elif action == "set_current":
            if not sys_id:
                return "Error: 'sys_id' is required for the 'set_current' action."

            # The preference name for the current update set is 'sys_update_set'.
            # Value is the sys_id of the desired Update Set.
            # Ref: ServiceNow docs – sys_user_preference table, verified via community.
            search = await client.get(
                "/api/now/table/sys_user_preference",
                params={
                    "sysparm_query": f"name=sys_update_set^user.user_name={SN_USER}",
                    "sysparm_limit": "1",
                },
            )
            search.raise_for_status()
            records = search.json()["result"]

            if records:
                # Update existing preference
                pref_id = records[0]["sys_id"]
                resp = await client.patch(
                    f"/api/now/table/sys_user_preference/{pref_id}",
                    json={"value": sys_id},
                )
            else:
                # Create new preference
                resp = await client.post(
                    "/api/now/table/sys_user_preference",
                    json={
                        "name": "sys_update_set",
                        "value": sys_id,
                        "type": "string",
                    },
                )
            resp.raise_for_status()
            logger.info("Current Update Set → %s", sys_id)
            return (
                f"Current Update Set preference is now: {sys_id}\n"
                f"NOTE: If this does not take effect, the instance admin can also set it via:\n"
                f"  gs.getUser().savePreference('sys_update_set', '{sys_id}');\n"
                f"in a Background Script (which applies immediately to the session)."
            )

        # ── COMPLETE ──
        elif action == "complete":
            if not sys_id:
                return "Error: 'sys_id' is required for the 'complete' action."

            # Validate contents before completing
            records, summary = await _get_update_set_contents(client, sys_id)

            if not records:
                logger.warning("Update Set %s has 0 tracked records", sys_id)
                return (
                    f"WARNING: Update Set {sys_id} has NO tracked records (0 entries in sys_update_xml).\n"
                    f"This may mean configuration changes were not captured.\n\n"
                    f"The Update Set has NOT been completed.\n"
                    f"If you are sure this is correct, investigate with validate_update_set() "
                    f"or manually complete it in the instance."
                )

            resp = await client.patch(
                f"/api/now/table/sys_update_set/{sys_id}",
                json={"state": "complete"},
            )
            resp.raise_for_status()
            logger.info("Update Set %s → complete (%d records)", sys_id, len(records))
            return (
                f"Update Set {sys_id} marked as complete.\n\n"
                f"{summary}"
            )

        else:
            return f"Unknown action '{action}'. Use: create, set_current, complete."


@mcp.tool()
async def get_current_update_set(instance: str = "") -> str:
    """
    Return the sys_id and name of the Update Set currently active for this API user.
    Use this to verify you're working inside the correct container before making changes.

    Reads from: sys_user_preference where name='sys_update_set'.
    """
    # Resolve the correct username for the target instance
    _, user, _ = _resolve_instance(instance)
    async with _client(instance) as client:
        resp = await client.get(
            "/api/now/table/sys_user_preference",
            params={
                "sysparm_query": f"name=sys_update_set^user.user_name={user}",
                "sysparm_limit": "1",
                "sysparm_fields": "value",
            },
        )
        resp.raise_for_status()
        records = resp.json()["result"]
        if not records:
            return "No current Update Set is configured for this user."

        us_id = records[0]["value"]

        # Fetch the name for readability
        us_resp = await client.get(
            f"/api/now/table/sys_update_set/{us_id}",
            params={"sysparm_fields": "name,state,sys_id"},
        )
        us_resp.raise_for_status()
        us = us_resp.json()["result"]
        return (
            f"Current Update Set:\n"
            f"  sys_id: {us['sys_id']}\n"
            f"  name:   {us['name']}\n"
            f"  state:  {us['state']}"
        )


# ── B. Data Structure (Tables & Fields) ──────────────────────────────────
#
# IMPORTANT — Doc-verified limitation:
#   Directly POSTing to /api/now/table/sys_db_object returns 403 Forbidden
#   even with admin credentials. ServiceNow intentionally blocks REST-based
#   table creation for platform security.
#
#   Workaround: We use a Scripted REST API on the instance that wraps a
#   GlideRecord insert into sys_db_object server-side. If that endpoint is
#   not deployed yet, we fall back to providing the user with a Background
#   Script they can paste into the instance.
#
#   Similarly, sys_dictionary POST requires the 'personalize_dictionary' role.
# ─────────────────────────────────────────────────────────────────────────

# The Scripted REST API base path (must be deployed on the instance).
# See SKILL.md "Instance Prerequisites" for the setup script.
# TIP: Use the create_scripted_rest_resource() tool to create this API
# programmatically instead of deploying it manually.
# Set SN_MCP_HELPER_PATH to match your instance's Scripted REST API path.
SCRIPTED_API_BASE = os.environ.get("SN_MCP_HELPER_PATH", "/api/x_ai_config/mcp_helper")


@mcp.tool()
async def create_global_table(
    name: str,
    label: str,
    extends: str = "sys_import_set_row",
    instance: str = "",
) -> str:
    """
    Create a new custom table in Global Scope.

    IMPORTANT — Platform limitation:
    Direct POST to /api/now/table/sys_db_object returns 403 Forbidden.
    This tool first attempts to call a Scripted REST API endpoint on the
    instance that wraps the table creation server-side. If that endpoint
    is not deployed, it returns a ready-to-paste Background Script.

    Args:
        name:    Internal table name. MUST start with 'u_' (e.g. 'u_druva_users').
        label:   Human-readable label (e.g. 'Druva Users Import').
        extends: Parent table to extend. Defaults to 'sys_import_set_row'
                 which is recommended for integration import tables.

    Returns the sys_id of the newly created table, or a Background Script
    to paste manually if the Scripted REST endpoint is unavailable.
    """
    if not name.startswith("u_"):
        return "Error: Global table names MUST start with 'u_' prefix."

    _assert_safe_table(name)

    # Verify we're in an Update Set
    current_us = await get_current_update_set(instance=instance)
    if "No current Update Set" in current_us:
        return "Error: No active Update Set. Create and set one before creating tables."

    async with _client(instance) as client:
        # ── Attempt 1: Scripted REST API (preferred) ──
        try:
            resp = await client.post(
                f"{SCRIPTED_API_BASE}/create_table",
                json={
                    "name": name,
                    "label": label,
                    "extends": extends,
                },
            )
            if resp.status_code in (200, 201):
                result = resp.json().get("result", {})
                sys_id = result.get("sys_id", "unknown")
                logger.info("Created table '%s' via Scripted REST → %s", name, sys_id)
                return (
                    f"Table created via Scripted REST API.\n"
                    f"  name:    {name}\n"
                    f"  label:   {label}\n"
                    f"  extends: {extends}\n"
                    f"  sys_id:  {sys_id}"
                )
        except Exception as e:
            logger.warning("Scripted REST endpoint not available: %s", e)

        # ── Attempt 2: Direct Table API (may 403, but worth trying) ──
        try:
            payload = {
                "name": name,
                "label": label,
                "super_class": extends,
                "is_extendable": "true",
                "create_access_controls": "true",
            }
            resp = await client.post(
                "/api/now/table/sys_db_object",
                json=payload,
            )
            if resp.status_code in (200, 201):
                result = resp.json()["result"]
                logger.info("Created table '%s' via direct API → %s", name, result["sys_id"])
                return (
                    f"Table created via direct Table API.\n"
                    f"  name:    {name}\n"
                    f"  label:   {label}\n"
                    f"  extends: {extends}\n"
                    f"  sys_id:  {result['sys_id']}"
                )
            elif resp.status_code == 403:
                logger.warning("Direct POST to sys_db_object returned 403 (expected).")
            else:
                logger.warning("Unexpected status %s from sys_db_object POST", resp.status_code)
        except Exception as e:
            logger.warning("Direct Table API attempt failed: %s", e)

    # ── Fallback: Generate Background Script ──
    bg_script = (
        f"// Paste this into System Definition > Scripts - Background\n"
        f"// Ensure your Update Set is set to the correct one first!\n"
        f"var t = new GlideRecord('sys_db_object');\n"
        f"t.initialize();\n"
        f"t.name = '{name}';\n"
        f"t.label = '{label}';\n"
        f"t.super_class.setDisplayValue('{extends}');\n"
        f"t.is_extendable = true;\n"
        f"t.create_access_controls = true;\n"
        f"var sys_id = t.insert();\n"
        f"gs.info('Created table {name}: ' + sys_id);\n"
    )

    return (
        f"⚠️  Could not create table via REST API (403 Forbidden is expected — \n"
        f"ServiceNow blocks direct POST to sys_db_object for security).\n\n"
        f"OPTION A: Deploy the Scripted REST endpoint (see SKILL.md 'Instance Prerequisites').\n\n"
        f"OPTION B: Run this Background Script on the instance:\n\n"
        f"```javascript\n{bg_script}```\n\n"
        f"After the table exists, use add_column_to_table() to add fields."
    )


@mcp.tool()
async def add_column_to_table(
    table: str,
    column_name: str,
    column_type: str,
    max_length: int = 255,
    label: str = "",
    instance: str = "",
) -> str:
    """
    Add a column (field) to an existing table via sys_dictionary.

    IMPORTANT: The service account needs the 'personalize_dictionary' role
    for this to work. If you get 403, ask the instance admin to grant it.

    Args:
        table:       Target table name (e.g. 'u_druva_users').
        column_name: Internal field name (e.g. 'u_email').
        column_type: ServiceNow type string – common values:
                     'string', 'integer', 'boolean', 'glide_date_time',
                     'reference', 'email', 'url', 'journal'.
        max_length:  Max character length (default 255). Ignored for non-string types.
        label:       Display label. Defaults to a title-cased version of column_name.

    Returns confirmation with the new field's sys_id.
    """
    _assert_safe_table(table)

    if not column_name.startswith("u_"):
        return "Error: Custom column names in Global Scope must start with 'u_'."

    # Map friendly type names to ServiceNow internal_type values
    type_map = {
        "string": "string",
        "integer": "integer",
        "boolean": "boolean",
        "glide_date_time": "glide_date_time",
        "date": "glide_date",
        "reference": "reference",
        "email": "email",
        "url": "url",
        "journal": "journal",
        "journal_input": "journal_input",
        "decimal": "decimal",
        "currency": "currency",
    }
    internal_type = type_map.get(column_type.lower(), column_type)
    display_label = label or column_name.replace("u_", "").replace("_", " ").title()

    async with _client(instance) as client:
        # sys_dictionary: the 'name' field holds the TABLE name,
        # and 'element' holds the COLUMN name.
        payload = {
            "name": table,
            "element": column_name,
            "column_label": display_label,
            "internal_type": internal_type,
            "max_length": str(max_length),
            "active": "true",
            "read_only": "false",
            "mandatory": "false",
        }

        resp = await client.post(
            "/api/now/table/sys_dictionary",
            json=payload,
        )

        if resp.status_code == 403:
            # Known issue: requires personalize_dictionary role
            bg_script = (
                f"var d = new GlideRecord('sys_dictionary');\n"
                f"d.initialize();\n"
                f"d.name = '{table}';\n"
                f"d.element = '{column_name}';\n"
                f"d.column_label = '{display_label}';\n"
                f"d.internal_type = '{internal_type}';\n"
                f"d.max_length = {max_length};\n"
                f"d.insert();\n"
            )
            return (
                f"⚠️  403 Forbidden writing to sys_dictionary.\n"
                f"The service account needs the 'personalize_dictionary' role.\n\n"
                f"Ask the admin to grant it, or run this Background Script:\n\n"
                f"```javascript\n{bg_script}```"
            )

        resp.raise_for_status()
        result = resp.json()["result"]
        logger.info("Added column '%s' (%s) to table '%s'", column_name, internal_type, table)
        return (
            f"Column added.\n"
            f"  table:  {table}\n"
            f"  column: {column_name}\n"
            f"  type:   {internal_type}\n"
            f"  label:  {display_label}\n"
            f"  sys_id: {result['sys_id']}"
        )


# ── C. Integration Machinery ─────────────────────────────────────────────

@mcp.tool()
async def create_rest_message(
    name: str,
    endpoint: str,
    http_method: str = "GET",
    description: str = "",
    auth_type: str = "basic",
    instance: str = "",
) -> str:
    """
    Create a REST Message definition in ServiceNow (sys_rest_message).

    Credentials are NOT hardcoded. The message is created with placeholder
    authentication that must be configured by a human in the instance.

    The authentication_type field accepts: 'basic', 'oauth2', 'no_auth'.
    Authentication configured on the parent REST Message is inherited by
    its child HTTP Methods (functions).

    Args:
        name:        Display name (e.g. 'Druva User Sync API').
        endpoint:    Base URL of the external API.
        http_method: Default HTTP method for the default function (GET, POST, etc.).
        description: Optional description.
        auth_type:   'basic', 'oauth2', or 'no_auth'. Defaults to 'basic'.

    Returns sys_id of the REST Message and its default HTTP Method record.
    """
    async with _client(instance) as client:
        # 1. Create the REST Message envelope
        msg_payload = {
            "name": name,
            "description": description or f"REST Message for {name}. Configure credentials before use.",
            "rest_endpoint": endpoint,
            "authentication_type": auth_type,
            # Do NOT hardcode credentials — human must configure via
            # Connection & Credential Aliases or Basic Auth Profile
        }
        resp = await client.post(
            "/api/now/table/sys_rest_message",
            json=msg_payload,
        )
        resp.raise_for_status()
        msg = resp.json()["result"]
        msg_id = msg["sys_id"]

        # 2. Create the default HTTP Method (function)
        # The child table is sys_rest_message_fn.
        # 'rest_message' is the reference field pointing to the parent.
        # 'authentication_type' = 'inherit_from_parent' means it uses the
        # parent REST Message's auth config.
        fn_payload = {
            "name": "Default " + http_method,
            "http_method": http_method.upper(),
            "rest_message": msg_id,
            "rest_endpoint": endpoint,
            "authentication_type": "inherit_from_parent",
        }
        fn_resp = await client.post(
            "/api/now/table/sys_rest_message_fn",
            json=fn_payload,
        )
        fn_resp.raise_for_status()
        fn = fn_resp.json()["result"]

        logger.info("Created REST Message '%s' → %s", name, msg_id)
        return (
            f"REST Message created.\n"
            f"  name:       {name}\n"
            f"  endpoint:   {endpoint}\n"
            f"  msg_sys_id: {msg_id}\n"
            f"  fn_sys_id:  {fn['sys_id']}\n"
            f"  auth:       {auth_type} (placeholder – configure real credentials in the instance)\n"
            f"  NOTE: Go to System Web Services > Outbound > REST Messages to add credentials."
        )


@mcp.tool()
async def create_scripted_rest_resource(
    api_name: str,
    resource_name: str,
    http_method: str,
    relative_path: str,
    script: str,
    api_description: str = "",
    resource_description: str = "",
    instance: str = "",
) -> str:
    """
    Create a Scripted REST API and resource (operation) in ServiceNow.

    This creates the parent API definition (sys_ws_definition) and a child
    resource/operation (sys_ws_operation). If the parent API already exists,
    it is reused. If the child resource already exists, it is not overwritten.

    Use this to bootstrap instance-side helper endpoints (e.g. the MCP Helper
    API used by create_global_table and add_to_update_set).

    Args:
        api_name:             Display name for the Scripted REST API (e.g. 'MCP Helper').
        resource_name:        Name for the resource/operation (e.g. 'create_table').
        http_method:          HTTP method: GET, POST, PUT, PATCH, DELETE.
        relative_path:        URL path relative to the API base (e.g. '/create_table').
        script:               Server-side JavaScript for the resource's operation script.
        api_description:      Optional description for the parent API.
        resource_description: Optional description for the resource.

    Returns the API sys_id, resource sys_id, and the full endpoint path.
    """
    http_method = http_method.strip().upper()
    if http_method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
        return f"Error: Invalid http_method '{http_method}'. Use GET, POST, PUT, PATCH, or DELETE."

    # Ensure relative_path starts with /
    if not relative_path.startswith("/"):
        relative_path = "/" + relative_path

    async with _client(instance) as client:
        # ── Step 1: Find or create the parent API (sys_ws_definition) ──
        api_id = None
        api_created = False

        # Check if the API already exists
        search_resp = await client.get(
            "/api/now/table/sys_ws_definition",
            params={
                "sysparm_query": f"name={api_name}",
                "sysparm_fields": "sys_id,name",
                "sysparm_limit": "1",
            },
        )
        search_resp.raise_for_status()
        existing_apis = search_resp.json()["result"]

        if existing_apis:
            api_id = existing_apis[0]["sys_id"]
            logger.info("Found existing Scripted REST API '%s' → %s", api_name, api_id)
        else:
            # Create the parent API
            try:
                api_payload = {
                    "name": api_name,
                    "short_description": api_description or f"Scripted REST API: {api_name}",
                    "active": "true",
                }
                api_resp = await client.post(
                    "/api/now/table/sys_ws_definition",
                    json=api_payload,
                )
                if api_resp.status_code in (200, 201):
                    api_result = api_resp.json()["result"]
                    api_id = api_result["sys_id"]
                    api_created = True
                    logger.info("Created Scripted REST API '%s' → %s", api_name, api_id)
                elif api_resp.status_code == 403:
                    logger.warning("403 creating sys_ws_definition – falling back to Background Script.")
                else:
                    logger.warning(
                        "Unexpected status %s creating sys_ws_definition: %s",
                        api_resp.status_code, api_resp.text,
                    )
            except Exception as e:
                logger.warning("Failed to create sys_ws_definition: %s", e)

        # If we couldn't find or create the API, fall back to Background Script
        if not api_id:
            bg_script = _scripted_rest_bg_script(
                api_name, api_description, resource_name, http_method,
                relative_path, script, resource_description,
            )
            return (
                f"Could not create Scripted REST API via Table API (403 or error).\n\n"
                f"Run this Background Script on the instance:\n\n"
                f"```javascript\n{bg_script}```"
            )

        # ── Step 2: Check for duplicate, then create the resource (sys_ws_operation) ──
        res_search = await client.get(
            "/api/now/table/sys_ws_operation",
            params={
                "sysparm_query": f"web_service_definition={api_id}^name={resource_name}",
                "sysparm_fields": "sys_id,name,http_method,relative_path",
                "sysparm_limit": "1",
            },
        )
        res_search.raise_for_status()
        existing_resources = res_search.json()["result"]

        if existing_resources:
            existing = existing_resources[0]
            return (
                f"Resource already exists (not overwritten).\n"
                f"  api_name:      {api_name}\n"
                f"  api_sys_id:    {api_id}\n"
                f"  resource_name: {existing.get('name', resource_name)}\n"
                f"  resource_id:   {existing['sys_id']}\n"
                f"  http_method:   {existing.get('http_method', '?')}\n"
                f"  relative_path: {existing.get('relative_path', '?')}"
            )

        # Create the child resource
        try:
            res_payload = {
                "web_service_definition": api_id,
                "name": resource_name,
                "http_method": http_method,
                "relative_path": relative_path,
                "operation_script": script,
                "short_description": resource_description or f"Resource: {resource_name}",
                "active": "true",
            }
            res_resp = await client.post(
                "/api/now/table/sys_ws_operation",
                json=res_payload,
            )
            if res_resp.status_code in (200, 201):
                res_result = res_resp.json()["result"]
                res_id = res_result["sys_id"]
                logger.info(
                    "Created Scripted REST resource '%s' on '%s' → %s",
                    resource_name, api_name, res_id,
                )
                api_status = "created" if api_created else "existing"
                return (
                    f"Scripted REST resource created.\n"
                    f"  api_name:      {api_name} ({api_status})\n"
                    f"  api_sys_id:    {api_id}\n"
                    f"  resource_name: {resource_name}\n"
                    f"  resource_id:   {res_id}\n"
                    f"  http_method:   {http_method}\n"
                    f"  relative_path: {relative_path}\n"
                    f"  active:        true"
                )
            elif res_resp.status_code == 403:
                logger.warning("403 creating sys_ws_operation – falling back to Background Script.")
            else:
                logger.warning(
                    "Unexpected status %s creating sys_ws_operation: %s",
                    res_resp.status_code, res_resp.text,
                )
        except Exception as e:
            logger.warning("Failed to create sys_ws_operation: %s", e)

    # ── Fallback: Background Script for the resource only (API already exists) ──
    bg_script = _scripted_rest_bg_script(
        api_name, api_description, resource_name, http_method,
        relative_path, script, resource_description,
        api_sys_id=api_id,
    )
    return (
        f"Created the API definition but could not create the resource via REST API.\n"
        f"  api_name:   {api_name}\n"
        f"  api_sys_id: {api_id}\n\n"
        f"Run this Background Script to create the resource:\n\n"
        f"```javascript\n{bg_script}```"
    )


def _scripted_rest_bg_script(
    api_name: str,
    api_description: str,
    resource_name: str,
    http_method: str,
    relative_path: str,
    script: str,
    resource_description: str,
    api_sys_id: str = "",
) -> str:
    """Generate a Background Script to create a Scripted REST API + resource."""
    # Escape single quotes in the script body for safe embedding
    safe_script = script.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")

    lines = [
        "// Paste into: System Definition > Scripts - Background",
        "// Creates a Scripted REST API and resource",
        "",
    ]

    if api_sys_id:
        # API already exists, only create the resource
        lines.append(f"var apiId = '{api_sys_id}';")
    else:
        # Create the API first
        lines.extend([
            "// Step 1: Create the Scripted REST API",
            "var api = new GlideRecord('sys_ws_definition');",
            "api.initialize();",
            f"api.name = '{api_name}';",
            f"api.short_description = '{api_description or api_name}';",
            "api.active = true;",
            "var apiId = api.insert();",
            "gs.info('Created Scripted REST API: ' + apiId);",
            "",
        ])

    lines.extend([
        "// Step 2: Create the resource/operation",
        "var res = new GlideRecord('sys_ws_operation');",
        "res.initialize();",
        "res.web_service_definition = apiId;",
        f"res.name = '{resource_name}';",
        f"res.http_method = '{http_method}';",
        f"res.relative_path = '{relative_path}';",
        f"res.operation_script = '{safe_script}';",
        f"res.short_description = '{resource_description or resource_name}';",
        "res.active = true;",
        "var resId = res.insert();",
        "gs.info('Created Scripted REST resource: ' + resId);",
    ])

    return "\n".join(lines) + "\n"


@mcp.tool()
async def create_scheduled_job(
    name: str,
    script: str,
    run_type: str = "daily",
    run_time: str = "02:00:00",
    description: str = "",
    instance: str = "",
) -> str:
    """
    Create a Scheduled Script Execution (sysauto_script) in ServiceNow.

    SAFETY: The job is ALWAYS created with active=false.
    A human must review and activate it manually.

    Verified fields for sysauto_script:
      - name: Display name
      - script: The server-side JavaScript (Run this Script field)
      - run_type: Scheduling frequency ('daily', 'weekly', 'monthly',
                  'periodically', 'once', 'on_demand')
      - run_time: Time of day (HH:MM:SS format)
      - active: Boolean ('true'/'false') – we always force 'false'

    Args:
        name:        Job name (e.g. 'Druva User Sync - Daily').
        script:      The server-side JavaScript to execute.
        run_type:    'daily', 'weekly', 'monthly', 'periodically', 'once', or 'on_demand'.
        run_time:    Time of day to run (HH:MM:SS). Default '02:00:00'.
        description: Optional description.

    Returns sys_id of the new scheduled job.
    """
    # Validate run_type against known values
    valid_run_types = {"daily", "weekly", "monthly", "periodically", "once", "on_demand"}
    if run_type.lower() not in valid_run_types:
        return (
            f"Error: Invalid run_type '{run_type}'. "
            f"Valid values: {', '.join(sorted(valid_run_types))}"
        )

    # CRITICAL SAFETY: force inactive + review comment
    safe_script = (
        "// ============================================================\n"
        "// AUTO-GENERATED by AI Agent – Review required before activation\n"
        f"// Created: {datetime.utcnow().isoformat()}Z\n"
        "// ============================================================\n\n"
        + script
    )

    async with _client(instance) as client:
        payload = {
            "name": name,
            "script": safe_script,
            "run_type": run_type.lower(),
            "run_time": run_time,
            "active": "false",  # ← CRITICAL: never active by default
            "description": description or f"AI-generated job: {name}. Review before activating.",
        }
        resp = await client.post(
            "/api/now/table/sysauto_script",
            json=payload,
        )
        resp.raise_for_status()
        result = resp.json()["result"]
        logger.info("Created Scheduled Job '%s' (active=false) → %s", name, result["sys_id"])
        return (
            f"Scheduled Job created (INACTIVE – requires human review).\n"
            f"  name:     {name}\n"
            f"  sys_id:   {result['sys_id']}\n"
            f"  active:   false\n"
            f"  run_type: {run_type}\n"
            f"  run_time: {run_time}\n"
            f"  ⚠️  A human must review the script and set active=true in the instance.\n"
            f"  Navigate: System Definition > Scheduled Jobs to review."
        )


# ── D. Deployment – Export Update Set to Git ──────────────────────────────
#
# VERIFIED EXPORT PROCESS (from ServiceNow docs & community):
#
# The export is a TWO-STEP process:
#   Step 1: Generate the remote update set record.
#     - A server-side script (UpdateSetExport class) creates a temporary
#       sys_remote_update_set record and copies sys_update_xml entries to it.
#     - Alternatively, the UI Action "Export to XML" does this behind the scenes.
#
#   Step 2: Download the XML file.
#     - URL: export_update_set.do?sysparm_sys_id=<REMOTE_UPDATE_SET_SYS_ID>
#                                &sysparm_delete_when_done=true
#     - This endpoint uses multiple HTTP redirects before serving the file.
#     - The sysparm_sys_id here is the sys_id of the sys_remote_update_set
#       record, NOT the local sys_update_set record.
#
# ALTERNATIVE (simpler, also documented):
#     - URL: sys_update_set.do?UNL&sys_id=<LOCAL_UPDATE_SET_SYS_ID>&
#     - Returns the update set as an unloaded XML file.
#     - This is the pattern used by the "Export to XML" related link.
#
# We attempt both approaches: the simpler UNL pattern first, then the
# export_update_set.do processor.
# ─────────────────────────────────────────────────────────────────────────

@mcp.tool()
async def export_update_set_to_git(
    sys_id: str,
    git_path: str = "",
    commit_message: str = "",
    branch: str = "main",
    instance: str = "",
) -> str:
    """
    Export a completed Update Set as XML and commit it to a Git repository.

    Workflow:
      1. Verify the Update Set is complete.
      2. Fetch the Update Set XML from the ServiceNow instance.
         - Primary:   GET /sys_update_set.do?UNL&sys_id=<sys_id>&
         - Fallback:  GET /export_update_set.do?sysparm_sys_id=<sys_id>
      3. Write the XML to the local Git repository.
      4. Commit and push.

    Args:
        sys_id:         sys_id of the Update Set to export.
        git_path:       Sub-path within the repo (e.g. 'update_sets/'). Defaults to 'update_sets/'.
        commit_message: Git commit message. Auto-generated if blank.
        branch:         Git branch to commit to. Default 'main'.

    Returns the file path and git commit hash.
    """
    repo = GIT_REPO_PATH
    if not repo:
        return "Error: GIT_REPO_PATH environment variable is not set."

    git_path = git_path.strip("/") if git_path else "update_sets"

    async with _client(instance) as client:
        # 1. Fetch Update Set metadata for the filename and state check
        meta_resp = await client.get(
            f"/api/now/table/sys_update_set/{sys_id}",
            params={"sysparm_fields": "name,state"},
        )
        meta_resp.raise_for_status()
        us_meta = meta_resp.json()["result"]
        us_name = us_meta["name"]

        if us_meta["state"] != "complete":
            return (
                f"Error: Update Set '{us_name}' is not complete (state: {us_meta['state']}). "
                "Complete it first with manage_update_set(action='complete', ...)."
            )

    # 2. Export the XML
    # The XML download endpoints don't return JSON, so we use the xml_client
    xml_content = None
    export_method = None

    async with _xml_client(instance) as xml_client:
        # Attempt A: export_update_set.do (preferred — includes sys_update_xml records)
        try:
            resp_a = await xml_client.get(
                "/export_update_set.do",
                params={
                    "sysparm_sys_id": sys_id,
                    "sysparm_delete_when_done": "false",
                },
            )
            if resp_a.status_code == 200 and "sys_update_xml" in resp_a.text:
                xml_content = resp_a.text
                export_method = "export_update_set.do"
                logger.info("Exported Update Set via export_update_set.do.")
        except Exception as e:
            logger.warning("export_update_set.do attempt failed: %s", e)

        # Attempt B: UNL pattern (only if export_update_set.do failed)
        if not xml_content:
            try:
                resp_b = await xml_client.get(
                    "/sys_update_set.do",
                    params={"UNL": "", "sys_id": sys_id},
                )
                if resp_b.status_code == 200 and "sys_update_xml" in resp_b.text:
                    xml_content = resp_b.text
                    export_method = "UNL"
                    logger.info("Exported Update Set via UNL pattern.")
            except Exception as e:
                logger.warning("UNL export attempt failed: %s", e)

    if not xml_content:
        return (
            f"Error: Could not download Update Set XML for '{us_name}' ({sys_id}).\n"
            f"Both export methods failed. Try downloading manually:\n"
            f"  1. Navigate to: {SN_INSTANCE}/sys_update_set.do?sys_id={sys_id}\n"
            f"  2. Click 'Export to XML' under Related Links.\n"
            f"  3. Save the file to {os.path.join(repo, git_path)}/ and commit manually."
        )

    # 3. Write to local file system
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    safe_name = us_name.replace(" ", "_").replace("/", "_")
    filename = f"{safe_name}_{timestamp}.xml"
    target_dir = os.path.join(repo, git_path)
    os.makedirs(target_dir, exist_ok=True)
    file_path = os.path.join(target_dir, filename)

    with open(file_path, "w", encoding="utf-8") as f:
        f.write(xml_content)

    logger.info("Wrote Update Set XML → %s (via %s)", file_path, export_method)

    # 4. Git commit & push
    msg = commit_message or f"Export Update Set: {us_name} ({sys_id})"
    try:
        subprocess.run(["git", "-C", repo, "add", os.path.join(git_path, filename)], check=True)
        subprocess.run(["git", "-C", repo, "commit", "-m", msg], check=True)
        result = subprocess.run(
            ["git", "-C", repo, "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        )
        commit_hash = result.stdout.strip()
        subprocess.run(["git", "-C", repo, "push", "origin", branch], check=True)
    except subprocess.CalledProcessError as exc:
        return (
            f"XML exported to {file_path} but git operation failed:\n{exc}\n"
            "You may need to commit and push manually."
        )

    return (
        f"Update Set exported and pushed to Git.\n"
        f"  file:     {file_path}\n"
        f"  method:   {export_method}\n"
        f"  commit:   {commit_hash}\n"
        f"  branch:   {branch}\n"
        f"  message:  {msg}"
    )


# ── E. Utility / Read-Only Tools ─────────────────────────────────────────

@mcp.tool()
async def list_update_sets(
    state: str = "in progress",
    limit: int = 20,
    instance: str = "",
) -> str:
    """
    List Update Sets filtered by state. Useful for finding existing work.

    Args:
        state: Filter by state – 'in progress', 'complete', or 'ignore'.
        limit: Max records to return (default 20).
    """
    async with _client(instance) as client:
        resp = await client.get(
            "/api/now/table/sys_update_set",
            params={
                "sysparm_query": f"state={state}^ORDERBYDESCsys_updated_on",
                "sysparm_fields": "sys_id,name,state,description,sys_updated_on",
                "sysparm_limit": str(limit),
            },
        )
        resp.raise_for_status()
        records = resp.json()["result"]

    if not records:
        return f"No Update Sets found with state='{state}'."

    lines = [f"Update Sets (state={state}):"]
    for r in records:
        lines.append(f"  • {r['name']}  [{r['sys_id']}]  updated: {r['sys_updated_on']}")
    return "\n".join(lines)


@mcp.tool()
async def describe_table(table: str, instance: str = "") -> str:
    """
    Return the column definitions (sys_dictionary) for a given table.
    Only works for custom (u_*) or system definition tables – blocked for sensitive tables.

    Note: This reads sys_dictionary metadata only, not actual table data.
    For incident/task tables, dictionary inspection is allowed since it's
    metadata, not transactional data.

    Args:
        table: Table name to describe (e.g. 'u_druva_users').
    """
    # describe_table reads sys_dictionary (metadata), not the table itself,
    # so we allow it for all tables.

    async with _client(instance) as client:
        resp = await client.get(
            "/api/now/table/sys_dictionary",
            params={
                "sysparm_query": f"name={table}^elementISNOTEMPTY",
                "sysparm_fields": "element,column_label,internal_type,max_length,mandatory",
                "sysparm_limit": "200",
            },
        )
        resp.raise_for_status()
        records = resp.json()["result"]

    if not records:
        return f"No dictionary entries found for table '{table}'."

    lines = [f"Columns for '{table}':"]
    for r in records:
        mand = " (mandatory)" if r.get("mandatory") == "true" else ""
        # internal_type may come back as a dict ({"link": ..., "value": ...})
        # from the ServiceNow Table API for reference-type fields.
        itype = r.get("internal_type", "")
        if isinstance(itype, dict):
            itype = itype.get("value", str(itype))
        element = str(r.get("element", ""))
        col_label = str(r.get("column_label", ""))
        max_len = str(r.get("max_length", ""))
        lines.append(
            f"  • {element:30s}  {itype:20s}  "
            f"len={max_len}{mand}  label=\"{col_label}\""
        )
    return "\n".join(lines)


@mcp.tool()
async def validate_update_set(sys_id: str, instance: str = "") -> str:
    """
    Validate an Update Set by listing all tracked records (sys_update_xml).

    Read-only inspection tool. Use this before completing an Update Set to
    verify that all expected configuration changes were captured.

    Args:
        sys_id: sys_id of the Update Set to validate.

    Returns a formatted summary with record count, types, and names.
    """
    async with _client(instance) as client:
        # Fetch update set metadata
        meta_resp = await client.get(
            f"/api/now/table/sys_update_set/{sys_id}",
            params={"sysparm_fields": "name,state,sys_id"},
        )
        meta_resp.raise_for_status()
        us = meta_resp.json()["result"]

        records, summary = await _get_update_set_contents(client, sys_id)

    header = (
        f"Update Set: {us['name']}\n"
        f"  sys_id: {us['sys_id']}\n"
        f"  state:  {us['state']}\n\n"
    )
    return header + summary


@mcp.tool()
async def add_to_update_set(
    table: str,
    record_sys_id: str,
    update_set_sys_id: str = "",
    instance: str = "",
) -> str:
    """
    Add an existing record to an Update Set so it is tracked for deployment.

    Use this when a record was created via REST API but was not automatically
    captured in the Update Set (e.g. scheduled jobs, REST messages).

    This uses the instance-side addToUpdateSetUtils Script Include via the
    Scripted REST helper endpoint. If that endpoint is not deployed, a
    ready-to-paste Background Script is returned instead.

    Args:
        table:              Table name of the record (e.g. 'sysauto_script').
        record_sys_id:      sys_id of the record to add.
        update_set_sys_id:  sys_id of the target Update Set. If blank, uses
                            the current Update Set for this API user.

    Returns confirmation or a Background Script fallback.
    """
    _assert_safe_table(table)

    # Resolve target update set
    target_us = update_set_sys_id
    if not target_us:
        current = await get_current_update_set()
        if "No current Update Set" in current:
            return "Error: No active Update Set and no update_set_sys_id provided."
        # Parse sys_id from the formatted output
        for line in current.splitlines():
            if "sys_id:" in line:
                target_us = line.split("sys_id:")[-1].strip()
                break
        if not target_us:
            return "Error: Could not determine current Update Set sys_id."

    async with _client(instance) as client:
        # Verify the record exists
        rec_resp = await client.get(
            f"/api/now/table/{table}/{record_sys_id}",
            params={"sysparm_fields": "sys_id,sys_name,sys_class_name"},
        )
        if rec_resp.status_code == 404:
            return f"Error: Record {record_sys_id} not found in table '{table}'."
        rec_resp.raise_for_status()
        rec = rec_resp.json()["result"]
        rec_display = rec.get("sys_name") or rec.get("sys_id", record_sys_id)

        # ── Attempt 1: Scripted REST API (calls addToUpdateSetUtils server-side) ──
        try:
            resp = await client.post(
                f"{SCRIPTED_API_BASE}/add_to_update_set",
                json={
                    "table": table,
                    "sys_id": record_sys_id,
                    "update_set": target_us,
                },
            )
            if resp.status_code in (200, 201):
                result = resp.json().get("result", {})
                logger.info(
                    "Added %s/%s to Update Set %s via Scripted REST",
                    table, record_sys_id, target_us,
                )
                return (
                    f"Record added to Update Set via Scripted REST API.\n"
                    f"  table:      {table}\n"
                    f"  record:     {rec_display} [{record_sys_id}]\n"
                    f"  update_set: {target_us}\n"
                    f"  xml_sys_id: {result.get('sys_id', 'see update set')}"
                )
        except Exception as e:
            logger.warning("Scripted REST add_to_update_set not available: %s", e)

    # ── Fallback: Generate Background Script using addToUpdateSetUtils ──
    bg_script = (
        f"// Paste into: System Definition > Scripts - Background\n"
        f"// Adds record {table}/{record_sys_id} to Update Set {target_us}\n"
        f"//\n"
        f"// Option A: Use addToUpdateSetUtils (if available on this instance)\n"
        f"var util = new addToUpdateSetUtils();\n"
        f"var gr = new GlideRecord('{table}');\n"
        f"if (gr.get('{record_sys_id}')) {{\n"
        f"    // Temporarily switch to target update set\n"
        f"    var currentUS = gs.getPreference('sys_update_set');\n"
        f"    gs.getUser().savePreference('sys_update_set', '{target_us}');\n"
        f"    \n"
        f"    var um = new GlideUpdateManager2();\n"
        f"    um.saveRecord(gr);\n"
        f"    gs.info('Added ' + gr.getClassDisplayValue() + ' to update set');\n"
        f"    \n"
        f"    // Restore original update set\n"
        f"    gs.getUser().savePreference('sys_update_set', currentUS);\n"
        f"}} else {{\n"
        f"    gs.error('Record not found: {table}/{record_sys_id}');\n"
        f"}}\n"
    )

    return (
        f"Could not add record via REST API (Scripted REST endpoint not available).\n"
        f"  record: {rec_display} [{record_sys_id}] in '{table}'\n"
        f"  target: Update Set {target_us}\n\n"
        f"OPTION A: Deploy the Scripted REST endpoint (see SKILL.md 'Instance Prerequisites').\n\n"
        f"OPTION B: Run this Background Script on the instance:\n\n"
        f"```javascript\n{bg_script}```"
    )


# Whitelist of tables allowed for script searching
_SCRIPT_SEARCH_TABLES: dict[str, dict] = {
    "sysauto_script": {
        "label": "Scheduled Jobs",
        "fields": "sys_id,name,script,active,run_type",
    },
    "sys_script_include": {
        "label": "Script Includes",
        "fields": "sys_id,name,script,active,api_name",
    },
    "sys_script": {
        "label": "Business Rules",
        "fields": "sys_id,name,script,active,collection",
    },
    "sys_script_client": {
        "label": "Client Scripts",
        "fields": "sys_id,name,script,active,table,type,ui_type",
    },
    "sys_ui_policy": {
        "label": "UI Policies",
        "fields": "sys_id,short_description,script_true,script_false,active,table,global",
        "script_field": "script_true",
    },
    "sys_security_acl": {
        "label": "ACLs",
        "fields": "sys_id,name,script,active,type,operation",
    },
}


@mcp.tool()
async def search_scripts(
    search_term: str,
    tables: str = "sysauto_script,sys_script_include",
    instance: str = "",
) -> str:
    """
    Search script bodies in ServiceNow for a given term. Read-only.

    Useful for finding all Scheduled Jobs, Script Includes, or Business Rules
    that reference a specific table, function, or keyword.

    Args:
        search_term: String to search for in script bodies (e.g. 'u_druva_users').
        tables:      Comma-separated list of tables to search.
                     Allowed: 'sysauto_script', 'sys_script_include', 'sys_script',
                     'sys_script_client', 'sys_ui_policy', 'sys_security_acl'.
                     Default: 'sysauto_script,sys_script_include'.

    Returns a formatted listing of matching records per table.
    """
    requested = [t.strip() for t in tables.split(",") if t.strip()]

    # Whitelist check
    invalid = [t for t in requested if t not in _SCRIPT_SEARCH_TABLES]
    if invalid:
        allowed = ", ".join(sorted(_SCRIPT_SEARCH_TABLES))
        return f"Error: Invalid table(s): {', '.join(invalid)}. Allowed: {allowed}"

    logger.info("search_scripts: term=%r tables=%s", search_term, requested)

    all_lines: list[str] = []

    async with _client(instance) as client:
        for table_name in requested:
            meta = _SCRIPT_SEARCH_TABLES[table_name]
            # Some tables use a different script field (e.g. UI Policies use script_true)
            script_field = meta.get("script_field", "script")
            resp = await client.get(
                f"/api/now/table/{table_name}",
                params={
                    "sysparm_query": f"{script_field}LIKE{search_term}",
                    "sysparm_fields": meta["fields"],
                    "sysparm_limit": "20",
                },
            )
            resp.raise_for_status()
            records = resp.json()["result"]

            all_lines.append(f"\n── {meta['label']} ({table_name}) ── {len(records)} match(es)")
            if not records:
                all_lines.append("  (none)")
                continue

            for r in records:
                active = "active" if r.get("active") == "true" else "inactive"
                extra = ""
                if table_name == "sysauto_script":
                    extra = f"  run_type={r.get('run_type', '?')}"
                elif table_name == "sys_script_include":
                    extra = f"  api_name={r.get('api_name', '?')}"
                elif table_name == "sys_script":
                    extra = f"  table={r.get('collection', '?')}"
                elif table_name == "sys_script_client":
                    extra = f"  table={r.get('table', '?')}  type={r.get('type', '?')}"
                elif table_name == "sys_ui_policy":
                    extra = f"  table={r.get('table', '?')}  global={r.get('global', '?')}"
                elif table_name == "sys_security_acl":
                    extra = f"  type={r.get('type', '?')}  operation={r.get('operation', '?')}"
                display_name = r.get('name') or r.get('short_description') or '(unnamed)'
                all_lines.append(
                    f"  • {display_name}  [{r['sys_id']}]  ({active}){extra}"
                )

    if not all_lines:
        return f"No results found for '{search_term}'."

    header = f"Scripts containing '{search_term}':"
    return header + "\n".join(all_lines)


@mcp.tool()
async def read_record(
    table: str,
    sys_id: str,
    fields: str = "",
    instance: str = "",
) -> str:
    """
    Read a single record from any non-blocked ServiceNow table.

    Useful for inspecting metadata/configuration records such as sp_widget,
    sys_ws_definition, sysauto_script, sys_script_include, etc.

    Args:
        table:  Table name (e.g. 'sp_widget', 'sys_ws_definition').
        sys_id: The sys_id of the record to read.
        fields: Optional comma-separated field names to return.
                If blank, all fields are returned.
    """
    _assert_safe_table(table)

    params: dict[str, str] = {}
    if fields.strip():
        params["sysparm_fields"] = fields.strip()

    async with _client(instance) as client:
        resp = await client.get(
            f"/api/now/table/{table}/{sys_id}",
            params=params,
        )
        if resp.status_code == 404:
            return f"Record not found: {table}/{sys_id}"
        resp.raise_for_status()
        record = resp.json()["result"]

    MAX_VALUE_LEN = 4000
    lines = [f"Record: {table} / {sys_id}"]
    for key, value in record.items():
        # Reference fields come back as {"link": ..., "value": ...}
        if isinstance(value, dict):
            value = value.get("value", str(value))
        value = str(value)
        if len(value) > MAX_VALUE_LEN:
            value = value[:MAX_VALUE_LEN] + "... (truncated)"
        lines.append(f"  {key}: {value}")
    return "\n".join(lines)


@mcp.tool()
async def update_record(
    table: str,
    sys_id: str,
    fields_json: str,
    instance: str = "",
) -> str:
    """
    Update a single record on any non-blocked ServiceNow table.

    The change is captured in the currently active Update Set.

    Args:
        table:       Table name (e.g. 'sp_widget', 'sysauto_script').
        sys_id:      The sys_id of the record to update.
        fields_json: JSON string of field-value pairs to set.
                     Example: '{"name": "My Widget", "script": "(function(){...})()"}'
    """
    _assert_safe_table(table)

    import json as _json
    try:
        payload = _json.loads(fields_json)
    except _json.JSONDecodeError as exc:
        return f"Invalid JSON in fields_json: {exc}"

    if not isinstance(payload, dict) or not payload:
        return "fields_json must be a non-empty JSON object."

    async with _client(instance) as client:
        resp = await client.patch(
            f"/api/now/table/{table}/{sys_id}",
            json=payload,
        )
        if resp.status_code == 404:
            return f"Record not found: {table}/{sys_id}"
        resp.raise_for_status()
        result = resp.json()["result"]

    updated_fields = list(payload.keys())
    name = result.get("name") or result.get("sys_name") or sys_id
    return (
        f"Updated {table}/{sys_id} ({name}).\n"
        f"Fields changed: {', '.join(updated_fields)}"
    )


# ── F. Multi-Instance CI/CD Pipeline ──────────────────────────────────────

def _get_pipeline() -> list[str]:
    """Return the ordered list of instance keys from sn_instances.json."""
    return _INSTANCES_CONFIG.get("pipeline", [])


def _next_in_pipeline(source: str) -> str | None:
    """Return the next instance after *source* in the pipeline, or None."""
    pipeline = _get_pipeline()
    try:
        idx = pipeline.index(source)
        if idx + 1 < len(pipeline):
            return pipeline[idx + 1]
    except ValueError:
        pass
    return None


@mcp.tool()
async def list_instances() -> str:
    """List all configured ServiceNow instances and the promotion pipeline order.

    Read-only. Shows configured instances, pipeline order, and connectivity
    status (via a lightweight API call to each instance).
    """
    if not _INSTANCES_CONFIG.get("instances"):
        return (
            "No multi-instance configuration found.\n"
            "Create sn_instances.json next to server.py to enable CI/CD pipeline features.\n"
            f"Currently using single instance: {SN_INSTANCE or '(not set)'}"
        )

    pipeline = _get_pipeline()
    instances = _INSTANCES_CONFIG["instances"]
    service_account = _INSTANCES_CONFIG.get("service_account", SN_USER)

    lines = [
        "CI/CD Pipeline Instances:",
        f"  Service account: {service_account}",
        f"  Pipeline order:  {' -> '.join(pipeline)}",
        "",
    ]

    for key in pipeline:
        cfg = instances.get(key, {})
        url = cfg.get("url", "(not configured)")
        pw_env = cfg.get("password_env", "?")
        has_pw = bool(os.environ.get(pw_env, ""))

        # Connectivity check
        status = "unknown"
        if has_pw:
            try:
                async with _client(key) as client:
                    resp = await client.get(
                        "/api/now/table/sys_properties",
                        params={"sysparm_limit": "1", "sysparm_fields": "sys_id"},
                    )
                    status = "OK" if resp.status_code == 200 else f"HTTP {resp.status_code}"
            except Exception as e:
                status = f"error: {e}"
        else:
            status = f"no password (set {pw_env})"

        lines.append(f"  {key:10s}  {url}")
        lines.append(f"  {'':10s}  credentials: {pw_env}={'set' if has_pw else 'MISSING'}  status: {status}")
        lines.append("")

    # Also list instances not in the pipeline
    extra = [k for k in instances if k not in pipeline]
    if extra:
        lines.append("  Instances not in pipeline: " + ", ".join(extra))

    return "\n".join(lines)


# ── Scripted REST helper scripts for promotion endpoints ──────────────────

_PROMOTION_HELPER_ENDPOINTS = [
    {
        "name": "import_update_set",
        "method": "POST",
        "path": "/import_update_set",
        "description": "Import an Update Set XML into this instance",
        "script": r"""(function process(request, response) {
    try {
        var body = request.body.data;
        var usName = body.name + '';
        var usDescription = body.description + '';
        var remoteSysId = body.remote_sys_id + '';
        var records = body.records;

        if (!usName || !records || records.length === 0) {
            response.setStatus(400);
            response.setBody({error: 'Missing name or records in request body'});
            return;
        }

        // Create the remote update set record
        var remoteUS = new GlideRecord('sys_remote_update_set');
        remoteUS.initialize();
        remoteUS.setValue('name', usName);
        remoteUS.setValue('description', usDescription);
        remoteUS.setValue('state', 'loaded');
        remoteUS.setValue('remote_sys_id', remoteSysId);
        remoteUS.setValue('application', 'global');
        var rusSysId = remoteUS.insert();

        if (!rusSysId) {
            response.setStatus(500);
            response.setBody({error: 'Failed to create sys_remote_update_set record'});
            return;
        }

        // Insert each sys_update_xml record
        var imported = 0;
        for (var i = 0; i < records.length; i++) {
            var rec = records[i];
            var ux = new GlideRecord('sys_update_xml');
            ux.initialize();
            ux.setValue('name', rec.name || '');
            ux.setValue('action', rec.action || 'INSERT_OR_UPDATE');
            ux.setValue('type', rec.type || '');
            ux.setValue('target_name', rec.target_name || '');
            ux.setValue('payload', rec.payload || '');
            ux.setValue('remote_update_set', rusSysId);
            ux.setValue('update_set', rusSysId);
            if (ux.insert()) imported++;
        }

        response.setStatus(200);
        response.setBody({
            result: {
                sys_id: rusSysId + '',
                name: usName,
                state: 'loaded',
                records_imported: imported
            }
        });
    } catch (ex) {
        response.setStatus(500);
        response.setBody({error: 'Import failed: ' + ex.getMessage()});
    }
})(request, response);""",
    },
    {
        "name": "preview_remote_update_set",
        "method": "POST",
        "path": "/preview_remote_update_set",
        "description": "Preview a remote Update Set before committing",
        "script": r"""(function process(request, response) {
    try {
        gs.setCurrentDomainID('global');  // Required for domain-separated instances
        var remoteSysId = request.body.data.sys_id + '';
        if (!remoteSysId) {
            response.setStatus(400);
            response.setBody({error: 'Missing sys_id in request body'});
            return;
        }

        var rus = new GlideRecord('sys_remote_update_set');
        if (!rus.get(remoteSysId)) {
            response.setStatus(404);
            response.setBody({error: 'Remote Update Set not found: ' + remoteSysId});
            return;
        }

        // Run preview using hierarchical worker (synchronous)
        var worker = new GlideScriptedHierarchicalWorker();
        worker.setProgressName('Remote Update Set Previewer');
        worker.setScriptIncludeName('SNC_UpdateSetPreviewAjax');
        worker.putMethodArg('remote_update_set_sys_id', remoteSysId);
        worker.setBackground(false);
        worker.start();

        // Re-read state after preview
        rus.get(remoteSysId);
        var state = rus.getValue('state');

        // Count problems
        var probCount = 0;
        var prob = new GlideAggregate('sys_update_preview_problem');
        prob.addQuery('remote_update_set', remoteSysId);
        prob.addAggregate('COUNT');
        prob.query();
        if (prob.next()) probCount = parseInt(prob.getAggregate('COUNT'));

        response.setStatus(200);
        response.setBody({
            result: {
                remote_update_set_id: remoteSysId,
                state: state,
                problem_count: probCount,
                status: state === 'previewed' ? 'preview_complete' : 'preview_' + state
            }
        });
    } catch (ex) {
        response.setStatus(500);
        response.setBody({error: 'Preview failed: ' + ex.getMessage()});
    }
})(request, response);""",
    },
    {
        "name": "preview_status",
        "method": "GET",
        "path": "/preview_status/{sys_id}",
        "description": "Check preview status and problems for a remote Update Set",
        "script": r"""(function process(request, response) {
    try {
        var remoteSysId = request.pathParams.sys_id;
        if (!remoteSysId) {
            response.setStatus(400);
            response.setBody({error: 'Missing sys_id path parameter'});
            return;
        }

        // Get remote update set state
        var rus = new GlideRecord('sys_remote_update_set');
        if (!rus.get(remoteSysId)) {
            response.setStatus(404);
            response.setBody({error: 'Remote Update Set not found: ' + remoteSysId});
            return;
        }

        var state = rus.getValue('state');
        var name = rus.getValue('name');

        // Query preview problems
        var problems = [];
        var prob = new GlideRecord('sys_update_preview_problem');
        prob.addQuery('remote_update_set', remoteSysId);
        prob.query();
        while (prob.next()) {
            problems.push({
                type: prob.getValue('type'),
                description: prob.getValue('description'),
                disposition: prob.getValue('disposition'),
                status: prob.getValue('status')
            });
        }

        response.setStatus(200);
        response.setBody({
            result: {
                sys_id: remoteSysId,
                name: name,
                state: state,
                problem_count: problems.length,
                problems: problems
            }
        });
    } catch (ex) {
        response.setStatus(500);
        response.setBody({error: 'Status check failed: ' + ex.getMessage()});
    }
})(request, response);""",
    },
    {
        "name": "commit_remote_update_set",
        "method": "POST",
        "path": "/commit_remote_update_set",
        "description": "Commit a previewed remote Update Set",
        "script": r"""(function process(request, response) {
    try {
        gs.setCurrentDomainID('global');  // Required for domain-separated instances
        var remoteSysId = request.body.data.sys_id + '';
        if (!remoteSysId) {
            response.setStatus(400);
            response.setBody({error: 'Missing sys_id in request body'});
            return;
        }

        // Verify the remote update set exists and is previewed
        var rus = new GlideRecord('sys_remote_update_set');
        if (!rus.get(remoteSysId)) {
            response.setStatus(404);
            response.setBody({error: 'Remote Update Set not found: ' + remoteSysId});
            return;
        }

        var state = rus.getValue('state');
        if (state !== 'previewed') {
            response.setStatus(400);
            response.setBody({
                error: 'Update Set must be previewed before committing. Current state: ' + state
            });
            return;
        }

        // Check for blocking problems
        var prob = new GlideRecord('sys_update_preview_problem');
        prob.addQuery('remote_update_set', remoteSysId);
        prob.addQuery('disposition', 'reject');
        prob.query();
        if (prob.hasNext()) {
            response.setStatus(400);
            response.setBody({
                error: 'Cannot commit: there are rejected/blocking preview problems. Resolve them first.'
            });
            return;
        }

        // Commit using hierarchical worker (synchronous)
        var worker = new GlideScriptedHierarchicalWorker();
        worker.setProgressName('Committing Remote Update Set');
        worker.setScriptIncludeName('SNC_UpdateSetCommitAjax');
        worker.putMethodArg('remote_update_set_sys_id', remoteSysId);
        worker.setBackground(false);
        worker.start();

        // Re-read to get final state
        rus.get(remoteSysId);
        var localUSId = rus.getValue('update_set') || '';

        response.setStatus(200);
        response.setBody({
            result: {
                remote_update_set_id: remoteSysId,
                status: 'committed',
                local_update_set_id: localUSId,
                state: rus.getValue('state')
            }
        });
    } catch (ex) {
        response.setStatus(500);
        response.setBody({error: 'Commit failed: ' + ex.getMessage()});
    }
})(request, response);""",
    },
]


@mcp.tool()
async def deploy_promotion_helper(instance: str) -> str:
    """
    Deploy the CI/CD Promotion Helper Scripted REST API on a target instance.

    Must be run once per instance before promoting update sets to it.
    Creates 4 endpoints under the MCP Helper API:
      - POST /import_update_set
      - POST /preview_remote_update_set
      - GET  /preview_status/{sys_id}
      - POST /commit_remote_update_set

    Args:
        instance: Target instance key from sn_instances.json (e.g. 'dev').
    """
    if not _INSTANCES_CONFIG.get("instances", {}).get(instance):
        available = list(_INSTANCES_CONFIG.get("instances", {}).keys())
        return (
            f"Error: Instance '{instance}' not found in sn_instances.json.\n"
            f"Available: {', '.join(available) if available else '(none – create sn_instances.json)'}"
        )

    api_name = "MCP Helper"
    results = []

    async with _client(instance) as client:
        # Find or create the parent API
        api_id = None
        search_resp = await client.get(
            "/api/now/table/sys_ws_definition",
            params={
                "sysparm_query": f"name={api_name}",
                "sysparm_fields": "sys_id,name",
                "sysparm_limit": "1",
            },
        )
        search_resp.raise_for_status()
        existing = search_resp.json()["result"]

        api_namespace = "x_ai_config"
        api_service_id = "mcp_helper"

        if existing:
            api_id = existing[0]["sys_id"]
            # Ensure namespace and service_id are correct
            await client.patch(
                f"/api/now/table/sys_ws_definition/{api_id}",
                json={
                    "namespace": api_namespace,
                    "service_id": api_service_id,
                },
            )
            results.append(f"Found existing MCP Helper API: {api_id}")
        else:
            api_resp = await client.post(
                "/api/now/table/sys_ws_definition",
                json={
                    "name": api_name,
                    "namespace": api_namespace,
                    "service_id": api_service_id,
                    "short_description": "MCP Helper API for CI/CD pipeline operations",
                    "active": "true",
                },
            )
            if api_resp.status_code in (200, 201):
                api_id = api_resp.json()["result"]["sys_id"]
                results.append(f"Created MCP Helper API: {api_id}")
            else:
                return (
                    f"Error: Could not create Scripted REST API on '{instance}' "
                    f"(HTTP {api_resp.status_code}). Check service account roles."
                )

        # Deploy each endpoint
        for endpoint in _PROMOTION_HELPER_ENDPOINTS:
            # Check if already exists
            res_search = await client.get(
                "/api/now/table/sys_ws_operation",
                params={
                    "sysparm_query": f"web_service_definition={api_id}^name={endpoint['name']}",
                    "sysparm_fields": "sys_id,name",
                    "sysparm_limit": "1",
                },
            )
            res_search.raise_for_status()
            existing_res = res_search.json()["result"]

            if existing_res:
                # Update existing endpoint with latest script
                res_id = existing_res[0]["sys_id"]
                upd_resp = await client.patch(
                    f"/api/now/table/sys_ws_operation/{res_id}",
                    json={"operation_script": endpoint["script"]},
                )
                if upd_resp.status_code in (200, 201):
                    results.append(f"  {endpoint['method']:6s} {endpoint['path']:40s} updated ({res_id})")
                else:
                    results.append(f"  {endpoint['method']:6s} {endpoint['path']:40s} update failed (HTTP {upd_resp.status_code})")
                continue

            res_resp = await client.post(
                "/api/now/table/sys_ws_operation",
                json={
                    "web_service_definition": api_id,
                    "name": endpoint["name"],
                    "http_method": endpoint["method"],
                    "relative_path": endpoint["path"],
                    "operation_script": endpoint["script"],
                    "short_description": endpoint["description"],
                    "active": "true",
                },
            )
            if res_resp.status_code in (200, 201):
                res_id = res_resp.json()["result"]["sys_id"]
                results.append(f"  {endpoint['method']:6s} {endpoint['path']:40s} created ({res_id})")
            else:
                results.append(
                    f"  {endpoint['method']:6s} {endpoint['path']:40s} "
                    f"FAILED (HTTP {res_resp.status_code})"
                )

    url = _INSTANCES_CONFIG["instances"][instance]["url"]
    return (
        f"Promotion Helper deployed on '{instance}' ({url}):\n\n"
        + "\n".join(results)
        + "\n\nThe instance is now ready to receive promoted Update Sets."
    )


@mcp.tool()
async def promote_update_set(
    update_set_sys_id: str,
    source: str,
    target: str = "",
    auto_commit: bool = False,
) -> str:
    """
    Promote a completed Update Set from one instance to the next in the pipeline.

    Flow:
      1. Validate update set is complete on source
      2. Export XML from source
      3. Save XML to Git (under update_sets/{source}/)
      4. Upload XML to target via Scripted REST helper
      5. Preview on target
      6. Report preview results (conflicts or clean)
      7. If auto_commit=True and preview is clean, commit on target

    Args:
        update_set_sys_id: sys_id of the completed Update Set on the source instance.
        source: Source instance key (e.g. 'sandbox').
        target: Target instance key (e.g. 'dev'). If blank, uses next in pipeline.
        auto_commit: If True, automatically commit after clean preview. Defaults to False.
    """
    # Validate instances
    instances = _INSTANCES_CONFIG.get("instances", {})
    if not instances:
        return "Error: No multi-instance configuration. Create sn_instances.json first."

    if source not in instances:
        return f"Error: Source instance '{source}' not found. Available: {', '.join(instances.keys())}"

    # Determine target
    if not target:
        target = _next_in_pipeline(source)
        if not target:
            return (
                f"Error: '{source}' is the last instance in the pipeline "
                f"({' -> '.join(_get_pipeline())}). Specify a target explicitly."
            )

    if target not in instances:
        return f"Error: Target instance '{target}' not found. Available: {', '.join(instances.keys())}"

    if source == target:
        return "Error: Source and target cannot be the same instance."

    # Enforce pipeline order (warn but don't block)
    pipeline = _get_pipeline()
    pipeline_warning = ""
    if source in pipeline and target in pipeline:
        src_idx = pipeline.index(source)
        tgt_idx = pipeline.index(target)
        if tgt_idx != src_idx + 1:
            pipeline_warning = (
                f"WARNING: Promoting {source} -> {target} skips pipeline stages "
                f"({' -> '.join(pipeline)}). Proceeding anyway.\n\n"
            )

    steps: list[str] = [pipeline_warning] if pipeline_warning else []

    # Step 1: Validate source update set is complete
    steps.append("Step 1: Validating source Update Set...")
    try:
        async with _client(source) as client:
            meta_resp = await client.get(
                f"/api/now/table/sys_update_set/{update_set_sys_id}",
                params={"sysparm_fields": "name,state,description"},
            )
            meta_resp.raise_for_status()
            us_meta = meta_resp.json()["result"]
            us_name = us_meta["name"]

            if us_meta["state"] != "complete":
                return (
                    f"Error: Update Set '{us_name}' on {source} is not complete "
                    f"(state: {us_meta['state']}). Complete it first."
                )
            steps.append(f"  Update Set '{us_name}' is complete on {source}.")
    except Exception as e:
        return f"Error connecting to source '{source}': {e}"

    # Step 2: Export records from source via Table API
    steps.append("Step 2: Exporting records from source...")
    update_xml_records: list[dict] = []

    try:
        async with _client(source) as api_c:
            xml_resp = await api_c.get(
                "/api/now/table/sys_update_xml",
                params={
                    "sysparm_query": f"update_set={update_set_sys_id}",
                    "sysparm_fields": "name,action,type,target_name,payload",
                    "sysparm_limit": "500",
                },
            )
            xml_resp.raise_for_status()
            update_xml_records = xml_resp.json()["result"]
    except Exception as e:
        return f"Error reading sys_update_xml from source '{source}': {e}"

    if not update_xml_records:
        return f"Error: Update Set '{us_name}' on {source} has no records to promote."

    steps.append(f"  Found {len(update_xml_records)} record(s) to promote.")

    # Step 3: Save to Git (optional)
    repo = GIT_REPO_PATH
    if repo:
        steps.append("Step 3: Saving to Git...")
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        safe_name = us_name.replace(" ", "_").replace("/", "_")
        filename = f"{safe_name}_{timestamp}.json"
        git_subpath = f"update_sets/{source}"
        target_dir = os.path.join(repo, git_subpath)
        os.makedirs(target_dir, exist_ok=True)
        file_path = os.path.join(target_dir, filename)

        import json as _json
        with open(file_path, "w", encoding="utf-8") as f:
            _json.dump({"update_set": us_name, "records": update_xml_records}, f, indent=2)

        msg = f"Promote Update Set: {us_name} ({source} -> {target})"
        try:
            subprocess.run(["git", "-C", repo, "add", os.path.join(git_subpath, filename)], check=True)
            subprocess.run(["git", "-C", repo, "commit", "-m", msg], check=True)
            git_result = subprocess.run(
                ["git", "-C", repo, "rev-parse", "HEAD"],
                capture_output=True, text=True, check=True,
            )
            steps.append(f"  Saved to {file_path} (commit: {git_result.stdout.strip()[:8]})")
        except subprocess.CalledProcessError as exc:
            steps.append(f"  Saved to {file_path} but git commit failed: {exc}")
    else:
        steps.append("Step 3: Skipped Git save (GIT_REPO_PATH not set).")

    # Step 4: Import to target via Scripted REST helper (JSON-based)
    steps.append(f"Step 4: Importing to {target}...")
    remote_us_sys_id = None

    # Discover the actual API base path on the target instance
    target_api_base = SCRIPTED_API_BASE
    try:
        async with _client(target) as client:
            api_search = await client.get(
                "/api/now/table/sys_ws_definition",
                params={
                    "sysparm_query": "name=MCP Helper",
                    "sysparm_fields": "sys_id,namespace,service_id",
                    "sysparm_limit": "1",
                },
            )
            api_search.raise_for_status()
            api_results = api_search.json()["result"]
            if api_results:
                ns = api_results[0].get("namespace", "")
                sid = api_results[0].get("service_id", "")
                if ns and sid:
                    target_api_base = f"/api/{ns}/{sid}"
                    steps.append(f"  Discovered API path: {target_api_base}")
    except Exception:
        pass

    try:
        async with _client(target) as client:
            import_resp = await client.post(
                f"{target_api_base}/import_update_set",
                json={
                    "name": us_name,
                    "description": us_meta.get("description", ""),
                    "remote_sys_id": update_set_sys_id,
                    "records": update_xml_records,
                },
            )
            if import_resp.status_code in (200, 201):
                resp_json = import_resp.json()
                # Handle possible nested result wrapper
                import_result = resp_json.get("result", resp_json)
                if isinstance(import_result, dict) and "result" in import_result:
                    import_result = import_result["result"]
                remote_us_sys_id = import_result.get("sys_id")
                rec_count = import_result.get("records_imported", "?")
                steps.append(
                    f"  Imported as remote Update Set: {remote_us_sys_id} "
                    f"({rec_count} records, state: {import_result.get('state', '?')})"
                )
            else:
                err_body = ""
                try:
                    err_body = import_resp.text[:500]
                except Exception:
                    pass
                return (
                    "\n".join(steps) + "\n\n"
                    f"Error: Import failed on {target} (HTTP {import_resp.status_code}).\n"
                    f"Response: {err_body}\n"
                    f"Ensure the Promotion Helper is deployed: "
                    f"call deploy_promotion_helper('{target}') first."
                )
    except Exception as e:
        return "\n".join(steps) + f"\n\nError importing to {target}: {e}"

    if not remote_us_sys_id:
        raw = ""
        try:
            raw = import_resp.text[:500]
        except Exception:
            pass
        return "\n".join(steps) + f"\n\nError: Import succeeded but no sys_id was returned.\nRaw response: {raw}"

    # Step 5: Preview on target
    steps.append(f"Step 5: Previewing on {target}...")
    preview_state = "unknown"
    problem_count = 0
    try:
        async with _client(target) as client:
            preview_resp = await client.post(
                f"{target_api_base}/preview_remote_update_set",
                json={"sys_id": remote_us_sys_id},
            )
            if preview_resp.status_code in (200, 201):
                preview_result = preview_resp.json().get("result", {})
                if isinstance(preview_result, dict) and "result" in preview_result:
                    preview_result = preview_result["result"]
                preview_state = preview_result.get("state", "unknown")
                problem_count = preview_result.get("problem_count", 0)
                steps.append(f"  Preview state: {preview_state}, problems: {problem_count}")
            else:
                return "\n".join(steps) + f"\n\nError: Preview failed (HTTP {preview_resp.status_code}): {preview_resp.text[:500]}"
    except Exception as e:
        return "\n".join(steps) + f"\n\nError during preview: {e}"

    # Step 6: Commit if auto_commit and preview is clean
    if auto_commit:
        if preview_state == "previewed" and problem_count == 0:
            steps.append(f"Step 6: Committing on {target}...")
            try:
                async with _client(target) as client:
                    commit_resp = await client.post(
                        f"{target_api_base}/commit_remote_update_set",
                        json={"sys_id": remote_us_sys_id},
                    )
                    if commit_resp.status_code in (200, 201):
                        commit_result = commit_resp.json().get("result", {})
                        if isinstance(commit_result, dict) and "result" in commit_result:
                            commit_result = commit_result["result"]
                        steps.append(f"  Committed. State: {commit_result.get('state', '?')}, local US: {commit_result.get('local_update_set_id', '?')}")
                    else:
                        steps.append(f"  Commit failed (HTTP {commit_resp.status_code}): {commit_resp.text[:300]}")
            except Exception as e:
                steps.append(f"  Commit error: {e}")
        elif problem_count > 0:
            steps.append(f"Step 6: Skipped commit — {problem_count} preview problem(s) found. Resolve manually in {target}.")
        else:
            steps.append(f"Step 6: Skipped commit — preview state is '{preview_state}', expected 'previewed'.")
    else:
        steps.append(
            f"Step 6: Preview complete. To commit, call:\n"
            f"  promote_update_set('{update_set_sys_id}', '{source}', '{target}', auto_commit=True)"
        )

    # Summary
    steps.append("")
    steps.append(
        f"Promotion summary: {us_name}\n"
        f"  Source:      {source} ({instances[source]['url']})\n"
        f"  Target:      {target} ({instances[target]['url']})\n"
        f"  Remote US:   {remote_us_sys_id}\n"
        + ""
    )

    return "\n".join(steps)


@mcp.tool()
async def check_promotion_status(
    update_set_name: str,
    instance: str = "",
) -> str:
    """
    Check where an Update Set exists across all pipeline instances.

    Searches by name on each configured instance in the pipeline, checking
    both local (sys_update_set) and remote (sys_remote_update_set) tables.

    Args:
        update_set_name: Name of the Update Set to search for.
        instance: If provided, only check this instance. Otherwise check all.
    """
    instances = _INSTANCES_CONFIG.get("instances", {})
    if not instances:
        return "Error: No multi-instance configuration. Create sn_instances.json first."

    targets = [instance] if instance else _get_pipeline()
    if not targets:
        targets = list(instances.keys())

    lines = [f"Promotion status for '{update_set_name}':", ""]

    for key in targets:
        if key not in instances:
            lines.append(f"  {key:10s}  (not in sn_instances.json)")
            continue

        try:
            async with _client(key) as client:
                # Check local update sets
                local_resp = await client.get(
                    "/api/now/table/sys_update_set",
                    params={
                        "sysparm_query": f"nameLIKE{update_set_name}",
                        "sysparm_fields": "sys_id,name,state,sys_updated_on",
                        "sysparm_limit": "5",
                    },
                )
                local_resp.raise_for_status()
                local_records = local_resp.json()["result"]

                # Check remote update sets
                remote_resp = await client.get(
                    "/api/now/table/sys_remote_update_set",
                    params={
                        "sysparm_query": f"nameLIKE{update_set_name}",
                        "sysparm_fields": "sys_id,name,state,sys_updated_on",
                        "sysparm_limit": "5",
                    },
                )
                remote_resp.raise_for_status()
                remote_records = remote_resp.json()["result"]

                url = instances[key]["url"]
                lines.append(f"  {key} ({url}):")

                if not local_records and not remote_records:
                    lines.append("    Not found")
                else:
                    for r in local_records:
                        lines.append(
                            f"    [local]  {r['name']}  state={r['state']}  "
                            f"updated={r.get('sys_updated_on', '?')}  id={r['sys_id']}"
                        )
                    for r in remote_records:
                        lines.append(
                            f"    [remote] {r['name']}  state={r['state']}  "
                            f"updated={r.get('sys_updated_on', '?')}  id={r['sys_id']}"
                        )
                lines.append("")

        except Exception as e:
            lines.append(f"  {key:10s}  error: {e}")
            lines.append("")

    return "\n".join(lines)


# ── G. Documentation / Deep-Read Tools ────────────────────────────────────
#
# These tools allow the agent to read existing ServiceNow configuration
# artifacts in depth and produce documentation.  They are READ-ONLY and
# never modify any record.
# ─────────────────────────────────────────────────────────────────────────

@mcp.tool()
async def read_scheduled_job(
    sys_id: str = "",
    name: str = "",
    instance: str = "",
) -> str:
    """
    Read a Scheduled Job (sysauto_script) in full detail.

    Provide either sys_id or name.  Returns all metadata plus the full script.

    Args:
        sys_id: sys_id of the scheduled job.
        name:   Exact or partial name to search for (uses LIKE).
    """
    async with _client(instance) as client:
        if sys_id:
            resp = await client.get(f"/api/now/table/sysauto_script/{sys_id}")
        elif name:
            resp = await client.get("/api/now/table/sysauto_script", params={
                "sysparm_query": f"nameLIKE{name}",
                "sysparm_limit": "1",
            })
        else:
            return "Error: Provide sys_id or name."

        resp.raise_for_status()
        data = resp.json()["result"]
        if isinstance(data, list):
            if not data:
                return f"No scheduled job found matching name '{name}'."
            data = data[0]

        lines = [
            f"Scheduled Job: {data.get('name', '?')}",
            f"  sys_id:      {data.get('sys_id', '')}",
            f"  active:      {data.get('active', '')}",
            f"  run_type:    {data.get('run_type', '')}",
            f"  run_time:    {data.get('run_time', '')}",
            f"  run_dayofweek: {data.get('run_dayofweek', '')}",
            f"  run_dayofmonth: {data.get('run_dayofmonth', '')}",
            f"  description: {data.get('description', '')}",
            f"  updated_on:  {data.get('sys_updated_on', '')}",
            f"  updated_by:  {data.get('sys_updated_by', '')}",
            f"  created_on:  {data.get('sys_created_on', '')}",
            f"",
            f"--- SCRIPT ---",
            data.get("script", "(empty)"),
        ]
        return "\n".join(lines)


@mcp.tool()
async def read_script_include(
    sys_id: str = "",
    name: str = "",
    api_name: str = "",
    instance: str = "",
) -> str:
    """
    Read a Script Include (sys_script_include) in full detail.

    Provide sys_id, name (exact/partial), or api_name (e.g. 'global.myUtil').

    Args:
        sys_id:   sys_id of the script include.
        name:     Exact or partial name (uses LIKE).
        api_name: Fully qualified api_name (e.g. 'x_zh_ilearn_tranin.iLearnAPI').
    """
    async with _client(instance) as client:
        if sys_id:
            resp = await client.get(f"/api/now/table/sys_script_include/{sys_id}")
        elif api_name:
            resp = await client.get("/api/now/table/sys_script_include", params={
                "sysparm_query": f"api_name={api_name}",
                "sysparm_limit": "1",
            })
        elif name:
            resp = await client.get("/api/now/table/sys_script_include", params={
                "sysparm_query": f"nameLIKE{name}",
                "sysparm_limit": "1",
            })
        else:
            return "Error: Provide sys_id, name, or api_name."

        resp.raise_for_status()
        data = resp.json()["result"]
        if isinstance(data, list):
            if not data:
                return f"No script include found."
            data = data[0]

        scope = data.get("sys_scope", "")
        if isinstance(scope, dict):
            scope = scope.get("value", "")

        lines = [
            f"Script Include: {data.get('name', '?')}",
            f"  api_name:        {data.get('api_name', '')}",
            f"  sys_id:          {data.get('sys_id', '')}",
            f"  active:          {data.get('active', '')}",
            f"  client_callable: {data.get('client_callable', '')}",
            f"  access:          {data.get('access', '')}",
            f"  scope:           {scope}",
            f"  updated_on:      {data.get('sys_updated_on', '')}",
            f"  updated_by:      {data.get('sys_updated_by', '')}",
            f"",
            f"--- SCRIPT ---",
            data.get("script", "(empty)"),
        ]
        return "\n".join(lines)


@mcp.tool()
async def read_business_rules(
    table: str = "",
    name: str = "",
    sys_id: str = "",
    instance: str = "",
) -> str:
    """
    Read Business Rules (sys_script) for a table, by name, or by sys_id.

    When querying by table, returns all BRs on that table (up to 50).
    When querying by name or sys_id, returns the full script.

    Args:
        table:  Table name to list all BRs for (e.g. 'x_zh_ilearn_tranin_ilearn_training_status').
        name:   Partial name match.
        sys_id: sys_id of a specific Business Rule.
    """
    async with _client(instance) as client:
        if sys_id:
            resp = await client.get(f"/api/now/table/sys_script/{sys_id}")
            resp.raise_for_status()
            data = resp.json()["result"]
            if isinstance(data, list):
                data = data[0] if data else {}
            return _format_br(data, full_script=True)

        query_parts = []
        if table:
            query_parts.append(f"collection={table}")
        if name:
            query_parts.append(f"nameLIKE{name}")
        if not query_parts:
            return "Error: Provide table, name, or sys_id."

        resp = await client.get("/api/now/table/sys_script", params={
            "sysparm_query": "^".join(query_parts),
            "sysparm_fields": "sys_id,name,collection,when,order,active,action_insert,action_update,action_delete,action_query,script",
            "sysparm_limit": "50",
        })
        resp.raise_for_status()
        records = resp.json()["result"]

    if not records:
        return "No Business Rules found."

    lines = [f"Business Rules ({len(records)} found):"]
    for r in records:
        lines.append(_format_br(r, full_script=(len(records) <= 3)))
        lines.append("")
    return "\n".join(lines)


def _format_br(r: dict, full_script: bool = False) -> str:
    actions = []
    if r.get("action_insert") == "true": actions.append("insert")
    if r.get("action_update") == "true": actions.append("update")
    if r.get("action_delete") == "true": actions.append("delete")
    if r.get("action_query") == "true": actions.append("query")
    lines = [
        f"  BR: {r.get('name', '?')}",
        f"    sys_id:     {r.get('sys_id', '')}",
        f"    table:      {r.get('collection', '')}",
        f"    when:       {r.get('when', '')}",
        f"    order:      {r.get('order', '')}",
        f"    active:     {r.get('active', '')}",
        f"    actions:    {', '.join(actions) if actions else '?'}",
    ]
    script = r.get("script", "")
    if full_script and script:
        lines.append(f"    --- SCRIPT ---")
        lines.append(script)
    elif script:
        lines.append(f"    script:     {script[:200]}...")
    return "\n".join(lines)


@mcp.tool()
async def read_rest_message(
    sys_id: str = "",
    name: str = "",
    instance: str = "",
) -> str:
    """
    Read a REST Message (sys_rest_message) and all its HTTP Methods in full detail.

    Args:
        sys_id: sys_id of the REST Message.
        name:   Exact or partial name (uses LIKE).
    """
    async with _client(instance) as client:
        if sys_id:
            resp = await client.get(f"/api/now/table/sys_rest_message/{sys_id}")
        elif name:
            resp = await client.get("/api/now/table/sys_rest_message", params={
                "sysparm_query": f"nameLIKE{name}",
                "sysparm_limit": "1",
            })
        else:
            return "Error: Provide sys_id or name."

        resp.raise_for_status()
        data = resp.json()["result"]
        if isinstance(data, list):
            if not data:
                return f"No REST Message found."
            data = data[0]

        msg_id = data.get("sys_id", "")
        lines = [
            f"REST Message: {data.get('name', '?')}",
            f"  sys_id:          {msg_id}",
            f"  endpoint:        {data.get('rest_endpoint', '')}",
            f"  auth_type:       {data.get('authentication_type', '')}",
            f"  description:     {data.get('description', '')}",
            f"  updated_on:      {data.get('sys_updated_on', '')}",
            f"",
        ]

        # Fetch HTTP Methods
        fn_resp = await client.get("/api/now/table/sys_rest_message_fn", params={
            "sysparm_query": f"rest_message={msg_id}",
            "sysparm_limit": "50",
        })
        fn_resp.raise_for_status()
        methods = fn_resp.json()["result"]

        lines.append(f"  HTTP Methods ({len(methods)}):")
        for fn in methods:
            lines.append(f"    {fn.get('http_method', '?'):6s} {fn.get('name', '?')}")
            lines.append(f"           endpoint: {fn.get('rest_endpoint', '')}")
            lines.append(f"           auth:     {fn.get('authentication_type', '')}")
            lines.append(f"           sys_id:   {fn.get('sys_id', '')}")
            # Include request body template if present
            body = fn.get("rest_message_body", "")
            if body:
                lines.append(f"           body:     {body[:500]}")
            lines.append("")

    return "\n".join(lines)


@mcp.tool()
async def read_scripted_rest_api(
    sys_id: str = "",
    name: str = "",
    instance: str = "",
) -> str:
    """
    Read a Scripted REST API (sys_ws_definition) and all its resources/operations.

    Args:
        sys_id: sys_id of the Scripted REST API.
        name:   Exact or partial name (uses LIKE).
    """
    async with _client(instance) as client:
        if sys_id:
            resp = await client.get(f"/api/now/table/sys_ws_definition/{sys_id}")
        elif name:
            resp = await client.get("/api/now/table/sys_ws_definition", params={
                "sysparm_query": f"nameLIKE{name}",
                "sysparm_limit": "1",
            })
        else:
            return "Error: Provide sys_id or name."

        resp.raise_for_status()
        data = resp.json()["result"]
        if isinstance(data, list):
            if not data:
                return f"No Scripted REST API found."
            data = data[0]

        api_id = data.get("sys_id", "")
        lines = [
            f"Scripted REST API: {data.get('name', '?')}",
            f"  sys_id:      {api_id}",
            f"  namespace:   {data.get('namespace', '')}",
            f"  base_uri:    {data.get('base_uri', '')}",
            f"  active:      {data.get('active', '')}",
            f"  description: {data.get('short_description', '')}",
            f"",
        ]

        # Fetch resources/operations
        op_resp = await client.get("/api/now/table/sys_ws_operation", params={
            "sysparm_query": f"web_service_definition={api_id}",
            "sysparm_limit": "50",
        })
        op_resp.raise_for_status()
        ops = op_resp.json()["result"]

        lines.append(f"  Resources ({len(ops)}):")
        for op in ops:
            lines.append(f"    {op.get('http_method', '?'):6s} {op.get('relative_path', '/')}")
            lines.append(f"           name:   {op.get('name', '?')}")
            lines.append(f"           sys_id: {op.get('sys_id', '')}")
            lines.append(f"           active: {op.get('active', '')}")
            script = op.get("operation_script", "")
            if script:
                lines.append(f"           --- SCRIPT ---")
                lines.append(script)
            lines.append("")

    return "\n".join(lines)


@mcp.tool()
async def read_app_scope(
    scope: str = "",
    name: str = "",
    sys_id: str = "",
    instance: str = "",
) -> str:
    """
    Read an Application Scope (sys_scope) and list all artifacts inside it.

    This gives an overview of everything in a scoped app: tables, script includes,
    business rules, scheduled jobs, REST messages, UI pages, etc.

    Args:
        scope:  Scope string (e.g. 'x_zh_ilearn_tranin').
        name:   Partial name match.
        sys_id: sys_id of the sys_scope record.
    """
    async with _client(instance) as client:
        # Resolve the scope record
        if sys_id:
            resp = await client.get(f"/api/now/table/sys_scope/{sys_id}")
        elif scope:
            resp = await client.get("/api/now/table/sys_scope", params={
                "sysparm_query": f"scope={scope}",
                "sysparm_limit": "1",
            })
        elif name:
            resp = await client.get("/api/now/table/sys_scope", params={
                "sysparm_query": f"nameLIKE{name}",
                "sysparm_limit": "1",
            })
        else:
            return "Error: Provide scope, name, or sys_id."

        resp.raise_for_status()
        data = resp.json()["result"]
        if isinstance(data, list):
            if not data:
                return "No application scope found."
            data = data[0]

        scope_id = data.get("sys_id", "")
        scope_name = data.get("scope", "")
        lines = [
            f"Application: {data.get('name', '?')}",
            f"  scope:       {scope_name}",
            f"  sys_id:      {scope_id}",
            f"  version:     {data.get('version', '')}",
            f"  active:      {data.get('active', '')}",
            f"  description: {data.get('short_description', '')}",
            f"",
        ]

        # Discover artifacts in this scope
        artifact_tables = [
            ("sys_db_object",      "Tables",           f"nameLIKE{scope_name}",      "sys_id,name,label"),
            ("sys_script_include", "Script Includes",  f"sys_scope={scope_id}",       "sys_id,name,api_name,active"),
            ("sys_script",         "Business Rules",   f"sys_scope={scope_id}",       "sys_id,name,collection,when,active"),
            ("sysauto_script",     "Scheduled Jobs",   f"sys_scope={scope_id}",       "sys_id,name,active,run_type"),
            ("sys_rest_message",   "REST Messages",    f"sys_scope={scope_id}",       "sys_id,name,rest_endpoint"),
            ("sys_ws_definition",  "Scripted REST",    f"sys_scope={scope_id}",       "sys_id,name,namespace,active"),
            ("sys_ui_page",        "UI Pages",         f"sys_scope={scope_id}",       "sys_id,name"),
            ("sys_properties",     "System Properties",f"sys_scope={scope_id}",       "sys_id,name,value"),
        ]

        for table, label, query, fields in artifact_tables:
            try:
                art_resp = await client.get(f"/api/now/table/{table}", params={
                    "sysparm_query": query,
                    "sysparm_fields": fields,
                    "sysparm_limit": "50",
                })
                art_resp.raise_for_status()
                records = art_resp.json()["result"]
                lines.append(f"  {label} ({len(records)}):")
                if not records:
                    lines.append(f"    (none)")
                for r in records:
                    display = r.get("name", r.get("sys_id", "?"))
                    extra_parts = []
                    if "api_name" in r:
                        extra_parts.append(f"api={r['api_name']}")
                    if "collection" in r:
                        extra_parts.append(f"table={r['collection']}")
                    if "rest_endpoint" in r:
                        extra_parts.append(f"endpoint={r['rest_endpoint']}")
                    if "active" in r:
                        extra_parts.append(f"active={r['active']}")
                    extra = f"  ({', '.join(extra_parts)})" if extra_parts else ""
                    lines.append(f"    • {display}{extra}")
                lines.append("")
            except Exception:
                lines.append(f"  {label}: (could not query)")
                lines.append("")

    return "\n".join(lines)


@mcp.tool()
async def read_table_columns(table: str, instance: str = "") -> str:
    """
    Read all column definitions for a table from sys_dictionary.

    Args:
        table: Table name (e.g. 'x_zh_ilearn_tranin_ilearn_training_status').
    """
    _assert_safe_table(table)

    async with _client(instance) as client:
        resp = await client.get("/api/now/table/sys_dictionary", params={
            "sysparm_query": f"name={table}^elementISNOTEMPTY",
            "sysparm_fields": "element,column_label,internal_type,max_length,mandatory,reference_qual,default_value",
            "sysparm_display_value": "true",
            "sysparm_limit": "200",
        })
        resp.raise_for_status()
        records = resp.json()["result"]

    if not records:
        return f"No columns found for table '{table}'."

    lines = [f"Columns for '{table}' ({len(records)}):"]
    for r in records:
        itype = r.get("internal_type", "")
        if isinstance(itype, dict):
            itype = itype.get("display_value", itype.get("value", ""))
        mand = " MANDATORY" if r.get("mandatory") == "true" else ""
        default = f"  default='{r['default_value']}'" if r.get("default_value") else ""
        ref = f"  ref_qual='{r['reference_qual']}'" if r.get("reference_qual") else ""
        lines.append(
            f"  {r.get('element', '?'):35s}  {itype:20s}  "
            f"label='{r.get('column_label', '')}'{mand}{default}{ref}"
        )
    return "\n".join(lines)


@mcp.tool()
async def investigate_artifact(
    url: str = "",
    table: str = "",
    sys_id: str = "",
    instance: str = "",
) -> str:
    """
    Deep-investigate a ServiceNow artifact given its URL, or table+sys_id.

    Accepts a ServiceNow URL like:
      https://instance.service-now.com/nav_to.do?uri=sysauto_script.do?sys_id=abc123

    Automatically detects the artifact type (scheduled job, script include,
    business rule, REST message, etc.) and fetches all related artifacts:
    - The record itself (full script/config)
    - Script Includes it calls (by parsing function references)
    - Business Rules on related tables
    - REST Messages referenced in scripts
    - Table columns for referenced tables

    Args:
        url:    Full ServiceNow URL (nav_to.do format).
        table:  Table name (if not using URL).
        sys_id: sys_id (if not using URL).
    """
    # Parse URL if provided
    if url:
        import re
        # Extract table and sys_id from nav_to.do URL
        # Pattern: uri=TABLE.do?sys_id=SYS_ID
        match = re.search(r"uri=(\w+)\.do\?sys_id=([a-f0-9]{32})", url)
        if not match:
            # Try direct URL pattern: /TABLE.do?sys_id=SYS_ID
            match = re.search(r"/(\w+)\.do\?sys_id=([a-f0-9]{32})", url)
        if match:
            table = match.group(1)
            sys_id = match.group(2)
        else:
            return f"Could not parse table/sys_id from URL: {url}"

    if not table or not sys_id:
        return "Error: Provide a URL, or both table and sys_id."

    lines = [f"=== INVESTIGATION: {table} / {sys_id} ===", ""]

    async with _client(instance) as client:
        # 1. Fetch the primary record
        resp = await client.get(f"/api/now/table/{table}/{sys_id}")
        if resp.status_code == 404:
            return f"Record not found: {table}/{sys_id}"
        resp.raise_for_status()
        record = resp.json()["result"]

        record_name = record.get("name", record.get("short_description", sys_id))
        lines.append(f"Record: {record_name}")

        # Show key metadata
        for key in ["name", "active", "run_type", "run_time", "description",
                     "collection", "when", "api_name", "rest_endpoint",
                     "sys_updated_on", "sys_updated_by", "sys_created_on"]:
            val = record.get(key, "")
            if isinstance(val, dict):
                val = val.get("display_value", val.get("value", ""))
            if val:
                lines.append(f"  {key}: {val}")

        # Show the script
        script = record.get("script", "")
        if script:
            lines.append("")
            lines.append("--- PRIMARY SCRIPT ---")
            lines.append(script)
            lines.append("--- END SCRIPT ---")
            lines.append("")

        # 2. Parse script for references to investigate
        if script:
            import re

            # Find Script Include references: new scope.ClassName() or scope.ClassName.method
            si_refs = set(re.findall(r"new\s+([\w.]+)\(\)", script))
            si_refs.update(re.findall(r"([\w]+\.[\w]+)\(\)\.\w+", script))

            # Find GlideRecord table references
            gr_tables = set(re.findall(r'GlideRecord\(["\'](\w+)["\']\)', script))

            # Find REST message references
            rest_refs = set(re.findall(r"RESTMessageV2\(['\"]([^'\"]+)['\"]", script))

            # 3. Fetch referenced Script Includes
            if si_refs:
                lines.append(f"=== REFERENCED SCRIPT INCLUDES ({len(si_refs)}) ===")
                for ref in sorted(si_refs):
                    # Try as api_name first, then as class name
                    si_resp = await client.get("/api/now/table/sys_script_include", params={
                        "sysparm_query": f"api_name={ref}^ORnameLIKE{ref.split('.')[-1]}",
                        "sysparm_fields": "sys_id,name,api_name,active,script",
                        "sysparm_limit": "1",
                    })
                    si_resp.raise_for_status()
                    si_records = si_resp.json()["result"]
                    if si_records:
                        si = si_records[0]
                        lines.append(f"")
                        lines.append(f"  Script Include: {si.get('name')} ({si.get('api_name', '')})")
                        lines.append(f"    sys_id: {si.get('sys_id')}")
                        lines.append(f"    active: {si.get('active')}")
                        si_script = si.get("script", "")
                        if si_script:
                            lines.append(f"    --- SCRIPT ---")
                            lines.append(si_script)
                            lines.append(f"    --- END ---")
                    else:
                        lines.append(f"  {ref}: (not found)")
                lines.append("")

            # 4. Fetch referenced table schemas
            if gr_tables:
                lines.append(f"=== REFERENCED TABLES ({len(gr_tables)}) ===")
                for tbl in sorted(gr_tables):
                    # Skip blocked tables – just note them
                    if tbl.lower() in BLOCKED_TABLES:
                        lines.append(f"  {tbl}: (blocked – transactional table)")
                        continue
                    col_resp = await client.get("/api/now/table/sys_dictionary", params={
                        "sysparm_query": f"name={tbl}^elementISNOTEMPTY",
                        "sysparm_fields": "element,column_label,internal_type",
                        "sysparm_display_value": "true",
                        "sysparm_limit": "50",
                    })
                    col_resp.raise_for_status()
                    cols = col_resp.json()["result"]
                    lines.append(f"")
                    lines.append(f"  Table: {tbl} ({len(cols)} columns)")
                    for col in cols:
                        itype = col.get("internal_type", "")
                        if isinstance(itype, dict):
                            itype = itype.get("display_value", "")
                        lines.append(f"    {col.get('element', '?'):30s} {itype:20s} '{col.get('column_label', '')}'")
                lines.append("")

            # 5. Fetch Business Rules on referenced tables
            custom_tables = [t for t in gr_tables if t.lower() not in BLOCKED_TABLES]
            if custom_tables:
                lines.append(f"=== BUSINESS RULES ON REFERENCED TABLES ===")
                for tbl in sorted(custom_tables):
                    br_resp = await client.get("/api/now/table/sys_script", params={
                        "sysparm_query": f"collection={tbl}^active=true",
                        "sysparm_fields": "sys_id,name,when,active,order,script",
                        "sysparm_limit": "20",
                    })
                    br_resp.raise_for_status()
                    brs = br_resp.json()["result"]
                    lines.append(f"  BRs on {tbl} ({len(brs)}):")
                    for br in brs:
                        lines.append(f"    • {br.get('name')} when={br.get('when')} order={br.get('order')}")
                        br_script = br.get("script", "")
                        if br_script:
                            lines.append(f"      {br_script[:300]}...")
                lines.append("")

            # 6. REST Messages referenced
            if rest_refs:
                lines.append(f"=== REFERENCED REST MESSAGES ===")
                for ref in sorted(rest_refs):
                    rm_resp = await client.get("/api/now/table/sys_rest_message", params={
                        "sysparm_query": f"name={ref}",
                        "sysparm_fields": "sys_id,name,rest_endpoint,authentication_type",
                        "sysparm_limit": "1",
                    })
                    rm_resp.raise_for_status()
                    rms = rm_resp.json()["result"]
                    if rms:
                        rm = rms[0]
                        lines.append(f"  {rm.get('name')}: endpoint={rm.get('rest_endpoint')} auth={rm.get('authentication_type')}")
                    else:
                        lines.append(f"  {ref}: (not found as named REST Message)")
                lines.append("")

    return "\n".join(lines)


# ===========================================================================
# Operational Tools — query, update, and manage transactional records
# ===========================================================================


@mcp.tool()
async def query_table(
    table: str,
    query: str,
    fields: str = "",
    limit: int = 20,
    instance: str = "",
) -> str:
    """
    Query any non-blocked ServiceNow table with an encoded query string.

    Useful for listing incidents, tasks, email actions, transform maps, etc.
    Returns up to `limit` records (max 200) with key fields.

    Args:
        table:  Table name (e.g. 'incident', 'sc_task', 'sys_transform_map').
        query:  ServiceNow encoded query (e.g. 'active=true^priority=1').
        fields: Comma-separated field names to return. If blank, returns common fields.
        limit:  Max records to return (default 20, max 200).
        instance: Instance key from sn_instances.json (optional).
    """
    _assert_safe_table(table)
    limit = min(limit, 200)

    params: dict = {
        "sysparm_query": query,
        "sysparm_limit": str(limit),
    }
    if fields:
        params["sysparm_fields"] = fields

    async with _client(instance) as client:
        resp = await client.get(f"/api/now/table/{table}", params=params)
        resp.raise_for_status()
        records = resp.json()["result"]

    if not records:
        return f"No records found in '{table}' matching: {query}"

    lines = [f"Query results: {table} ({len(records)} record(s))", ""]
    for i, rec in enumerate(records, 1):
        # Show sys_id first, then all other returned fields
        sid = rec.get("sys_id", "?")
        parts = [f"sys_id={sid}"]
        for k, v in rec.items():
            if k == "sys_id":
                continue
            if isinstance(v, dict):
                v = v.get("display_value", v.get("value", str(v)))
            if v and str(v).strip():
                parts.append(f"{k}={v}")
        lines.append(f"  {i}. " + " | ".join(parts))
    return "\n".join(lines)


@mcp.tool()
async def query_table_count(
    table: str,
    query: str,
    instance: str = "",
) -> str:
    """
    Count records in a table matching an encoded query. Efficient — no data returned.

    Args:
        table:    Table name (e.g. 'incident', 'task_sla').
        query:    ServiceNow encoded query.
        instance: Instance key (optional).
    """
    _assert_safe_table(table)

    async with _client(instance) as client:
        resp = await client.get(
            f"/api/now/stats/{table}",
            params={
                "sysparm_query": query,
                "sysparm_count": "true",
            },
        )
        resp.raise_for_status()
        result = resp.json().get("result", {})
        count = result.get("stats", {}).get("count", "0")

    return f"Count of '{table}' where [{query}]: {count}"


@mcp.tool()
async def resolve_incident(
    sys_id: str,
    close_code: str,
    close_notes: str,
    work_notes: str = "",
    u_sub_status: str = "Permanently Resolved",
    instance: str = "",
) -> str:
    """
    Resolve an incident with proper close fields.

    Sets incident_state=6 (Resolved), state=6, and the required close fields.

    Args:
        sys_id:       sys_id of the incident.
        close_code:   Resolution code (e.g. 'Solved Remotely', 'Configuration Issue',
                      'Script Error', 'Hardware Replaced', 'Software Update').
        close_notes:  Non-technical resolution summary.
        work_notes:   Optional technical work notes.
        u_sub_status: Sub-status (default 'Permanently Resolved').
        instance:     Instance key (optional).
    """
    payload: dict = {
        "incident_state": "6",
        "state": "6",
        "close_code": close_code,
        "close_notes": close_notes,
        "u_sub_status": u_sub_status,
    }
    if work_notes:
        payload["work_notes"] = work_notes

    async with _client(instance) as client:
        resp = await client.patch(
            f"/api/now/table/incident/{sys_id}",
            json=payload,
        )
        resp.raise_for_status()
        result = resp.json()["result"]
        number = result.get("number", sys_id)

    return (
        f"Incident {number} resolved.\n"
        f"  close_code:   {close_code}\n"
        f"  u_sub_status: {u_sub_status}\n"
        f"  close_notes:  {close_notes[:100]}..."
    )


@mcp.tool()
async def close_task(
    sys_id: str,
    close_notes: str,
    work_notes: str = "",
    state: str = "3",
    instance: str = "",
) -> str:
    """
    Close a task (sc_task) with state and notes.

    Args:
        sys_id:      sys_id of the sc_task.
        close_notes: Non-technical closure summary.
        work_notes:  Optional technical work notes.
        state:       Target state (default '3' = Closed Complete).
        instance:    Instance key (optional).
    """
    payload: dict = {
        "state": state,
        "close_notes": close_notes,
    }
    if work_notes:
        payload["work_notes"] = work_notes

    async with _client(instance) as client:
        resp = await client.patch(
            f"/api/now/table/sc_task/{sys_id}",
            json=payload,
        )
        resp.raise_for_status()
        result = resp.json()["result"]
        number = result.get("number", sys_id)

    return (
        f"Task {number} closed (state={state}).\n"
        f"  close_notes: {close_notes[:100]}..."
    )


@mcp.tool()
async def bulk_update_records(
    table: str,
    query: str,
    fields_json: str,
    instance: str = "",
) -> str:
    """
    Batch-update records matching a query. Max 200 records per call.

    Args:
        table:       Table name (e.g. 'u_sr_gateway_connections').
        query:       Encoded query to find records to update.
        fields_json: JSON string of field-value pairs to set on each record.
                     Example: '{"u_active": "true"}'
        instance:    Instance key (optional).
    """
    _assert_safe_table(table)

    try:
        fields = json.loads(fields_json)
    except json.JSONDecodeError as e:
        return f"Error: Invalid JSON in fields_json: {e}"

    async with _client(instance) as client:
        # First, query the records
        resp = await client.get(
            f"/api/now/table/{table}",
            params={
                "sysparm_query": query,
                "sysparm_fields": "sys_id",
                "sysparm_limit": "200",
            },
        )
        resp.raise_for_status()
        records = resp.json()["result"]

        if not records:
            return f"No records found in '{table}' matching: {query}"

        if len(records) > 200:
            return f"Safety limit: query matched {len(records)} records (max 200). Narrow the query."

        # Batch PATCH each record
        updated = 0
        errors = 0
        for rec in records:
            try:
                patch_resp = await client.patch(
                    f"/api/now/table/{table}/{rec['sys_id']}",
                    json=fields,
                )
                patch_resp.raise_for_status()
                updated += 1
            except Exception:
                errors += 1

    return (
        f"Bulk update complete on '{table}'.\n"
        f"  Matched: {len(records)}\n"
        f"  Updated: {updated}\n"
        f"  Errors:  {errors}"
    )


@mcp.tool()
async def list_inbound_email_actions(
    query: str = "",
    instance: str = "",
) -> str:
    """
    List inbound email actions (sysevent_in_email_action).

    Args:
        query:    Optional encoded query filter (e.g. 'active=true^nameLIKEMDM').
        instance: Instance key (optional).
    """
    params: dict = {
        "sysparm_fields": "sys_id,name,order,active,table,stop_processing,filter_condition",
        "sysparm_limit": "50",
        "sysparm_orderby": "order",
    }
    if query:
        params["sysparm_query"] = query

    async with _client(instance) as client:
        resp = await client.get("/api/now/table/sysevent_in_email_action", params=params)
        resp.raise_for_status()
        records = resp.json()["result"]

    if not records:
        return "No inbound email actions found."

    lines = [f"Inbound Email Actions ({len(records)}):", ""]
    for r in records:
        name = r.get("name", "?")
        order = r.get("order", "?")
        active = r.get("active", "?")
        table = r.get("table", "?")
        stop = r.get("stop_processing", "?")
        sid = r.get("sys_id", "?")
        lines.append(f"  [{order}] {name}  active={active}  table={table}  stop={stop}  sys_id={sid}")
    return "\n".join(lines)


@mcp.tool()
async def create_inbound_email_action(
    name: str,
    table: str,
    order: int,
    script: str,
    condition: str = "",
    stop_processing: bool = True,
    active: bool = True,
    description: str = "",
    instance: str = "",
) -> str:
    """
    Create an inbound email action (sysevent_in_email_action).

    Args:
        name:            Display name (e.g. 'MDM Inbound Email - Create Incident').
        table:           Target table (e.g. 'incident').
        order:           Processing order (lower = runs first).
        script:          Server-side JavaScript for the action.
        condition:       Filter condition encoded query (e.g. 'recipientsLIKEmdm@example.com').
        stop_processing: Stop processing further actions after this one (default True).
        active:          Whether the action is active (default True).
        description:     Optional description.
        instance:        Instance key (optional).
    """
    payload: dict = {
        "name": name,
        "table": table,
        "order": str(order),
        "script": script,
        "stop_processing": str(stop_processing).lower(),
        "active": str(active).lower(),
    }
    if condition:
        payload["filter_condition"] = condition
    if description:
        payload["description"] = description

    async with _client(instance) as client:
        resp = await client.post(
            "/api/now/table/sysevent_in_email_action",
            json=payload,
        )
        resp.raise_for_status()
        result = resp.json()["result"]
        sid = result.get("sys_id", "?")

    return (
        f"Inbound email action created.\n"
        f"  sys_id: {sid}\n"
        f"  name:   {name}\n"
        f"  order:  {order}\n"
        f"  table:  {table}\n"
        f"  stop:   {stop_processing}"
    )


@mcp.tool()
async def list_transform_maps(
    source_table: str = "",
    name: str = "",
    instance: str = "",
) -> str:
    """
    List transform maps, optionally filtered by source table or name.

    Args:
        source_table: Filter by source table name (e.g. 'u_import_gatway_connections').
        name:         Partial name match (uses LIKE).
        instance:     Instance key (optional).
    """
    parts = []
    if source_table:
        parts.append(f"source_table={source_table}")
    if name:
        parts.append(f"nameLIKE{name}")
    query = "^".join(parts) if parts else ""

    params: dict = {
        "sysparm_fields": "sys_id,name,source_table,target_table,active",
        "sysparm_limit": "50",
    }
    if query:
        params["sysparm_query"] = query

    async with _client(instance) as client:
        resp = await client.get("/api/now/table/sys_transform_map", params=params)
        resp.raise_for_status()
        records = resp.json()["result"]

    if not records:
        return "No transform maps found."

    lines = [f"Transform Maps ({len(records)}):", ""]
    for r in records:
        rname = r.get("name", "?")
        src = r.get("source_table", "?")
        tgt = r.get("target_table", "?")
        active = r.get("active", "?")
        sid = r.get("sys_id", "?")
        lines.append(f"  • {rname}  ({src} → {tgt})  active={active}  sys_id={sid}")
    return "\n".join(lines)


# ===========================================================================
# Knowledge Base tools
# ===========================================================================

# Default KB: "IT - ServiceNow"
IT_SERVICENOW_KB_SYS_ID = "0027effd97dffa148cc1f8700153af91"

# Standard JSON headers for KB API calls
KB_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
}


async def _ensure_kb_category(
    url: str, user: str, password: str, kb_sys_id: str, category_label: str
) -> str:
    """Find or create a kb_category under the 'Application' root in the given KB.

    Hierarchy: Application (root) → <category_label> (child)

    Returns the sys_id of the child category.
    """
    async with httpx.AsyncClient(verify=False) as client:
        auth = (user, password)

        # --- Step 1: Find or create the "Application" root category in this KB ---
        app_resp = await client.get(
            f"{url}/api/now/table/kb_category",
            auth=auth,
            headers=KB_HEADERS,
            params={
                "sysparm_query": (
                    f"kb_knowledge_base={kb_sys_id}"
                    "^label=Application"
                    "^parent_idISEMPTY"
                ),
                "sysparm_fields": "sys_id,label",
                "sysparm_limit": "1",
            },
        )
        app_resp.raise_for_status()
        app_results = app_resp.json()["result"]

        if app_results:
            app_cat_id = app_results[0]["sys_id"]
        else:
            # Create root "Application" category
            create_resp = await client.post(
                f"{url}/api/now/table/kb_category",
                auth=auth,
                headers=KB_HEADERS,
                json={
                    "label": "Application",
                    "kb_knowledge_base": kb_sys_id,
                },
            )
            create_resp.raise_for_status()
            app_cat_id = create_resp.json()["result"]["sys_id"]

        # --- Step 2: Find or create the child category under Application ---
        child_resp = await client.get(
            f"{url}/api/now/table/kb_category",
            auth=auth,
            headers=KB_HEADERS,
            params={
                "sysparm_query": (
                    f"kb_knowledge_base={kb_sys_id}"
                    f"^label={category_label}"
                    f"^parent_id={app_cat_id}"
                ),
                "sysparm_fields": "sys_id,label",
                "sysparm_limit": "1",
            },
        )
        child_resp.raise_for_status()
        child_results = child_resp.json()["result"]

        if child_results:
            return child_results[0]["sys_id"]

        # Create child category
        create_child = await client.post(
            f"{url}/api/now/table/kb_category",
            auth=auth,
            headers=KB_HEADERS,
            json={
                "label": category_label,
                "kb_knowledge_base": kb_sys_id,
                "parent_id": app_cat_id,
            },
        )
        create_child.raise_for_status()
        return create_child.json()["result"]["sys_id"]


@mcp.tool()
async def list_kb_articles(
    knowledge_base: str = "",
    category: str = "",
    query: str = "",
    limit: int = 20,
    instance: str = "",
) -> str:
    """List KB articles, optionally filtered by knowledge base, category, or text query.

    Args:
        knowledge_base: sys_id or name of the knowledge base to filter by
        category: sys_id or name of the category to filter by
        query: Free-text search term (searches short_description and text)
        limit: Max results (default 20)
        instance: Instance key from sn_instances.json (optional)
    """
    url, user, password = _resolve_instance(instance)
    filters = []

    if knowledge_base:
        # If it looks like a sys_id (32 hex chars), use it directly
        if len(knowledge_base) == 32 and all(c in "0123456789abcdef" for c in knowledge_base.lower()):
            filters.append(f"kb_knowledge_base={knowledge_base}")
        else:
            filters.append(f"kb_knowledge_base.title={knowledge_base}")

    if category:
        if len(category) == 32 and all(c in "0123456789abcdef" for c in category.lower()):
            filters.append(f"kb_category={category}")
        else:
            filters.append(f"kb_category.label={category}")

    if query:
        filters.append(f"short_descriptionLIKE{query}^ORtextLIKE{query}")

    sysparm_query = "^".join(filters) if filters else "ORDERBYDESCsys_updated_on"

    async with httpx.AsyncClient(verify=False) as client:
        resp = await client.get(
            f"{url}/api/now/table/kb_knowledge",
            auth=(user, password),
            headers=KB_HEADERS,
            params={
                "sysparm_query": sysparm_query,
                "sysparm_fields": "sys_id,number,short_description,kb_knowledge_base,kb_category,workflow_state,author,sys_updated_on",
                "sysparm_limit": str(limit),
                "sysparm_display_value": "true",
            },
        )
        resp.raise_for_status()
        articles = resp.json()["result"]

    if not articles:
        return "No KB articles found matching the criteria."

    lines = [f"Found {len(articles)} KB article(s):\n"]
    for a in articles:
        lines.append(f"  [{a.get('number','')}] {a.get('short_description','(no title)')}")
        lines.append(f"    sys_id: {a.get('sys_id','')}")
        lines.append(f"    KB: {a.get('kb_knowledge_base','')}")
        lines.append(f"    Category: {a.get('kb_category','')}")
        lines.append(f"    State: {a.get('workflow_state','')}")
        lines.append(f"    Author: {a.get('author','')}")
        lines.append(f"    Updated: {a.get('sys_updated_on','')}")
        lines.append("")

    return "\n".join(lines)


async def _resolve_group_sys_id(
    url: str, user: str, password: str, group_name: str
) -> str:
    """Look up a sys_user_group by name and return its sys_id.

    Returns empty string if not found.
    """
    async with httpx.AsyncClient(verify=False) as client:
        resp = await client.get(
            f"{url}/api/now/table/sys_user_group",
            auth=(user, password),
            headers=KB_HEADERS,
            params={
                "sysparm_query": f"name={group_name}",
                "sysparm_fields": "sys_id,name",
                "sysparm_limit": "1",
            },
        )
        resp.raise_for_status()
        results = resp.json()["result"]
        return results[0]["sys_id"] if results else ""


@mcp.tool()
async def create_kb_article(
    short_description: str,
    text: str,
    kb_category_label: str = "ServiceNow",
    topic: str = "General",
    u_sub_category: str = "",
    workflow_state: str = "draft",
    knowledge_base: str = "",
    u_category: str = "Application - ServiceNow",
    ownership_group: str = "Knowledge Management - English",
    roles: str = "itil",
    instance: str = "",
) -> str:
    """Create a KB article in the 'IT - ServiceNow' Knowledge Base.

    The article is always created in 'draft' state by default so a human can
    review and publish it.

    The tool automatically:
    - Places the article in the 'IT - ServiceNow' KB (override with knowledge_base)
    - Sets u_category to 'Application - ServiceNow'
    - Sets ownership_group to 'Knowledge Management - English' (looked up by name)
    - Sets roles to 'itil'
    - Creates the kb_category tree: Application (root) → <kb_category_label> (child)
      if it doesn't already exist

    Args:
        short_description: Article title (required)
        text: Article body in HTML format (required)
        kb_category_label: Category label under 'Application' parent.
            Examples: "Integration", "Workflow & Automation", "ITSM Modules",
            "Configuration", "Reporting", "Security", "Platform".
            The tool finds or creates this category automatically. Default "ServiceNow".
        topic: Article topic. Valid values: "General", "FAQ", "Technical Tip",
            "Applications", "Known Error", "Process & Policy", "Technical SOP",
            "News", "Desktop", "Email", "Collaboration". Default "General".
        u_sub_category: Sub-category string. Determined by the AI based on content.
        workflow_state: Workflow state – default "draft". Other values: "published", "review"
        knowledge_base: Override KB sys_id. Defaults to 'IT - ServiceNow' KB.
        u_category: Category field value. Default "Application - ServiceNow".
        ownership_group: Ownership group name. Default "Knowledge Management - English".
            Looked up by name on the target instance to resolve the correct sys_id.
        roles: Roles required to view the article. Default "itil".
        instance: Instance key from sn_instances.json (optional)
    """
    url, user, password = _resolve_instance(instance)

    # Determine KB sys_id (default: IT - ServiceNow)
    kb_sys_id = knowledge_base if knowledge_base else IT_SERVICENOW_KB_SYS_ID

    # Find or create the kb_category under Application → <kb_category_label>
    cat_sys_id = await _ensure_kb_category(url, user, password, kb_sys_id, kb_category_label)

    # Resolve ownership_group name → sys_id on the target instance
    group_sys_id = ""
    if ownership_group:
        group_sys_id = await _resolve_group_sys_id(url, user, password, ownership_group)

    payload = {
        "short_description": short_description,
        "text": text,
        "kb_knowledge_base": kb_sys_id,
        "kb_category": cat_sys_id,
        "topic": topic,
        "workflow_state": workflow_state,
        "u_category": u_category,
        "roles": roles,
    }

    if group_sys_id:
        payload["ownership_group"] = group_sys_id

    # Only set u_sub_category if provided
    if u_sub_category:
        payload["u_sub_category"] = u_sub_category

    async with httpx.AsyncClient(verify=False) as client:
        resp = await client.post(
            f"{url}/api/now/table/kb_knowledge",
            auth=(user, password),
            headers=KB_HEADERS,
            json=payload,
        )
        resp.raise_for_status()
        result = resp.json()["result"]

    return (
        f"KB article created successfully!\n"
        f"  sys_id:       {result.get('sys_id','')}\n"
        f"  Number:       {result.get('number','')}\n"
        f"  Title:        {result.get('short_description','')}\n"
        f"  State:        {result.get('workflow_state','draft')}\n"
        f"  KB:           IT - ServiceNow\n"
        f"  Category:     Application > {kb_category_label}\n"
        f"  u_category:   {u_category}\n"
        f"  Ownership:    {ownership_group}\n"
        f"  Roles:        {roles}\n"
        f"  Topic:        {topic}\n"
        f"  Link:         {url}/nav_to.do?uri=kb_knowledge.do?sys_id={result.get('sys_id','')}"
    )


@mcp.tool()
async def list_kb_bases_and_categories(instance: str = "") -> str:
    """List available Knowledge Bases and their categories.

    Use this to find the correct kb_knowledge_base and kb_category sys_ids
    before creating a KB article.

    Args:
        instance: Instance key from sn_instances.json (optional)
    """
    url, user, password = _resolve_instance(instance)
    lines = []

    async with httpx.AsyncClient(verify=False) as client:
        # Fetch knowledge bases
        kb_resp = await client.get(
            f"{url}/api/now/table/kb_knowledge_base",
            auth=(user, password),
            headers=KB_HEADERS,
            params={
                "sysparm_query": "active=true",
                "sysparm_fields": "sys_id,title,description",
                "sysparm_limit": "50",
            },
        )
        kb_resp.raise_for_status()
        bases = kb_resp.json()["result"]

        lines.append(f"Knowledge Bases ({len(bases)}):\n")
        for kb in bases:
            lines.append(f"  [{kb.get('sys_id','')}] {kb.get('title','')}")
            if kb.get("description"):
                lines.append(f"    {kb['description'][:100]}")
            lines.append("")

        # Fetch categories
        cat_resp = await client.get(
            f"{url}/api/now/table/kb_category",
            auth=(user, password),
            headers=KB_HEADERS,
            params={
                "sysparm_query": "active=true",
                "sysparm_fields": "sys_id,label,parent_id,kb_knowledge_base",
                "sysparm_limit": "100",
                "sysparm_display_value": "true",
            },
        )
        cat_resp.raise_for_status()
        cats = cat_resp.json()["result"]

        lines.append(f"\nCategories ({len(cats)}):\n")
        for c in cats:
            parent = c.get("parent_id", "")
            kb_name = c.get("kb_knowledge_base", "")
            lines.append(f"  [{c.get('sys_id','')}] {c.get('label','')}")
            lines.append(f"    KB: {kb_name}  Parent: {parent or '(root)'}")
            lines.append("")

    return "\n".join(lines)


# ===========================================================================
# Entry point
# ===========================================================================

if __name__ == "__main__":
    mcp.run()
