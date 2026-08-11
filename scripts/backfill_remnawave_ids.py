#!/usr/bin/env python
"""One-shot CLI for the Remnawave 3.0.0 identity backfill.

Migration 0104 only ADDS the ``remnawave_id`` columns; it cannot fill them,
because resolving a panel account requires talking to the panel.  This script is
that step, and on an existing install it must run before the bot is trusted:
until it does, every pre-upgrade row has ``remnawave_id IS NULL`` and the grace
reader treats a NULL identity as a hard data fault.

Usage:
    python -m scripts.backfill_remnawave_ids            # dry run, writes nothing
    python -m scripts.backfill_remnawave_ids --apply    # persist

A dry run is the default on purpose: read the report, confirm ``unresolved`` and
``conflicts`` are what you expect, and only then re-run with --apply.  The apply
pass refuses to commit if any two bot rows claim the same panel account.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import structlog

from app.config import settings
from app.database.database import AsyncSessionLocal
from app.services.remnawave_identity_backfill import backfill_remnawave_ids
from app.services.system_settings_service import bot_configuration_service


logger = structlog.get_logger(__name__)


def _write_audit(report, *, committed: bool) -> str | None:
    # settings.LOG_DIR, а не голое окружение: в контейнере смонтирован ./logs, а
    # запасное '.' — это /app, который НЕ том, и файл исчезал вместе с
    # `docker compose run --rm` ровно в тот момент, когда он и нужен.
    directory = Path(os.environ.get('BACKFILL_AUDIT_DIR') or settings.LOG_DIR or 'logs')
    # Имя отражает ЧТО это за прогон, а не флаг committed: холостой прогон
    # ничего не записывает по определению, и называть его файл `conflicts`
    # (как было) — врать оператору, у которого конфликтов ноль.
    if report.conflicts:
        suffix = 'conflicts'
    elif report.dry_run:
        suffix = 'dryrun'
    else:
        suffix = 'apply'
    # Уникальное имя: фиксированное затирало след предыдущего прогона — тот
    # самый, с которым инструкция велит сверяться.
    stamp = datetime.now(UTC).strftime('%Y%m%d-%H%M%S')
    path = directory / f'remnawave_backfill_{suffix}_{stamp}.json'
    try:
        directory.mkdir(parents=True, exist_ok=True)
        payload = report.as_audit()
        # Явно, а не по признаку dry_run: после отката по конфликтам ничего не
        # записано, хотя `applied` заполнен — инструкция обещает «не записано
        # ничего», и файл не должен ей противоречить.
        payload['summary']['committed'] = committed
        if not committed:
            payload['applied_but_rolled_back'] = payload.pop('applied')
        with path.open('w', encoding='utf-8') as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
    except OSError as error:
        # Отчёт — не причина ронять прогон, но молчать о потере следа нельзя.
        logger.warning('backfill: не удалось записать полный отчёт', path=path, error=str(error))
        print(f'  !! полный отчёт записать не удалось: {error}')
        return None
    return str(path)


def _print_report(report) -> None:
    data = report.as_dict()
    print()
    print('=' * 62)
    print('  DRY RUN — ничего не записано' if data['dry_run'] else '  APPLIED')
    print('=' * 62)
    print(f'  пользователей в панели      : {data["panel_users"]}')
    print(f'  подписок к резолву          : {data["subscriptions_total"]}')
    print(f'  подписок связано            : {data["subscriptions_resolved"]}')
    print(f'  пользователей связано       : {data["users_resolved"]}')
    print(f'  grace-сессий связано        : {data["grace_sessions_resolved"]}')
    print()
    if data['by_strategy']:
        print('  по стратегии сопоставления:')
        for strategy, count in sorted(data['by_strategy'].items(), key=lambda kv: -kv[1]):
            print(f'    {strategy:<32} {count}')
        print()
    if report.conflicts:
        print(f'  !! КОНФЛИКТЫ: {len(report.conflicts)} — коммита не будет, пока не разберёте')
        for line in report.conflicts[:20]:
            print(f'     {line}')
        if len(report.conflicts) > 20:
            print(f'     ...и ещё {len(report.conflicts) - 20}')
        print()
    if report.unresolved:
        print(f'  не разрешено: {len(report.unresolved)}')
        by_reason: dict[str, int] = {}
        for row in report.unresolved:
            by_reason[row.reason] = by_reason.get(row.reason, 0) + 1
        for reason, count in sorted(by_reason.items(), key=lambda kv: -kv[1]):
            print(f'    {reason:<40} {count}')
        print()
        print('  первые 20 строк:')
        for row in report.unresolved[:20]:
            print(f'    {row.kind} #{row.row_id}: {row.reason} (uuid={row.remnawave_uuid})')
        print()
    print(f'  ИТОГ: {"полный" if report.complete else "ЧАСТИЧНЫЙ — см. выше"}')
    print('=' * 62)


async def _run(apply: bool) -> int:
    # Настройки, отредактированные из кабинета, живут в system_settings, а не в
    # .env — их обязан подтянуть и одноразовый контейнер. Без этого
    # MULTI_TARIFF_ENABLED (в .env.example его нет вовсе) читался бы как False,
    # и на мультитарифе бэкфилл пошёл бы по однотарифной ветке: перенёс бы
    # панельный id между подписками, чего в мультитарифе делать нельзя.
    await bot_configuration_service.initialize(sync_web_api_token=False)
    logger.info(
        'backfill: конфигурация загружена',
        multi_tariff=settings.is_multi_tariff_enabled(),
        sales_mode=settings.SALES_MODE,
    )
    print(f'  режим: {"МУЛЬТИТАРИФ" if settings.is_multi_tariff_enabled() else "однотарифный"}')

    async with AsyncSessionLocal() as db:
        report = await backfill_remnawave_ids(db, dry_run=not apply)
    _print_report(report)

    # Полный след — в файл. В консоли списки усечены до 20 строк, а инструкция
    # требует «разбирать по списку»: без файла полный перечень взять негде, как
    # и ответить потом на вопрос «какие строки прогон изменил и на что».
    audit_path = _write_audit(report, committed=apply and not report.conflicts)
    if audit_path:
        print(f'  полный отчёт: {audit_path}')

    if report.conflicts:
        return 2
    return 0 if report.complete else 1


def main() -> int:
    parser = argparse.ArgumentParser(description='Backfill numeric Remnawave panel ids (3.0.0 migration)')
    parser.add_argument('--apply', action='store_true', help='persist changes (default is a dry run)')
    args = parser.parse_args()
    return asyncio.run(_run(args.apply))


if __name__ == '__main__':
    sys.exit(main())
