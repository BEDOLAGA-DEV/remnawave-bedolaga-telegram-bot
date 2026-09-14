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
(`_results_truncated_from` хранит исходное число). Главные расхождения с текстовым контрактом: синхронный probe за Cloudflare обрывается 524 при
долгих проверках (результат достаётся повтором с тем же Idempotency-Key, пока идёт — 409
`request_in_progress`); preview не отдаёт `skipped_*`; неизвестный оператор в op_key → 503
`worker_unavailable`; `sni[]` бывает в двух формах (`host` либо `evidence.sni`); отмена VLESS даёт
`state: done` с `cancelled: true` в леге; отмена завершённого VLESS → 404, скана → 409 `not_running`.
