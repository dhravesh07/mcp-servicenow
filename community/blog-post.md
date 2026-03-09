# RefFieldFetcher — One Script Include to Replace All Your GlideAjax Endpoints

## The Problem Every ServiceNow Team Has

If you've been on ServiceNow long enough, you've seen this pattern repeat across every project:

1. Developer needs a reference field's data on the client side
2. They use `g_form.getReference()` or `getXMLWait()` — both **synchronous**, both freeze the form
3. Someone tells them "use GlideAjax instead"
4. They create a **brand-new Script Include** just to return one or two fields
5. Multiply by 50 developers over 3 years → dozens of nearly-identical Script Includes

The result? Slow forms, unmaintainable code, inconsistent security, and a Script Include table full of one-off endpoints.

## The Solution: RefFieldFetcher

**RefFieldFetcher** is a single, client-callable Script Include that replaces the need for individual GlideAjax endpoints when you need to read data from the server.

Pass it a table, sys_id(s), and a list of fields — it returns structured JSON. That's it. One Script Include, unlimited use cases.

### What makes it different from a simple "generic GlideAjax"?

- **Dot-walk support** — fetch `caller_id.department.name` in one call, no extra hops
- **Three return modes** — `value`, `display`, or `both` (value + display together)
- **Encoded query support** — not just sys_id lookups, but dynamic queries too
- **7-layer security model** — table allowlist, field denylist, encrypted-type blocking, ACL enforcement, dot-depth limits, encoded query validation, admin-only ACL bypass
- **Configurable via System Properties** — admins control behavior without touching code
- **Server-side caching** — user-scoped cache with configurable TTL
- **Server-side direct call** — `getFieldsDirect()` lets other Script Includes reuse the same logic

---

## Before & After

### Before (synchronous, freezes the form):

```javascript
// DON'T DO THIS — blocks the entire browser thread
function onChange(control, oldValue, newValue, isLoading) {
    if (isLoading || !newValue) return;
    var rec = g_form.getReference('caller_id'); // SYNCHRONOUS!
    g_form.setValue('location', rec.location);
}
```

### After (async, non-blocking):

```javascript
function onChange(control, oldValue, newValue, isLoading) {
    if (isLoading || !newValue) return;

    var ga = new GlideAjax('RefFieldFetcher');
    ga.addParam('sysparm_name', 'getFields');
    ga.addParam('sysparm_table', 'sys_user');
    ga.addParam('sysparm_sys_ids', newValue);
    ga.addParam('sysparm_fields', 'name,email,location.name');
    ga.addParam('sysparm_mode', 'display');
    ga.addParam('sysparm_limit', '1');
    ga.getXMLAnswer(function(answer) {
        var data = JSON.parse(answer);
        if (data.rows && data.rows.length) {
            g_form.setValue('location', data.rows[0]['location.name']);
        }
    });
}
```

**Same result. Zero form freeze. Zero new Script Includes.**

---

## Parameters

| Parameter | Required | Description |
|-----------|----------|-------------|
| `sysparm_name` | Yes | Always `'getFields'` |
| `sysparm_table` | Yes | Table to query (must be in allowlist) |
| `sysparm_sys_ids` | Yes* | Comma-separated sys_ids |
| `sysparm_query` | Yes* | Encoded query (alternative to sys_ids) |
| `sysparm_fields` | No | Comma-separated fields (supports dot-walks). Default: `name` |
| `sysparm_mode` | No | `value`, `display`, or `both`. Default: `value` |
| `sysparm_limit` | No | Max records. Capped by system property (default 50) |
| `sysparm_acl` | No | `true` (default) = GlideRecordSecure. `false` = admin-only bypass |

*One of `sysparm_sys_ids` or `sysparm_query` is required.

---

## Response Format

```json
{
  "meta": {
    "table": "sys_user",
    "count": 1,
    "mode": "both",
    "fields": ["name", "email", "department.name"],
    "acl": true
  },
  "rows": [
    {
      "sys_id": "abc123...",
      "name": { "value": "John Doe", "display": "John Doe" },
      "email": { "value": "john@example.com", "display": "john@example.com" },
      "department.name": { "value": "IT_SYS_ID", "display": "Information Technology" }
    }
  ]
}
```

Errors return structured JSON too:

```json
{ "error": "table_not_allowed", "table": "sys_user_role" }
{ "error": "field_forbidden", "reason": "denylist_name", "field": "password" }
{ "error": "acl_bypass_forbidden" }
```

