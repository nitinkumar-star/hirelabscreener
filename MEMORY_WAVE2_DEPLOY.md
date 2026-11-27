# Memory — Wave 2 (Claude connector tools) deploy notes (Nov 2026)

Goal: Claude can read the ATS memory before answering, and save what it learned.
Wave 1 (the `/api/memory` API) must be live on the ATS first.

## What changed

| File | Change |
|---|---|
| `mcp_server.py` | 4 new tools, a new `MCP_ALLOW_MEMORY_WRITES` switch, one memory line in the connector instructions, and `version` + `memory_writes_enabled` in `/health`. Version 1.1.0 → 1.2.0. |
| `modules/memory.py` | Wave 1 fix: a dedupe update now changes only the fields that were sent, the same way PUT does. Before, unsent fields were reset: `importance` dropped to normal and `client_name` was cleared. The Wave 2 tests found this. |
| `test_memory.py` | +1 check for the fix above |
| `test_mcp_memory.py` (new) | 11 tests: the tools against the real ATS routes, plus both switches |
| `mcp_memory_check.py` (new) | manual check against the live ATS |

The 15 existing tools are unchanged. Their code, descriptions and the
`MCP_ALLOW_WRITES` behaviour are the same as before.

### Tools

| Tool | Type | ATS call |
|---|---|---|
| `get_memory_context` | read | `GET /api/memory/context` (one of `client` / `mandate_id` / `candidate_id`). The description says "Call this FIRST …". |
| `recall_memory` | read | `GET /api/memory` (`query`, `scope`, `entity_name`, `entity_id`, `limit` 20, `include_expired`) |
| `save_memory` | memory write | without `note_id`: `POST /api/memory` with `dedupe: true` and `source: "claude"`. With `note_id`: `PUT` (only the given fields change). `valid_until: "none"` removes an expiry date. |
| `archive_memory` | memory write, `destructiveHint` | `POST /api/memory/<id>/archive` (soft delete) |

When the ATS refuses a request, Claude now sees the ATS's reason, e.g. "a
candidate note needs entity_name or entity_id". Before, it only saw "ATS
returned 400". If Wave 1 is not deployed, the tools reply "Memory is not on this
ATS yet … Deploy the ATS Memory Wave 1 update first."

### Switches (Render → MCP service → Environment)

| `MCP_ALLOW_WRITES` | `MCP_ALLOW_MEMORY_WRITES` | Result |
|---|---|---|
| off | not set | memory read only, no other writes (default) |
| on | not set | memory writes on, other writes on |
| off | `true` | **memory writes on, other writes stay off** (recommended if you keep writes off) |
| on | `false` | memory read only, other writes on |

The memory tools are always available for reading.

## Verification done

- `python -m py_compile` passed for `mcp_server.py`, `modules/memory.py` and `mcp_memory_check.py`.
- `pytest` (full suite): 487 passed, 0 skipped. `test_mcp_server.py` used to be skipped on this machine only because `httpx` was not installed. It now runs and passes.

## Deploy — stage 1: ATS (the Wave 1 fix)

1. Merge and push the branch. The ATS service on Render redeploys, which brings in the `modules/memory.py` fix.
2. Check: `/api/memory` still answers `{"ok": true, ...}` when you are logged in.

## Deploy — stage 2: MCP service

1. If the MCP service is linked to the same GitHub repo, it redeploys on the same push. If it is not, use Manual Deploy → "Deploy latest commit".
2. Decide on the switch, using the table above. To allow only memory writes, add the env var `MCP_ALLOW_MEMORY_WRITES=true` and save; Render restarts the service.
3. Open `https://<mcp-service>.onrender.com/health`. It should show `"version": "1.2.0"` and `"memory_writes_enabled": true` (or false, if that is what you chose).
4. Run the live check from Render → MCP service → **Shell**:
   ```bash
   python mcp_memory_check.py
   ```
   The expected result is `8/8 passed`. It creates one note titled "Connector check …" and archives it.
5. In Claude.ai: Settings → Connectors → HireLab Screener. Disconnect and reconnect it, or start a new chat, so Claude loads the new tool list. Then try:
   - "L&K ke baare mein memory mein kya hai?" → Claude should call `get_memory_context`.
   - "Yaad rakho: Nishtha Bajaj (L&K) DM roles ke liye 10-yr profiles reject karti hain" → Claude should call `save_memory` and tell you in one line what it saved.

## Rollback

- Fast, no redeploy: set `MCP_ALLOW_MEMORY_WRITES=false`. Claude can then read memory but not write it.
- Full: redeploy the previous commit on the MCP service. The 15 old tools return exactly as before. Notes Claude saved stay in the ATS, and you can archive any of them from the API now or from the Memory page after Wave 3.
- The ATS fix in stage 1 can stay even if the MCP service is rolled back. To undo it as well, redeploy the Wave 1 commit on the ATS service.
