# Mobile Support WebSocket v1

Backend endpoint: `/cabinet/ws/support/v1`

This endpoint is for mobile support clients only. The existing `/cabinet/ws` endpoint remains the legacy cabinet notification socket and still uses its existing query-token behavior. Mobile support clients must use this support socket for support ticket commands and support media transfer.

## Handshake

- `Authorization: Bearer <cabinet_access_token>` is required.
- Query-token authentication is rejected on this endpoint.
- `Sec-WebSocket-Protocol` must include `bedolaga.support.mobile.v1`; the server echoes that exact value.
- Optional `X-Telegram-Init-Data` is validated against the JWT user when present. A mismatch rejects the connection.
- Invalid or expired access tokens are rejected before command processing.

## Envelope

Client command:

```json
{"type":"command","command":"ticket.list","requestId":"string","sentAt":"2026-07-09T00:00:00Z","payload":{}}
```

Successful result:

```json
{"type":"command.result","command":"ticket.list","requestId":"string","ok":true,"receivedAt":"2026-07-09T00:00:00Z","payload":{}}
```

Failure result:

```json
{"type":"command.result","command":"ticket.list","requestId":"string","ok":false,"receivedAt":"2026-07-09T00:00:00Z","error":{"code":"VALIDATION_ERROR","message":"string","retryable":false,"resourceType":null,"resourceId":null,"details":{},"retryAfterMs":null,"backpressure":null}}
```

`retryAfterMs` is always an integer or `null`; it is never a string placeholder. `details` is always an object. `backpressure` is only populated for `BACKPRESSURE`.

## Ticket Commands

- `ticket.list`
- `ticket.detail`
- `ticket.reply`
- `ticket.status.update`
- `ticket.priority.update`
- `state.reconcile`
- `auth.reauthenticate`

WebSocket payloads use camelCase at the socket boundary and map to existing internal snake_case fields where needed.

Ticket snapshots include `assignedTo: object|null`. The current backend ticket model has no support assignment field, so the value is `null`. Connection readiness also declares assignment event nullability with `previousAssignedTo: null` and `assignedTo: null`.

`ticket.create` is intentionally out of scope for the current mobile admin support implementation. The connection readiness event reports it as unsupported so clients do not infer hidden REST fallback behavior.

## Media

Support media is websocket-only for mobile clients. No signed media URL is returned by the support socket.

Upload commands:

- `media.upload.begin`
- `media.upload.chunk`
- `media.upload.finish`
- `media.upload.cancel`

Download commands:

- `media.download.begin`
- `media.download.next`
- `media.download.cancel`

Uploads and downloads are scoped to the authenticated websocket session. Cross-connection resume is not supported. A returned `mediaId` is attachable to `ticket.reply` only after `media.upload.finish` succeeds.

`media.upload.begin` requires a `ticketId` for a ticket the caller can see; a missing or invisible ticket is rejected (`VALIDATION_ERROR`/`NOT_FOUND`).

Limits and protections:

- maximum file size: 10 MB
- maximum upload chunk: 512 KiB
- maximum command frame: 2 MiB (larger frames are rejected with `PAYLOAD_TOO_LARGE`)
- maximum concurrent in-flight transfers per session: 8 (further begins return `RATE_LIMITED`; expired/cancelled transfers are pruned first)
- base64 JSON frames
- SHA-256 finish validation
- existing unsafe MIME/extension blocks for HTML, SVG, XML, and JavaScript content
- no raw JWT, refresh token, Telegram initData, media bytes, raw filenames, or raw Telegram file ids are logged by this endpoint

## RBAC And Visibility

Commands re-check permissions at execution time.

- owner: own visible support tickets only
- support: tickets permitted by active RBAC ticket permissions
- admin: all support tickets
- invisible ticket detail returns `NOT_FOUND`
- owners cannot update status or priority
- media upload/download requires ticket/media visibility and the current transfer owner
- events are delivered only to connections that currently pass ticket visibility
