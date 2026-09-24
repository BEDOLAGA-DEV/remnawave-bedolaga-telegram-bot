# Ответы API DPI//CHECKER

Сняты с живого API `https://dpichecker.st/api/v1` 2026-09-24 (ключ партнёра). Очищены: нет ключа API,
ссылки ключей заменены на `<схема>://masked-N`, MTProto — на `proxy.example`, адреса серверов и выходов точек —
на `203.0.113.N`, соседняя подсеть — на `198.51.100.0/24`, адрес приёмника вебхуков — на `bot.example`,
секрет подписи — `whsec_test`. Формат файла: `{"status": <HTTP>, "body": <тело>}`.

| Файл | Что показывает |
|---|---|
| `profile`, `quota`, `tariffs` | профиль/баланс, квоты (Соседи 10/сутки, мониторы 50, 120 запросов/мин), цены |
| `pops`, `optimal_ru` | точки присутствия (урезано до 30), «Оптимальный выбор» РФ |
| `parse_ip`, `parse_vpn`, `parse_mtproto` | разбор вставки (CIDR превращается в адрес, мусор у VPN молча отбрасывается) |
| `estimate_vpn_ok` | цена: hy2-ключ стоит `vantages × resource_price` |
| `estimate_bad_pop`, `estimate_bad_loc`, `too_many_resources`, `ip_bad_pops`, `idempotency_conflict`, `check_404`, `noisy_private` | ошибки `{error, code, rejected?}` |
| `check_vpn` | VPN: 2 ключа × 5 точек + строки `is_direct` («из-за границы»); одна неудачная строка с `control_check` |
| `check_ip_server`, `check_ip_noserver` | IP с `probe_mode` server / noserver |
| `check_mtproto` | MTProto, неудача `tls_handshake_failed` |
| `check_cancelled`, `cancel_ok` | отмена в очереди — полный возврат |
| `checks_list`, `checks_noisy` | списки истории |
| `noisy_run`, `noisy_done` | Шумные соседи: запуск и итог `analysis` |
| `probe_run`, `probe_running`, `probe_done` | Зонд: запуск, этап `scan`, итог (суммы строками!) |
| `cheremsha`, `blacklist`, `ip_lookup` | бесплатные справки |
| `monitor_created`, `monitors_empty`, `monitor_runs`, `monitor_after_run`, `monitor_deleted`, `check_watcher_run` | монитор, его прогон (`source: watcher`) |
| `webhook_*` | тела вебхуков: `check.completed`, `check.cancelled`, `noisy.done`, `probe.done`, `monitor.run` (приходит в НАЧАЛЕ прогона) |
| `wh_secret` | ответ `/webhooks/secret` |
