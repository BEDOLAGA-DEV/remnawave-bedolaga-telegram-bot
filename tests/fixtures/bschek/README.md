# Фикстуры bschekbot API v1

Записаны с живого API 2026-09-05 (тариф gold, X-API-Version 1.1) и санитизированы:
домены → `*.example`, IP → TEST-NET, uuid конфигов → нулевые, публичные ключи → `PUBKEY`,
секрет вебхука → `REDACTED`, ключ идемпотентности → `IDEMPOTENCY-KEY`. Формы ответов
сохранены байт в байт. Формат файла:

```json
{ "name": "...", "status": 200, "elapsed_sec": 9.5, "headers": {...},
  "request": {...} | null, "idempotency_key": "IDEMPOTENCY-KEY" | null, "body": {...} }
```

Именование: `op_*` — GET /operators, `pv_*` — /probe/preview, `sv_*` — /scans/preview,
`p*` — POST /probe, `v*` — /vless, `s*` — /scans, `auth_*`/`method_405` — ошибки доступа.
У сканов с находками `body.result.results` обрезан до 5 элементов
(`_results_truncated_from` хранит исходное число). Полный разбор поведения — в
`docs/superpowers/specs/2026-09-05-reachability-bschek-design.md`, приложение А.
