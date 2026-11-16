# Client compatibility notes (design §3.4, §8.6)

This page records how real MCP clients behave with the gateway's additions.
Automated tests cover the protocol side (`tests/compat/`); what a *model*
actually sees can only be measured on the real products, by hand.

## Unread-mail notice: which channel reaches the model

Since M6 every `tools/call` result -- errors included -- ends with the unread
notice when the caller has unread mail. It is carried three ways:

| Channel | Shape | Present when |
|---|---|---|
| `content` | last `TextContent`, starting with `[agent-nexus 邮箱]` | always |
| `_meta["agent-nexus/mail"]` | `{"unread", "me", "me_display", "latest": [...]}` | `mailbox.notice.meta` (default `true`) |
| `structuredContent["_mail"]` | same object as `_meta` | the result has a structured payload whose `outputSchema` allows extra keys; `mailbox.notice.structured`: `auto` (default) / `always` / `never` |

### Procedure (M6 acceptance, to repeat after client updates)

1. Connect the client to the gateway (ChatGPT connector / Claude.ai custom connector).
2. `use_device(device_id=…, session_name="probe", kind=…)`, keep the `workspace_id`.
3. `mail_send(workspace_id=…, to="probe", body="probe")` -- mail to itself.
4. Call `toolsearch(query="exec", workspace_id=…)` and ask the model to repeat,
   verbatim, everything after the tool's own result.
5. Record below which channels the model could repeat. Then `mail_read` and
   repeat step 4: the notice must be gone.

### Results

| Client | Date | content | structuredContent | `_meta` | Notes |
|---|---|---|---|---|---|
| ChatGPT (connector) | not yet measured | | | | needs a real ChatGPT account and the deployed gateway |
| Claude.ai (custom connector) | not yet measured | | | | needs a real Claude.ai account and the deployed gateway |

Until measured, `notice.structured` stays `auto`. If a client turns out to show
the model only `structuredContent`, `auto` already covers every gateway-native
tool (they return open objects); device tools with strict output schemas then
carry the notice in text and `_meta` only.

## Cached tool schemas

New parameters are optional only (rule-based snapshot `tests/compat/tool_schemas.json`;
mailbox tools in `schemas/mail-tools.v1.json`). A client that keeps an old
tool list does not pass `workspace_id` to discovery tools and so may miss
notices; the gateway then falls back to the stateful session and to the one
address its OAuth client used recently, and counts the misses
(`notice_missed`, `notice_fallback_used` in `mail_who` and `/healthz?detail=1`).
