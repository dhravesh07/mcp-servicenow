---
description: Promote a ServiceNow Update Set through the CI/CD pipeline
argument-hint: [update-set-name-or-sys-id]
allowed-tools: Bash, Read, Glob
---

# Promote Update Set

You are promoting a ServiceNow Update Set through the CI/CD pipeline.

## Instructions

Follow this workflow to promote an Update Set from one instance to the next:

### 1. Determine the source instance

If the user provided a source instance, use it. Otherwise:
- Call `list_instances()` to show available instances and the pipeline order.
- Ask the user which instance to promote FROM.

### 2. Find the Update Set

If the user provided a sys_id, use it directly. If they provided a name or partial name:
- Call `list_update_sets(state='complete')` on the source instance to find completed Update Sets.
- If the argument matches a name, use that. Otherwise show the list and ask the user to pick one.

### 3. Validate before promotion

- Call `validate_update_set(sys_id)` on the source to show what's in the Update Set.
- Confirm with the user that these are the right changes to promote.

### 4. Determine the target instance

- The default target is the next instance in the pipeline (e.g. sandbox -> dev).
- Show the user the source -> target path and ask for confirmation.
- Allow the user to override the target if needed.

### 5. Promote

- Call `promote_update_set(update_set_sys_id, source, target)`.
- Do NOT use `auto_commit=True` unless the user explicitly asks for it.
- Show the full promotion output including preview results.

### 6. Post-promotion

If the preview was clean:
- Ask the user if they want to commit the Update Set on the target instance.
- If yes, call `promote_update_set(...)` again with `auto_commit=True`, or guide them to commit in the target instance UI.

If the preview had problems:
- Show the conflicts clearly.
- Advise the user to resolve them in the target instance before committing.

### Safety rules

- NEVER auto-commit to prod without explicit user confirmation.
- Always show preview results before committing.
- Warn if the promotion skips pipeline stages (e.g. sandbox -> prod).
