# ServiceNow Share Listing

## Title
RefFieldFetcher — Generic Async GlideAjax Endpoint for Client Scripts

## Short Description
A single, reusable, client-callable Script Include that eliminates the need for individual GlideAjax endpoints. Pass a table, sys_ids, and fields — get structured JSON back. Supports dot-walks, encoded queries, three return modes, ACL enforcement, field denylists, and property-driven configuration.

## Description

### Problem
Every ServiceNow team faces the same pattern: developers need server-side data on the client, so they create one-off GlideAjax Script Includes. Over time, this leads to dozens of nearly-identical endpoints, inconsistent security, and forms that use synchronous calls (getXMLWait, g_form.getReference) that freeze the browser.

### Solution
**RefFieldFetcher** is one Script Include that handles all standard "fetch fields from a record" use cases. It is:

- **Generic** — works with any table (controlled via allowlist property)
- **Async-only** — designed for getXMLAnswer/getXML, eliminating synchronous calls
- **Secure** — 7-layer security: table allowlist, field denylist (names + types), ACL enforcement, dot-depth limits, encoded query validation, admin-only ACL bypass
- **Configurable** — all behavior controlled via 9 system properties, no code changes needed
- **Dot-walk capable** — fetch `caller_id.department.name` in one call
- **Cached** — user-scoped server-side cache with configurable TTL
- **Server-side callable** — `getFieldsDirect()` for use from other Script Includes

### What's Included
- 1 Script Include (RefFieldFetcher, Global scope, client-callable)
- 9 System Properties (ref_field_fetcher.*)

### Compatibility
- Tested on Washington DC, Xanadu, Yokohama
- Works in Standard UI, Service Portal, Agent Workspace, and Mobile
- Global scope — accessible from all application scopes

## Tags
GlideAjax, Client Script, Performance, Script Include, Security, Async, Dot-Walk, Reference Field

## Category
Developer Tools