---

## Security Model (7 Layers)

| Layer | What It Does |
|-------|-------------|
| **Table Allowlist** | Only tables in `ref_field_fetcher.allow_tables` can be queried |
| **ACL Enforcement** | Uses `GlideRecordSecure` by default — respects all table/field ACLs |
| **ACL Bypass Guard** | `sysparm_acl=false` only works when property allows AND user has `admin` or `security_admin` role |
| **Field Denylist (names)** | Blocks fields like `password`, `token`, `client_secret`, `private_key` |
| **Field Denylist (types)** | Blocks fields with dictionary type `password` or `glide_encrypted` |
| **Dot-Walk Depth Limit** | Max segments configurable (default: 4) |
| **Encoded Query Validation** | Every field in an encoded query is validated against deny rules before execution |

---

## System Properties (9)

All behavior is controlled via system properties — no code changes needed:

| Property | Default | Purpose |
|----------|---------|---------|
| `ref_field_fetcher.enabled` | `true` | Master kill switch |
| `ref_field_fetcher.allow_tables` | `{"sys_user":true, ...}` | JSON allowlist of queryable tables |
| `ref_field_fetcher.max_records` | `50` | Max records per call |
| `ref_field_fetcher.max_fields` | `30` | Max fields per request |
| `ref_field_fetcher.max_dot_depth` | `4` | Max dot-walk segments |
| `ref_field_fetcher.cache_ttl_seconds` | `30` | Cache lifetime (0 = disabled) |
| `ref_field_fetcher.deny_fields` | `{"password":true, ...}` | Blocked field names (JSON) |
| `ref_field_fetcher.deny_field_types` | `{"password":true}` | Blocked field types (JSON) |
| `ref_field_fetcher.allow_acl_bypass` | `false` | Whether admins can bypass ACLs |

---

## Real-World Use Cases

Here are patterns where RefFieldFetcher shines:

### 1. VIP Badge on Form Load
Dot-walk through a reference field to check VIP status and add decorations — one async call replaces what used to be a synchronous `getReference()`:

```javascript
ga.addParam('sysparm_fields', 'caller_id.vip,caller_id.u_platinum_vip');
// In callback: if (row['caller_id.vip'] == '1') → add star decoration
```

### 2. Auto-Fill from Reference Selection
When a user picks a reference field (e.g., an application), fetch related fields to auto-populate other form fields:

```javascript
ga.addParam('sysparm_table', 'cmdb_ci_business_app');
ga.addParam('sysparm_fields', 'number,used_for,department.name');
// In callback: setValue() the related fields
```

### 3. Dynamic Validation
Use encoded query mode to check if a record exists before allowing submission:

```javascript
ga.addParam('sysparm_query', 'serial_number=' + serialNum + '^active=true');
ga.addParam('sysparm_fields', 'sys_id');
// In callback: if (data.rows.length === 0) → show "not found" error
```

### 4. Server-Side Reuse
Other Script Includes can call it directly without GlideAjax overhead:

```javascript
var rff = new RefFieldFetcher();
var json = rff.getFieldsDirect('sys_user', sysId, 'name,email', 'both', 1, true, '');
var data = JSON.parse(json);
```

---

## Installation

1. **Import the Update Set** from ServiceNow Share (link below)
2. **Review the 9 system properties** — adjust `allow_tables` for your instance
3. **Start using it** in your client scripts — no further setup needed

The Update Set contains:
- 1 Script Include (`RefFieldFetcher`)
- 9 System Properties (`ref_field_fetcher.*`)

---

## Guidelines for Your Team

| # | Rule |
|---|------|
| 1 | **Never use `getXMLWait()`** — synchronous = blocks UI |
| 2 | **Never use `g_form.getReference()`** — synchronous in most contexts |
| 3 | **Never create a new GlideAjax Script Include** for simple field lookups — use RefFieldFetcher |
| 4 | **Request only the fields you need** — smaller payload = faster response |
| 5 | **Always use `sysparm_acl=true`** — unless you're admin with a valid reason |
| 6 | **Always handle errors** — check `data.error` before `data.rows` |
| 7 | **Use dot-walks instead of multiple calls** — one call with `a.b,a.c` beats two separate calls |

---

## Download

Get the Update Set from **ServiceNow Share**: [link]

---

*Built to solve a real problem across 50+ client scripts. If your instance has the same pattern of synchronous lookups and Script Include sprawl, give it a try.*
