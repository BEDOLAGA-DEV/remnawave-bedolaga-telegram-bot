import asyncio
import gzip
import html as html_lib
import json as json_lib
import math
import os
import shutil
import tarfile
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, date as dt_date, datetime, time as dt_time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import aiofiles
import pyzipper
import structlog
from aiogram.types import FSInputFile
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import selectinload
from sqlalchemy.pool import NullPool

from app.config import settings
from app.database.database import AsyncSessionLocal, engine, sync_postgres_sequences
from app.database.models import (
    AccessPolicy,
    AdminAuditLog,
    AdminRole,
    AdvertisingCampaign,
    AdvertisingCampaignRegistration,
    BroadcastHistory,
    ButtonClickLog,
    CabinetRefreshToken,
    CloudPaymentsPayment,
    ContestAttempt,
    ContestRound,
    ContestTemplate,
    CryptoBotPayment,
    DiscountOffer,
    FaqPage,
    FaqSetting,
    FreekassaPayment,
    HeleketPayment,
    KassaAiPayment,
    MainMenuButton,
    MenuLayoutHistory,
    MonitoringLog,
    MulenPayPayment,
    Pal24Payment,
    PartnerApplication,
    PaymentMethodConfig,
    PinnedMessage,
    PlategaPayment,
    Poll,
    PollAnswer,
    PollOption,
    PollQuestion,
    PollResponse,
    PrivacyPolicy,
    PromoCode,
    PromoCodeUse,
    PromoGroup,
    PromoOfferLog,
    PromoOfferTemplate,
    PublicOffer,
    ReferralContest,
    ReferralContestEvent,
    ReferralContestVirtualParticipant,
    ReferralEarning,
    RequiredChannel,
    SentNotification,
    ServerSquad,
    ServiceRule,
    Squad,
    Subscription,
    SubscriptionConversion,
    SubscriptionEvent,
    SubscriptionServer,
    SubscriptionTemporaryAccess,
    SupportAuditLog,
    SystemSetting,
    Tariff,
    Ticket,
    TicketMessage,
    TicketNotification,
    TrafficPurchase,
    Transaction,
    User,
    UserChannelSubscription,
    UserMessage,
    UserPromoGroup,
    UserRole,
    WataPayment,
    WebApiToken,
    Webhook,
    WebhookDelivery,
    WelcomeText,
    WheelConfig,
    WheelPrize,
    WheelSpin,
    WithdrawalRequest,
    YooKassaPayment,
    payment_method_promo_groups,
    server_squad_promo_groups,
    tariff_promo_groups,
)


logger = structlog.get_logger(__name__)


@dataclass
class BackupMetadata:
    timestamp: str
    version: str = '1.2'
    database_type: str = 'postgresql'
    backup_type: str = 'full'
    tables_count: int = 0
    total_records: int = 0
    compressed: bool = True
    file_size_bytes: int = 0
    created_by: int | None = None


@dataclass
class BackupSettings:
    auto_backup_enabled: bool = True
    backup_interval_hours: int = 24
    backup_time: str = '03:00'
    max_backups_keep: int = 7
    compression_enabled: bool = True
    include_logs: bool = False
    backup_location: str = '/app/data/backups'


class BackupService:
    def __init__(self, bot=None):
        self.bot = bot
        self.backup_dir = Path(settings.BACKUP_LOCATION).expanduser().resolve()
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir = self.backup_dir.parent
        self.archive_format_version = '2.0'
        self._auto_backup_task = None
        self._settings = self._load_settings()

        self._base_backup_models = [
            SystemSetting,
            ServiceRule,
            Squad,
            ServerSquad,
            PromoGroup,
            Tariff,  # Tariff должен быть ДО Subscription из-за FK
            User,
            PromoCode,
            WelcomeText,
            UserMessage,
            Subscription,
            SubscriptionServer,
            SubscriptionConversion,
            Transaction,
            YooKassaPayment,
            CryptoBotPayment,
            MulenPayPayment,
            Pal24Payment,
            PromoCodeUse,
            ReferralEarning,
            SentNotification,
            DiscountOffer,
            BroadcastHistory,
            AdvertisingCampaign,
            AdvertisingCampaignRegistration,
            Ticket,
            TicketMessage,
            SupportAuditLog,
            WebApiToken,
            # --- Payment providers (FK: users, transactions) ---
            HeleketPayment,
            WataPayment,
            PlategaPayment,
            CloudPaymentsPayment,
            FreekassaPayment,
            KassaAiPayment,
            # --- Settings/content ---
            PaymentMethodConfig,
            PrivacyPolicy,
            PublicOffer,
            FaqSetting,
            FaqPage,
            PinnedMessage,
            MainMenuButton,
            MenuLayoutHistory,
            # --- User data (FK: users, promo_groups, subscriptions) ---
            UserPromoGroup,
            TrafficPurchase,
            SubscriptionEvent,
            SubscriptionTemporaryAccess,
            PromoOfferTemplate,
            PromoOfferLog,
            # --- Referral/contests (FK: users) ---
            WithdrawalRequest,
            ReferralContest,
            ReferralContestEvent,
            ReferralContestVirtualParticipant,
            ContestTemplate,
            ContestRound,
            ContestAttempt,
            # --- Polls (FK chain: polls -> questions -> options -> answers) ---
            Poll,
            PollQuestion,
            PollOption,
            PollResponse,
            PollAnswer,
            # --- Webhooks ---
            Webhook,
            WebhookDelivery,
            # --- Wheel (FK chain: configs -> prizes -> spins) ---
            WheelConfig,
            WheelPrize,
            WheelSpin,
            # --- Support ---
            TicketNotification,
            ButtonClickLog,
            # --- RBAC / Admin ---
            AdminRole,
            UserRole,
            AccessPolicy,
            AdminAuditLog,
            # --- Channels / Partners ---
            RequiredChannel,
            UserChannelSubscription,
            PartnerApplication,
            CabinetRefreshToken,
        ]

        self.backup_models_ordered = self._base_backup_models.copy()

        if self._settings.include_logs:
            self.backup_models_ordered.append(MonitoringLog)

        self.association_tables = {
            'server_squad_promo_groups': server_squad_promo_groups,
            'tariff_promo_groups': tariff_promo_groups,
            'payment_method_promo_groups': payment_method_promo_groups,
        }

    def _load_settings(self) -> BackupSettings:
        return BackupSettings(
            auto_backup_enabled=os.getenv('BACKUP_AUTO_ENABLED', 'true').lower() == 'true',
            backup_interval_hours=int(os.getenv('BACKUP_INTERVAL_HOURS', '24')),
            backup_time=os.getenv('BACKUP_TIME', '03:00'),
            max_backups_keep=int(os.getenv('BACKUP_MAX_KEEP', '7')),
            compression_enabled=os.getenv('BACKUP_COMPRESSION', 'true').lower() == 'true',
            include_logs=os.getenv('BACKUP_INCLUDE_LOGS', 'false').lower() == 'true',
            backup_location=os.getenv('BACKUP_LOCATION', '/app/data/backups'),
        )

    def _parse_backup_time(self) -> tuple[int, int]:
        time_str = (self._settings.backup_time or '').strip()

        try:
            parts = time_str.split(':')
            if len(parts) != 2:
                raise ValueError('Invalid time format')

            hours, minutes = map(int, parts)

            if not (0 <= hours < 24 and 0 <= minutes < 60):
                raise ValueError('Hours or minutes out of range')

            return hours, minutes

        except ValueError:
            default_hours, default_minutes = 3, 0
            logger.warning(
                "Некорректное значение BACKUP_TIME=''. Используется значение по умолчанию 03:00.",
                backup_time=self._settings.backup_time,
            )
            self._settings.backup_time = '03:00'
            return default_hours, default_minutes

    def _calculate_next_backup_datetime(self, reference: datetime | None = None) -> datetime:
        reference = reference or datetime.now(UTC)
        hours, minutes = self._parse_backup_time()

        next_run = reference.replace(hour=hours, minute=minutes, second=0, microsecond=0)
        if next_run <= reference:
            next_run += timedelta(days=1)

        return next_run

    def _get_backup_interval(self) -> timedelta:
        hours = self._settings.backup_interval_hours

        if hours <= 0:
            logger.warning(
                'Некорректное значение BACKUP_INTERVAL_HOURS=. Используется значение по умолчанию 24.', hours=hours
            )
            hours = 24
            self._settings.backup_interval_hours = hours

        return timedelta(hours=hours)

    def _get_models_for_backup(self, include_logs: bool) -> list[Any]:
        models = self._base_backup_models.copy()

        if include_logs:
            if MonitoringLog not in models:
                models.append(MonitoringLog)
        else:
            models = [model for model in models if model is not MonitoringLog]

        return models

    def _resolve_command_path(self, command: str, env_var: str) -> str | None:
        override = os.getenv(env_var)
        if override:
            override_path = Path(override)
            if override_path.exists() and os.access(override_path, os.X_OK):
                return str(override_path)
            logger.warning('Путь из недоступен или не является исполняемым', override=override, env_var=env_var)

        resolved = shutil.which(command)
        if resolved:
            return resolved

        return None

    async def create_backup(
        self, created_by: int | None = None, compress: bool = True, include_logs: bool = None
    ) -> tuple[bool, str, str | None]:
        try:
            logger.info('📄 Начинаем создание бекапа...')

            if include_logs is None:
                include_logs = self._settings.include_logs

            overview = await self._collect_database_overview()

            timestamp = datetime.now(UTC).strftime('%Y%m%d_%H%M%S')
            archive_suffix = '.tar.gz' if compress else '.tar'
            filename = f'backup_{timestamp}{archive_suffix}'
            backup_path = self.backup_dir / filename

            with tempfile.TemporaryDirectory() as temp_dir:
                temp_path = Path(temp_dir)
                staging_dir = temp_path / 'backup'
                await asyncio.to_thread(lambda: staging_dir.mkdir(parents=True, exist_ok=True))

                database_info = await self._dump_database(staging_dir, include_logs=include_logs)
                database_info.setdefault('tables_count', overview.get('tables_count', 0))
                database_info.setdefault('total_records', overview.get('total_records', 0))
                files_info = await self._collect_files(staging_dir, include_logs=include_logs)
                data_snapshot_info = await self._collect_data_snapshot(staging_dir)

                metadata = {
                    'format_version': self.archive_format_version,
                    'timestamp': datetime.now(UTC).isoformat(),
                    'database_type': 'postgresql' if settings.is_postgresql() else 'sqlite',
                    'backup_type': 'full',
                    'tables_count': overview.get('tables_count', 0),
                    'total_records': overview.get('total_records', 0),
                    'compressed': True,
                    'created_by': created_by,
                    'database': database_info,
                    'files': files_info,
                    'data_snapshot': data_snapshot_info,
                    'settings': asdict(self._settings),
                }

                metadata_path = staging_dir / 'metadata.json'
                async with aiofiles.open(metadata_path, 'w', encoding='utf-8') as meta_file:
                    await meta_file.write(json_lib.dumps(metadata, ensure_ascii=False, indent=2))

                mode = 'w:gz' if compress else 'w'
                with tarfile.open(backup_path, mode) as tar:
                    for item in await asyncio.to_thread(lambda: list(staging_dir.iterdir())):
                        tar.add(item, arcname=item.name)

            file_size = (await asyncio.to_thread(backup_path.stat)).st_size

            await self._cleanup_old_backups()

            size_mb = file_size / 1024 / 1024
            message = (
                f'✅ Бекап успешно создан!\n'
                f'📁 Файл: {filename}\n'
                f'📊 Таблиц: {overview.get("tables_count", 0)}\n'
                f'📈 Записей: {overview.get("total_records", 0):,}\n'
                f'💾 Размер: {size_mb:.2f} MB'
            )

            logger.info(message)

            if self.bot:
                await self._send_backup_notification('success', message, str(backup_path))

                await self._send_backup_file_to_chat(str(backup_path))

            return True, message, str(backup_path)

        except Exception as e:
            error_msg = f'❌ Ошибка создания бекапа: {e!s}'
            logger.error(error_msg, exc_info=True)

            if self.bot:
                await self._send_backup_notification('error', error_msg)

            return False, error_msg, None

    async def restore_backup(self, backup_file_path: str, clear_existing: bool = False) -> tuple[bool, str]:
        try:
            logger.info('📄 Начинаем восстановление из', backup_file_path=backup_file_path)

            backup_path = Path(backup_file_path)
            if not await asyncio.to_thread(backup_path.exists):
                return False, f'❌ Файл бекапа не найден: {backup_file_path}'

            if self._is_archive_backup(backup_path):
                success, message = await self._restore_from_archive(backup_path, clear_existing)
            else:
                success, message = await self._restore_from_legacy(backup_path, clear_existing)

            if success and self.bot:
                await self._send_backup_notification('restore_success', message)
            elif not success and self.bot:
                await self._send_backup_notification('restore_error', message)

            return success, message

        except Exception as e:
            error_msg = f'❌ Ошибка восстановления: {e!s}'
            logger.error(error_msg, exc_info=True)

            if self.bot:
                await self._send_backup_notification('restore_error', error_msg)

            return False, error_msg

    async def _collect_database_overview(self) -> dict[str, Any]:
        overview: dict[str, Any] = {
            'tables_count': 0,
            'total_records': 0,
            'tables': [],
        }

        try:
            async with engine.begin() as conn:
                table_names = await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_table_names())

                for table_name in table_names:
                    try:
                        result = await conn.execute(text(f'SELECT COUNT(*) FROM {table_name}'))
                        count = result.scalar_one()
                    except Exception:
                        count = 0

                    overview['tables'].append({'name': table_name, 'rows': count})
                    overview['total_records'] += count

                overview['tables_count'] = len(table_names)
        except Exception as exc:
            logger.warning('Не удалось собрать статистику по БД', exc=exc)

        return overview

    async def _dump_database(self, staging_dir: Path, include_logs: bool) -> dict[str, Any]:
        if settings.is_postgresql():
            pg_dump_path = self._resolve_command_path('pg_dump', 'PG_DUMP_PATH')

            if pg_dump_path:
                dump_path = staging_dir / 'database.sql'
                await self._dump_postgres(dump_path, pg_dump_path)
                size = (
                    (await asyncio.to_thread(dump_path.stat)).st_size
                    if await asyncio.to_thread(dump_path.exists)
                    else 0
                )
                return {
                    'type': 'postgresql',
                    'path': dump_path.name,
                    'size_bytes': size,
                    'format': 'sql',
                    'tool': pg_dump_path,
                }

            logger.info('pg_dump не найден в PATH. Используется ORM-дамп в формате JSON')
            json_info = await self._dump_postgres_json(staging_dir, include_logs)
            return json_info

        dump_path = staging_dir / 'database.sqlite'
        await self._dump_sqlite(dump_path)
        size = (await asyncio.to_thread(dump_path.stat)).st_size if await asyncio.to_thread(dump_path.exists) else 0
        return {
            'type': 'sqlite',
            'path': dump_path.name,
            'size_bytes': size,
            'format': 'file',
        }

    async def _dump_postgres(self, dump_path: Path, pg_dump_path: str):
        env = os.environ.copy()
        env.update(
            {
                'PGHOST': settings.POSTGRES_HOST,
                'PGPORT': str(settings.POSTGRES_PORT),
                'PGUSER': settings.POSTGRES_USER,
                'PGPASSWORD': settings.POSTGRES_PASSWORD,
            }
        )

        command = [
            pg_dump_path,
            '--format=plain',
            '--no-owner',
            '--no-privileges',
            settings.POSTGRES_DB,
        ]

        logger.info('📦 Экспорт PostgreSQL через pg_dump ...', pg_dump_path=pg_dump_path)
        await asyncio.to_thread(lambda: dump_path.parent.mkdir(parents=True, exist_ok=True))

        with open(dump_path, 'wb') as dump_file:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=dump_file,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            _, stderr = await process.communicate()

        if process.returncode != 0:
            error_text = stderr.decode() if stderr else 'pg_dump error'
            raise RuntimeError(f'pg_dump завершился с ошибкой: {error_text}')

        logger.info('✅ PostgreSQL dump создан', dump_path=dump_path)

    async def _dump_postgres_json(self, staging_dir: Path, include_logs: bool) -> dict[str, Any]:
        models_to_backup = self._get_models_for_backup(include_logs)
        (
            backup_data,
            association_data,
            total_records,
            tables_count,
        ) = await self._export_database_via_orm(models_to_backup)

        dump_path = staging_dir / 'database.json'
        dump_structure = {
            'metadata': {
                'timestamp': datetime.now(UTC).isoformat(),
                'version': 'orm-1.0',
                'database_type': 'postgresql',
                'tables_count': tables_count,
                'total_records': total_records,
            },
            'data': backup_data,
            'associations': association_data,
        }

        async with aiofiles.open(dump_path, 'w', encoding='utf-8') as dump_file:
            await dump_file.write(json_lib.dumps(dump_structure, ensure_ascii=False, indent=2))

        size = (await asyncio.to_thread(dump_path.stat)).st_size if await asyncio.to_thread(dump_path.exists) else 0

        logger.info('✅ PostgreSQL экспортирован через ORM в JSON', dump_path=dump_path)

        return {
            'type': 'postgresql',
            'path': dump_path.name,
            'size_bytes': size,
            'format': 'json',
            'tool': 'orm',
            'format_version': 'orm-1.0',
            'tables_count': tables_count,
            'total_records': total_records,
        }

    async def _dump_sqlite(self, dump_path: Path):
        sqlite_path = Path(settings.SQLITE_PATH)
        if not await asyncio.to_thread(sqlite_path.exists):
            raise FileNotFoundError(f'SQLite база данных не найдена по пути {sqlite_path}')

        await asyncio.to_thread(lambda: dump_path.parent.mkdir(parents=True, exist_ok=True))
        await asyncio.to_thread(shutil.copy2, sqlite_path, dump_path)
        logger.info('✅ SQLite база данных скопирована', dump_path=dump_path)

    async def _export_database_via_orm(
        self,
        models_to_backup: list[Any],
    ) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]], int, int]:
        backup_data: dict[str, list[dict[str, Any]]] = {}
        total_records = 0

        async with AsyncSessionLocal() as db:
            try:
                for model in models_to_backup:
                    table_name = model.__tablename__
                    logger.info('📊 Экспортируем таблицу', table_name=table_name)

                    try:
                        query = select(model)

                        if model == User:
                            query = query.options(selectinload(User.subscription))
                        elif model == Subscription:
                            query = query.options(selectinload(Subscription.user))
                        elif model == Transaction:
                            query = query.options(selectinload(Transaction.user))

                        result = await db.execute(query)
                        records = result.scalars().all()
                    except Exception as table_exc:
                        logger.warning(
                            '⚠️ Ошибка экспорта таблицы, пропускаем',
                            table_name=table_name,
                            error=str(table_exc),
                        )
                        await db.rollback()
                        backup_data[table_name] = []
                        continue

                    table_data: list[dict[str, Any]] = []
                    for record in records:
                        record_dict: dict[str, Any] = {}
                        for column in model.__table__.columns:
                            value = getattr(record, column.name)

                            if value is None:
                                record_dict[column.name] = None
                            elif isinstance(value, (datetime, dt_date, dt_time)):
                                record_dict[column.name] = value.isoformat()
                            elif isinstance(value, Decimal):
                                record_dict[column.name] = float(value)
                            elif isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
                                record_dict[column.name] = 0.0
                            elif isinstance(value, (list, dict)):
                                try:
                                    record_dict[column.name] = json_lib.dumps(value) if value is not None else None
                                except TypeError:
                                    record_dict[column.name] = str(value)
                            elif hasattr(value, '__dict__'):
                                record_dict[column.name] = str(value)
                            else:
                                record_dict[column.name] = value

                        table_data.append(record_dict)

                    backup_data[table_name] = table_data
                    total_records += len(table_data)

                    logger.info('✅ Экспортировано записей из', table_data_count=len(table_data), table_name=table_name)

                association_data = await self._export_association_tables(db)
                for records in association_data.values():
                    total_records += len(records)

                tables_count = len(models_to_backup) + len(association_data)
                return backup_data, association_data, total_records, tables_count

            except Exception as exc:
                logger.error('Ошибка при экспорте данных', exc=exc)
                raise

    async def _collect_files(self, staging_dir: Path, include_logs: bool) -> list[dict[str, Any]]:
        files_info: list[dict[str, Any]] = []
        files_dir = staging_dir / 'files'
        await asyncio.to_thread(lambda: files_dir.mkdir(parents=True, exist_ok=True))

        if include_logs and settings.LOG_FILE:
            log_path = Path(settings.LOG_FILE)
            if await asyncio.to_thread(log_path.exists):
                dest = files_dir / log_path.name
                await asyncio.to_thread(shutil.copy2, log_path, dest)
                files_info.append(
                    {
                        'path': str(log_path),
                        'relative_path': f'files/{log_path.name}',
                    }
                )

        if not files_info and await asyncio.to_thread(files_dir.exists):
            await asyncio.to_thread(files_dir.rmdir)

        return files_info

    async def _collect_data_snapshot(self, staging_dir: Path) -> dict[str, Any]:
        data_dir = staging_dir / 'data'
        snapshot_info: dict[str, Any] = {
            'path': str(self.data_dir),
            'items': 0,
        }

        if not await asyncio.to_thread(self.data_dir.exists):
            return snapshot_info

        counter = {'items': 0}

        def _copy_data():
            data_dir.mkdir(parents=True, exist_ok=True)
            for item in self.data_dir.iterdir():
                if item.resolve() == self.backup_dir.resolve():
                    continue

                destination = data_dir / item.name
                if item.is_dir():
                    shutil.copytree(item, destination, dirs_exist_ok=True)
                else:
                    shutil.copy2(item, destination)
                counter['items'] += 1

        await asyncio.to_thread(_copy_data)
        snapshot_info['items'] = counter['items']
        return snapshot_info

    def _is_archive_backup(self, backup_path: Path) -> bool:
        suffixes = backup_path.suffixes
        if (len(suffixes) >= 2 and suffixes[-2:] == ['.tar', '.gz']) or (suffixes and suffixes[-1] == '.tar'):
            return True
        try:
            return tarfile.is_tarfile(backup_path)
        except Exception:
            return False

    async def _restore_from_archive(
        self,
        backup_path: Path,
        clear_existing: bool,
    ) -> tuple[bool, str]:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)

            mode = 'r:gz' if backup_path.suffixes and backup_path.suffixes[-1] == '.gz' else 'r'
            with tarfile.open(backup_path, mode) as tar:
                tar.extractall(temp_path, filter='data')

            metadata_path = temp_path / 'metadata.json'
            if not await asyncio.to_thread(metadata_path.exists):
                return False, '❌ Метаданные бекапа отсутствуют'

            async with aiofiles.open(metadata_path, encoding='utf-8') as meta_file:
                metadata = json_lib.loads(await meta_file.read())

            logger.info('📊 Загружен бекап формата', metadata=metadata.get('format_version', 'unknown'))

            database_info = metadata.get('database', {})
            metadata.get('data_snapshot', {})
            files_info = metadata.get('files', [])

            if database_info.get('type') == 'postgresql':
                db_format = database_info.get('format', 'sql')
                default_name = 'database.json' if db_format == 'json' else 'database.sql'
                dump_file = temp_path / database_info.get('path', default_name)

                if db_format == 'json':
                    await self._restore_postgres_json(dump_file, clear_existing)
                else:
                    await self._restore_postgres(dump_file, clear_existing)
            else:
                dump_file = temp_path / database_info.get('path', 'database.sqlite')
                await self._restore_sqlite(dump_file, clear_existing)

            data_dir = temp_path / 'data'
            if await asyncio.to_thread(data_dir.exists):
                await self._restore_data_snapshot(data_dir, clear_existing)

            if files_info:
                await self._restore_files(files_info, temp_path)

            message = (
                f'✅ Восстановление завершено!\n'
                f'📊 Таблиц: {metadata.get("tables_count", 0)}\n'
                f'📈 Записей: {metadata.get("total_records", 0):,}\n'
                f'📅 Дата бекапа: {metadata.get("timestamp", "неизвестно")}'
            )

            logger.info(message)
            return True, message

    async def _restore_postgres(self, dump_path: Path, clear_existing: bool):
        if not await asyncio.to_thread(dump_path.exists):
            raise FileNotFoundError(f'Dump PostgreSQL не найден: {dump_path}')

        psql_path = self._resolve_command_path('psql', 'PSQL_PATH')
        if not psql_path:
            raise FileNotFoundError(
                'psql не найден в PATH. Установите клиент PostgreSQL или выполните восстановление из JSON дампа'
            )

        env = os.environ.copy()
        env.update(
            {
                'PGHOST': settings.POSTGRES_HOST,
                'PGPORT': str(settings.POSTGRES_PORT),
                'PGUSER': settings.POSTGRES_USER,
                'PGPASSWORD': settings.POSTGRES_PASSWORD,
            }
        )

        if clear_existing:
            logger.info('🗑️ Полная очистка схемы PostgreSQL перед восстановлением')
            drop_command = [
                psql_path,
                settings.POSTGRES_DB,
                '-c',
                'DROP SCHEMA public CASCADE; CREATE SCHEMA public; GRANT ALL ON SCHEMA public TO public;',
            ]
            proc = await asyncio.create_subprocess_exec(
                *drop_command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                raise RuntimeError(f'Не удалось очистить схему: {stderr.decode()}')

        logger.info('📥 Восстановление PostgreSQL через psql ...', psql_path=psql_path)
        restore_command = [
            psql_path,
            settings.POSTGRES_DB,
            '-f',
            str(dump_path),
        ]
        proc = await asyncio.create_subprocess_exec(
            *restore_command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            raise RuntimeError(f'Ошибка psql: {stderr.decode()}')

        logger.info('✅ PostgreSQL восстановлен', dump_path=dump_path)

    async def _restore_postgres_json(self, dump_path: Path, clear_existing: bool):
        if not await asyncio.to_thread(dump_path.exists):
            raise FileNotFoundError(f'JSON дамп PostgreSQL не найден: {dump_path}')

        async with aiofiles.open(dump_path, encoding='utf-8') as dump_file:
            dump_data = json_lib.loads(await dump_file.read())

        metadata = dump_data.get('metadata', {})
        backup_data = dump_data.get('data', {})
        association_data = dump_data.get('associations', {})

        await self._restore_database_payload(
            backup_data,
            association_data,
            metadata,
            clear_existing,
        )

        logger.info('✅ PostgreSQL восстановлен из ORM JSON', dump_path=dump_path)

    async def _restore_sqlite(self, dump_path: Path, clear_existing: bool):
        if not await asyncio.to_thread(dump_path.exists):
            raise FileNotFoundError(f'SQLite файл не найден: {dump_path}')

        target_path = Path(settings.SQLITE_PATH)
        await asyncio.to_thread(lambda: target_path.parent.mkdir(parents=True, exist_ok=True))

        if clear_existing and await asyncio.to_thread(target_path.exists):
            await asyncio.to_thread(target_path.unlink)

        await asyncio.to_thread(shutil.copy2, dump_path, target_path)
        logger.info('✅ SQLite база восстановлена', target_path=target_path)

    async def _restore_data_snapshot(self, source_dir: Path, clear_existing: bool):
        if not await asyncio.to_thread(source_dir.exists):
            return

        def _restore():
            self.data_dir.mkdir(parents=True, exist_ok=True)
            for item in source_dir.iterdir():
                if item.name == self.backup_dir.name:
                    continue

                destination = self.data_dir / item.name
                if clear_existing and destination.exists():
                    if destination.is_dir():
                        shutil.rmtree(destination)
                    else:
                        destination.unlink()

                if item.is_dir():
                    shutil.copytree(item, destination, dirs_exist_ok=True)
                else:
                    shutil.copy2(item, destination)

        await asyncio.to_thread(_restore)
        logger.info('📁 Снимок директории data восстановлен')

    async def _restore_files(self, files_info: list[dict[str, Any]], temp_path: Path):
        allowed_base = await asyncio.to_thread(self.data_dir.resolve)

        for file_info in files_info:
            relative_path = file_info.get('relative_path')
            target_path = Path(file_info.get('path', ''))
            if not relative_path or not target_path:
                continue

            target_resolved = await asyncio.to_thread(target_path.resolve)
            if not str(target_resolved).startswith(str(allowed_base) + os.sep) and target_resolved != allowed_base:
                logger.warning('Заблокирована запись за пределами data_dir', target_path=target_path)
                continue

            source_file = await asyncio.to_thread((temp_path / relative_path).resolve)
            temp_path_resolved = await asyncio.to_thread(temp_path.resolve)
            if not str(source_file).startswith(str(temp_path_resolved) + os.sep):
                logger.warning('Path traversal в relative_path', relative_path=relative_path)
                continue

            if not await asyncio.to_thread(source_file.exists):
                logger.warning('Файл отсутствует в архиве', relative_path=relative_path)
                continue

            await asyncio.to_thread(lambda: target_resolved.parent.mkdir(parents=True, exist_ok=True))
            await asyncio.to_thread(shutil.copy2, source_file, target_resolved)
            logger.info('📁 Файл восстановлен', target_resolved=target_resolved)

    async def _restore_database_payload(
        self,
        backup_data: dict[str, list[dict[str, Any]]],
        association_data: dict[str, list[dict[str, Any]]],
        metadata: dict[str, Any],
        clear_existing: bool,
    ) -> tuple[int, int]:
        if not backup_data:
            raise ValueError('❌ Файл бекапа не содержит данных')

        logger.info('📊 Загружен дамп', metadata=metadata.get('timestamp', 'неизвестная дата'))

        estimated_records = metadata.get('total_records')
        if estimated_records is None:
            estimated_records = sum(len(records) for records in backup_data.values())
            estimated_records += sum(len(records) for records in association_data.values())

        logger.info('📈 Содержит записей', estimated_records=estimated_records)

        restored_records = 0
        restored_tables = 0

        async with AsyncSessionLocal() as db:
            try:
                if clear_existing:
                    logger.warning('🗑️ Очищаем существующие данные...')
                    await self._clear_database_tables(db, backup_data)

                models_for_restore = self._get_models_for_backup(True)
                models_by_table = {model.__tablename__: model for model in models_for_restore}

                pre_restore_tables = {'promo_groups', 'tariffs'}
                for table_name in pre_restore_tables:
                    model = models_by_table.get(table_name)
                    if not model:
                        continue

                    records = backup_data.get(table_name, [])
                    if not records:
                        continue

                    logger.info(
                        '🔥 Восстанавливаем таблицу (записей)', table_name=table_name, records_count=len(records)
                    )
                    restored = await self._restore_table_records(
                        db,
                        model,
                        table_name,
                        records,
                        clear_existing,
                    )
                    restored_records += restored

                    if restored:
                        restored_tables += 1
                        logger.info('✅ Таблица восстановлена', table_name=table_name)

                await self._restore_users_without_referrals(
                    db,
                    backup_data,
                    models_by_table,
                )

                for model in models_for_restore:
                    table_name = model.__tablename__

                    if table_name == 'users' or table_name in pre_restore_tables:
                        continue

                    records = backup_data.get(table_name, [])
                    if not records:
                        continue

                    logger.info(
                        '🔥 Восстанавливаем таблицу (записей)', table_name=table_name, records_count=len(records)
                    )
                    restored = await self._restore_table_records(
                        db,
                        model,
                        table_name,
                        records,
                        clear_existing,
                    )
                    restored_records += restored

                    if restored:
                        restored_tables += 1
                        logger.info('✅ Таблица восстановлена', table_name=table_name)

                # Flush все изменения перед обновлением реферальных связей
                await db.flush()

                await self._update_user_referrals(db, backup_data)

                assoc_tables, assoc_records = await self._restore_association_tables(
                    db,
                    association_data,
                    clear_existing,
                )
                restored_tables += assoc_tables
                restored_records += assoc_records

                await db.commit()

                # Синхронизируем PostgreSQL sequences после ORM-восстановления,
                # чтобы auto-increment ID не конфликтовали с восстановленными данными
                try:
                    await sync_postgres_sequences()
                    logger.info('🔢 Последовательности PostgreSQL синхронизированы')
                except Exception as seq_err:
                    logger.warning('⚠️ Не удалось синхронизировать sequences', error=seq_err)

            except Exception as exc:
                await db.rollback()
                logger.error('Ошибка при восстановлении', exc=exc)
                raise

        return restored_tables, restored_records

    async def _restore_from_legacy(
        self,
        backup_path: Path,
        clear_existing: bool,
    ) -> tuple[bool, str]:
        if backup_path.suffix == '.gz':
            async with aiofiles.open(backup_path, 'rb') as f:
                compressed_data = await f.read()
                uncompressed_data = gzip.decompress(compressed_data).decode('utf-8')
                backup_structure = json_lib.loads(uncompressed_data)
        else:
            async with aiofiles.open(backup_path, encoding='utf-8') as f:
                file_content = await f.read()
                backup_structure = json_lib.loads(file_content)

        metadata = backup_structure.get('metadata', {})
        backup_data = backup_structure.get('data', {})
        association_data = backup_structure.get('associations', {})
        file_snapshots = backup_structure.get('files', {})

        try:
            restored_tables, restored_records = await self._restore_database_payload(
                backup_data,
                association_data,
                metadata,
                clear_existing,
            )
        except ValueError as exc:
            return False, str(exc)

        if file_snapshots:
            restored_files = await self._restore_file_snapshots(file_snapshots)
            if restored_files:
                logger.info('📁 Восстановлено файлов конфигурации', restored_files=restored_files)

        message = (
            f'✅ Восстановление завершено!\n'
            f'📊 Таблиц: {restored_tables}\n'
            f'📈 Записей: {restored_records:,}\n'
            f'📅 Дата бекапа: {metadata.get("timestamp", "неизвестно")}'
        )

        logger.info(message)
        return True, message

    async def _restore_users_without_referrals(self, db: AsyncSession, backup_data: dict, models_by_table: dict):
        users_data = backup_data.get('users', [])
        if not users_data:
            return

        logger.info('👥 Восстанавливаем пользователей без реферальных связей', users_data_count=len(users_data))

        User = models_by_table['users']

        for user_data in users_data:
            try:
                processed_data = self._process_record_data(user_data, User, 'users')
                processed_data['referred_by_id'] = None

                if 'id' in processed_data:
                    existing_user = await db.execute(select(User).where(User.id == processed_data['id']))
                    existing = existing_user.scalar_one_or_none()

                    if existing:
                        for key, value in processed_data.items():
                            if key != 'id':
                                setattr(existing, key, value)
                    else:
                        instance = User(**processed_data)
                        try:
                            async with db.begin_nested():
                                db.add(instance)
                                await db.flush()
                        except IntegrityError:
                            logger.warning(
                                'Дубликат пользователя (id telegram_id=), пропускаем',
                                processed_data=processed_data.get('id'),
                                processed_data_2=processed_data.get('telegram_id'),
                            )
                            continue
                else:
                    instance = User(**processed_data)
                    try:
                        async with db.begin_nested():
                            db.add(instance)
                            await db.flush()
                    except IntegrityError:
                        logger.warning(
                            'Дубликат пользователя (telegram_id=), пропускаем',
                            processed_data=processed_data.get('telegram_id'),
                        )
                        continue

            except Exception as e:
                logger.error('Ошибка при восстановлении пользователя', error=e)
                raise

        try:
            async with db.begin_nested():
                await db.flush()
        except IntegrityError as e:
            logger.warning('IntegrityError при flush пользователей, savepoint откачен', e=e)
        logger.info('✅ Пользователи без реферальных связей восстановлены')

    async def _update_user_referrals(self, db: AsyncSession, backup_data: dict):
        users_data = backup_data.get('users', [])
        if not users_data:
            return

        logger.info('🔗 Обновляем реферальные связи пользователей')

        for user_data in users_data:
            try:
                referred_by_id = user_data.get('referred_by_id')
                user_id = user_data.get('id')

                if referred_by_id and user_id:
                    referrer_result = await db.execute(select(User).where(User.id == referred_by_id))
                    referrer = referrer_result.scalar_one_or_none()

                    if referrer:
                        user_result = await db.execute(select(User).where(User.id == user_id))
                        user = user_result.scalar_one_or_none()

                        if user:
                            user.referred_by_id = referred_by_id
                        else:
                            logger.warning('Пользователь не найден для обновления реферальной связи', user_id=user_id)
                    else:
                        logger.warning(
                            'Реферер не найден для пользователя', referred_by_id=referred_by_id, user_id=user_id
                        )

            except Exception as e:
                logger.error('Ошибка при обновлении реферальной связи', error=e)
                continue

        await db.flush()
        logger.info('✅ Реферальные связи обновлены')

    def _process_record_data(self, record_data: dict, model, table_name: str) -> dict:
        processed_data = {}

        for key, value in record_data.items():
            if value is None:
                processed_data[key] = None
                continue

            column = getattr(model.__table__.columns, key, None)
            if column is None:
                logger.warning('Колонка не найдена в модели', key=key, table_name=table_name)
                continue

            column_type_str = str(column.type).upper()

            if ('DATETIME' in column_type_str or 'TIMESTAMP' in column_type_str) and isinstance(value, str):
                try:
                    if 'T' in value:
                        processed_data[key] = datetime.fromisoformat(value.replace('Z', '+00:00'))
                    else:
                        processed_data[key] = datetime.strptime(value, '%Y-%m-%d %H:%M:%S').replace(tzinfo=UTC)
                except (ValueError, TypeError) as e:
                    logger.warning('Не удалось парсить дату для поля', value=value, key=key, error=e)
                    processed_data[key] = datetime.now(UTC)
            elif column_type_str == 'TIME' and isinstance(value, str):
                try:
                    processed_data[key] = dt_time.fromisoformat(value)
                except (ValueError, TypeError) as e:
                    logger.warning('Не удалось парсить время для поля', value=value, key=key, error=e)
                    processed_data[key] = dt_time(hour=12, minute=0)
            elif column_type_str == 'DATE' and isinstance(value, str):
                try:
                    processed_data[key] = dt_date.fromisoformat(value)
                except (ValueError, TypeError) as e:
                    logger.warning('Не удалось парсить дату для поля', value=value, key=key, error=e)
                    processed_data[key] = None
            elif ('BOOLEAN' in column_type_str or 'BOOL' in column_type_str) and isinstance(value, str):
                processed_data[key] = value.lower() in ('true', '1', 'yes', 'on')
            elif (
                'INTEGER' in column_type_str or 'INT' in column_type_str or 'BIGINT' in column_type_str
            ) and isinstance(value, str):
                try:
                    processed_data[key] = int(value)
                except ValueError:
                    processed_data[key] = 0
            elif (
                'FLOAT' in column_type_str or 'REAL' in column_type_str or 'NUMERIC' in column_type_str
            ) and isinstance(value, str):
                try:
                    processed_data[key] = float(value)
                except ValueError:
                    processed_data[key] = 0.0
            elif 'JSON' in column_type_str:
                if isinstance(value, str) and value.strip():
                    try:
                        processed_data[key] = json_lib.loads(value)
                    except (ValueError, TypeError):
                        processed_data[key] = value
                elif isinstance(value, (list, dict)):
                    processed_data[key] = value
                else:
                    processed_data[key] = None
            else:
                processed_data[key] = value

        return processed_data

    def _get_primary_key_columns(self, model) -> list[str]:
        return [col.name for col in model.__table__.columns if col.primary_key]

    async def _export_association_tables(self, db: AsyncSession) -> dict[str, list[dict[str, Any]]]:
        association_data: dict[str, list[dict[str, Any]]] = {}

        for table_name, table_obj in self.association_tables.items():
            try:
                logger.info('📊 Экспортируем таблицу связей', table_name=table_name)
                result = await db.execute(select(table_obj))
                rows = result.mappings().all()
                association_data[table_name] = [dict(row) for row in rows]
                logger.info('✅ Экспортировано связей из', rows_count=len(rows), table_name=table_name)
            except Exception as e:
                logger.error('Ошибка экспорта таблицы связей', table_name=table_name, error=e)

        return association_data

    async def _restore_association_tables(
        self, db: AsyncSession, association_data: dict[str, list[dict[str, Any]]], clear_existing: bool
    ) -> tuple[int, int]:
        if not association_data:
            return 0, 0

        restored_tables = 0
        restored_records = 0

        for table_name, table_obj in self.association_tables.items():
            if table_name not in association_data:
                continue
            col_names = [col.name for col in table_obj.columns]
            restored = await self._restore_association_table(
                db, table_obj, table_name, association_data[table_name], clear_existing, col_names
            )
            restored_tables += 1
            restored_records += restored

        return restored_tables, restored_records

    async def _restore_association_table(
        self,
        db: AsyncSession,
        table_obj,
        table_name: str,
        records: list[dict[str, Any]],
        clear_existing: bool,
        col_names: list[str],
    ) -> int:
        if not records:
            return 0

        if clear_existing:
            await db.execute(table_obj.delete())

        restored = 0

        for record in records:
            values = {col: record.get(col) for col in col_names}

            if any(v is None for v in values.values()):
                logger.warning('Пропущена некорректная запись', table_name=table_name, record=record)
                continue

            try:
                first_col = col_names[0]
                exists_stmt = (
                    select(table_obj.c[first_col])
                    .where(*[table_obj.c[col] == values[col] for col in col_names])
                    .limit(1)
                )
                existing = await db.execute(exists_stmt)

                if existing.scalar_one_or_none() is not None:
                    logger.debug('Запись уже существует', table_name=table_name, values=values)
                    continue

                try:
                    async with db.begin_nested():
                        await db.execute(table_obj.insert().values(**values))
                    restored += 1
                except IntegrityError:
                    logger.warning('Пропускаем связь (FK или дубликат)', table_name=table_name, values=values)
                    continue
            except Exception as e:
                logger.error('Ошибка при восстановлении связи', table_name=table_name, values=values, e=e)
                raise

        return restored

    async def _restore_table_records(
        self, db: AsyncSession, model, table_name: str, records: list[dict[str, Any]], clear_existing: bool
    ) -> int:
        restored_count = 0

        # Кешируем существующие tariff_id для проверки FK
        existing_tariff_ids = set()
        if table_name == 'subscriptions':
            try:
                result = await db.execute(select(Tariff.id))
                existing_tariff_ids = {row[0] for row in result.fetchall()}
                logger.info(
                    '📋 Найдено существующих тарифов для валидации FK',
                    existing_tariff_ids_count=len(existing_tariff_ids),
                )
            except Exception as e:
                logger.warning('⚠️ Не удалось получить список тарифов', error=e)

        for record_data in records:
            try:
                processed_data = self._process_record_data(record_data, model, table_name)

                # Валидация FK для subscriptions.tariff_id
                if table_name == 'subscriptions' and 'tariff_id' in processed_data:
                    tariff_id = processed_data.get('tariff_id')
                    if tariff_id is not None and tariff_id not in existing_tariff_ids:
                        logger.warning(
                            '⚠️ Тариф не найден, устанавливаем tariff_id=NULL для подписки', tariff_id=tariff_id
                        )
                        processed_data['tariff_id'] = None

                pk_cols = self._get_primary_key_columns(model)

                if pk_cols and all(col in processed_data for col in pk_cols):
                    where_clause = [getattr(model, col) == processed_data[col] for col in pk_cols]
                    existing_record = await db.execute(select(model).where(*where_clause))
                    existing = existing_record.scalar_one_or_none()

                    if existing:
                        for key, value in processed_data.items():
                            if key not in pk_cols:
                                setattr(existing, key, value)
                    else:
                        instance = model(**processed_data)
                        try:
                            async with db.begin_nested():
                                db.add(instance)
                                await db.flush()
                        except IntegrityError:
                            # Unique constraint conflict — record exists with different PK
                            logger.warning(
                                'Дубликат по уникальному ключу в %s (PK=%s), пропускаем',
                                table_name,
                                {col: processed_data.get(col) for col in pk_cols},
                            )
                            continue
                else:
                    instance = model(**processed_data)
                    db.add(instance)

                restored_count += 1

            except Exception as e:
                logger.error('Ошибка восстановления записи в', table_name=table_name, error=e)
                logger.error('Проблемные данные', record_data=record_data)
                raise

        return restored_count

    async def _clear_database_tables(self, db: AsyncSession, backup_data: dict[str, Any] | None = None):
        # Все таблицы, которые нужно очистить при восстановлении.
        # TRUNCATE CASCADE автоматически обработает FK зависимости,
        # поэтому порядок не критичен, но перечисляем все для полноты.
        all_tables = [
            # --- Association tables ---
            'server_squad_promo_groups',
            'tariff_promo_groups',
            'payment_method_promo_groups',
            # --- Polls ---
            'poll_answers',
            'poll_responses',
            'poll_options',
            'poll_questions',
            'polls',
            # --- Wheel ---
            'wheel_spins',
            'wheel_prizes',
            'wheel_configs',
            # --- Contests ---
            'contest_attempts',
            'contest_rounds',
            'contest_templates',
            'referral_contest_virtual_participants',
            'referral_contest_events',
            'referral_contests',
            # --- Webhooks ---
            'webhook_deliveries',
            'webhooks',
            # --- Promo offers ---
            'promo_offer_logs',
            'promo_offer_templates',
            'subscription_temporary_access',
            # --- User engagement ---
            'subscription_events',
            'traffic_purchases',
            'user_promo_groups',
            'withdrawal_requests',
            # --- Support extras ---
            'ticket_notifications',
            'button_click_logs',
            # --- Payment providers ---
            'heleket_payments',
            'wata_payments',
            'platega_payments',
            'cloudpayments_payments',
            'freekassa_payments',
            'kassa_ai_payments',
            # --- Content/config ---
            'pinned_messages',
            'main_menu_buttons',
            'menu_layout_history',
            'faq_pages',
            'faq_settings',
            'privacy_policies',
            'public_offers',
            'payment_method_configs',
            # --- Support ---
            'support_audit_logs',
            'ticket_messages',
            'tickets',
            'cabinet_refresh_tokens',
            # --- Campaigns ---
            'advertising_campaign_registrations',
            'advertising_campaigns',
            # --- Subscriptions ---
            'subscription_servers',
            'sent_notifications',
            'discount_offers',
            'user_messages',
            'broadcast_history',
            'subscription_conversions',
            'referral_earnings',
            'promocode_uses',
            'yookassa_payments',
            'cryptobot_payments',
            'mulenpay_payments',
            'pal24_payments',
            'transactions',
            'welcome_texts',
            'subscriptions',
            'promocodes',
            # --- RBAC / Admin (FK → users, must be before users) ---
            'access_policies',
            'user_roles',
            'admin_audit_log',
            'admin_roles',
            # --- Channels / Partners ---
            'partner_applications',
            'required_channels',
            'user_channel_subscriptions',
            # --- Core ---
            'users',
            'promo_groups',
            'tariffs',
            'server_squads',
            'squads',
            'service_rules',
            'system_settings',
            'web_api_tokens',
            'monitoring_logs',
        ]

        # Таблицы, которые не нужно очищать если в бекапе нет данных для них
        # (чтобы сохранить существующие настройки)
        preserve_if_no_backup = {'tariffs', 'promo_groups', 'server_squads', 'squads'}

        # Фильтруем таблицы, которые нужно сохранить
        tables_to_truncate = []
        for table_name in all_tables:
            if backup_data and table_name in preserve_if_no_backup:
                if not backup_data.get(table_name):
                    logger.info('⏭️ Пропускаем очистку (нет данных в бекапе)', table_name=table_name)
                    continue
            tables_to_truncate.append(table_name)

        if not tables_to_truncate:
            return

        # TRUNCATE CASCADE requires ACCESS EXCLUSIVE locks on all affected tables
        # and can take a long time with 80+ tables. The main engine has command_timeout=30s
        # which is too short. Use a dedicated connection with extended timeouts.
        from app.database.database import DATABASE_URL

        truncate_engine = create_async_engine(
            DATABASE_URL,
            connect_args={
                'server_settings': {
                    'statement_timeout': '300000',  # 5 минут
                    'lock_timeout': '120000',  # 2 минуты ожидания блокировок
                },
                'command_timeout': 300,
            },
            poolclass=NullPool,
        )
        try:
            tables_str = ', '.join(tables_to_truncate)
            async with truncate_engine.begin() as conn:
                await conn.execute(text(f'TRUNCATE {tables_str} RESTART IDENTITY CASCADE'))
            logger.info('🗑️ Очищены все таблицы', tables_count=len(tables_to_truncate))
        except Exception as e:
            logger.error('❌ Ошибка TRUNCATE CASCADE, пробуем поштучно', error=e)
            # Fallback: поштучная очистка, каждая в отдельном соединении
            # чтобы PendingRollbackError не каскадировал на остальные таблицы
            failed_tables = []
            for table_name in tables_to_truncate:
                try:
                    async with truncate_engine.begin() as conn:
                        await conn.execute(text(f'TRUNCATE {table_name} CASCADE'))
                    logger.info('🗑️ Очищена таблица', table_name=table_name)
                except Exception as table_err:
                    logger.warning('⚠️ Не удалось очистить таблицу', table_name=table_name, error=table_err)
                    failed_tables.append(table_name)
            if failed_tables:
                logger.warning(
                    '⚠️ Не удалось очистить таблицы',
                    failed_tables=failed_tables,
                    count=len(failed_tables),
                )
        finally:
            await truncate_engine.dispose()

    async def _collect_file_snapshots(self) -> dict[str, dict[str, Any]]:
        return {}

    async def _restore_file_snapshots(self, file_snapshots: dict[str, dict[str, Any]]) -> int:
        return 0

    async def get_backup_list(self) -> list[dict[str, Any]]:
        backups = []

        try:
            for backup_file in sorted(
                await asyncio.to_thread(lambda: list(self.backup_dir.glob('backup_*'))), reverse=True
            ):
                if not await asyncio.to_thread(backup_file.is_file):
                    continue

                try:
                    metadata = {}

                    if self._is_archive_backup(backup_file):
                        mode = 'r:gz' if backup_file.suffixes and backup_file.suffixes[-1] == '.gz' else 'r'
                        with tarfile.open(backup_file, mode) as tar:
                            try:
                                member = tar.getmember('metadata.json')
                                with tar.extractfile(member) as meta_file:
                                    metadata = json_lib.load(meta_file)
                            except KeyError:
                                metadata = {}
                    else:
                        if backup_file.suffix == '.gz':
                            with gzip.open(backup_file, 'rt', encoding='utf-8') as f:
                                backup_structure = json_lib.load(f)
                        else:
                            with open(backup_file, encoding='utf-8') as f:
                                backup_structure = json_lib.load(f)
                        metadata = backup_structure.get('metadata', {})

                    file_stats = await asyncio.to_thread(backup_file.stat)

                    backup_info = {
                        'filename': backup_file.name,
                        'filepath': str(backup_file),
                        'timestamp': metadata.get(
                            'timestamp', datetime.fromtimestamp(file_stats.st_mtime, tz=UTC).isoformat()
                        ),
                        'tables_count': metadata.get(
                            'tables_count', metadata.get('database', {}).get('tables_count', 0)
                        ),
                        'total_records': metadata.get(
                            'total_records', metadata.get('database', {}).get('total_records', 0)
                        ),
                        'compressed': self._is_archive_backup(backup_file) or backup_file.suffix == '.gz',
                        'file_size_bytes': file_stats.st_size,
                        'file_size_mb': round(file_stats.st_size / 1024 / 1024, 2),
                        'created_by': metadata.get('created_by'),
                        'database_type': metadata.get(
                            'database_type', metadata.get('database', {}).get('type', 'unknown')
                        ),
                        'version': metadata.get('format_version', metadata.get('version', '1.0')),
                    }

                    backups.append(backup_info)

                except Exception as e:
                    logger.error('Ошибка чтения метаданных', backup_file=backup_file, error=e)
                    file_stats = await asyncio.to_thread(backup_file.stat)
                    backups.append(
                        {
                            'filename': backup_file.name,
                            'filepath': str(backup_file),
                            'timestamp': datetime.fromtimestamp(file_stats.st_mtime, tz=UTC).isoformat(),
                            'tables_count': '?',
                            'total_records': '?',
                            'compressed': backup_file.suffix == '.gz',
                            'file_size_bytes': file_stats.st_size,
                            'file_size_mb': round(file_stats.st_size / 1024 / 1024, 2),
                            'created_by': None,
                            'database_type': 'unknown',
                            'version': 'unknown',
                            'error': f'Ошибка чтения: {e!s}',
                        }
                    )

        except Exception as e:
            logger.error('Ошибка получения списка бекапов', error=e)

        return backups

    async def delete_backup(self, backup_filename: str) -> tuple[bool, str]:
        try:
            backup_path = await asyncio.to_thread((self.backup_dir / backup_filename).resolve)
            backup_dir_resolved = await asyncio.to_thread(self.backup_dir.resolve)
            if not str(backup_path).startswith(str(backup_dir_resolved) + os.sep):
                return False, '❌ Недопустимое имя файла бекапа'

            if not await asyncio.to_thread(backup_path.is_file):
                return False, f'❌ Файл бекапа не найден: {backup_filename}'

            await asyncio.to_thread(backup_path.unlink)
            message = f'✅ Бекап {backup_filename} удален'
            logger.info(message)

            return True, message

        except Exception as e:
            error_msg = f'❌ Ошибка удаления бекапа: {e!s}'
            logger.error(error_msg)
            return False, error_msg

    async def _cleanup_old_backups(self):
        try:
            backups = await self.get_backup_list()

            if len(backups) > self._settings.max_backups_keep:
                backups.sort(key=lambda x: x.get('timestamp', ''), reverse=True)

                for backup in backups[self._settings.max_backups_keep :]:
                    try:
                        await self.delete_backup(backup['filename'])
                        logger.info('🗑️ Удален старый бекап', backup=backup['filename'])
                    except Exception as e:
                        logger.error('Ошибка удаления старого бекапа', backup=backup['filename'], error=e)

        except Exception as e:
            logger.error('Ошибка очистки старых бекапов', error=e)

    async def get_backup_settings(self) -> BackupSettings:
        return self._settings

    async def update_backup_settings(self, **kwargs) -> bool:
        try:
            for key, value in kwargs.items():
                if hasattr(self._settings, key):
                    setattr(self._settings, key, value)

            if self._settings.auto_backup_enabled:
                await self.start_auto_backup()
            else:
                await self.stop_auto_backup()

            return True

        except Exception as e:
            logger.error('Ошибка обновления настроек бекапов', error=e)
            return False

    async def start_auto_backup(self):
        if self._auto_backup_task and not self._auto_backup_task.done():
            self._auto_backup_task.cancel()

        if self._settings.auto_backup_enabled:
            next_run = self._calculate_next_backup_datetime()
            interval = self._get_backup_interval()
            self._auto_backup_task = asyncio.create_task(self._auto_backup_loop(next_run))
            logger.info(
                '📄 Автобекапы включены, интервал: ч, ближайший запуск',
                total_seconds=interval.total_seconds() / 3600,
                next_run=next_run.strftime('%d.%m.%Y %H:%M:%S'),
            )

    async def stop_auto_backup(self):
        if self._auto_backup_task and not self._auto_backup_task.done():
            self._auto_backup_task.cancel()
            logger.info('ℹ️ Автобекапы остановлены')

    async def _auto_backup_loop(self, next_run: datetime | None = None):
        next_run = next_run or self._calculate_next_backup_datetime()
        interval = self._get_backup_interval()

        while True:
            try:
                now = datetime.now(UTC)
                delay = (next_run - now).total_seconds()

                if delay > 0:
                    logger.info(
                        '⏰ Следующий автоматический бекап запланирован на (через ч)',
                        next_run=next_run.strftime('%d.%m.%Y %H:%M:%S'),
                        delay=delay / 3600,
                    )
                    await asyncio.sleep(delay)
                else:
                    logger.info(
                        '⏰ Время автоматического бекапа уже наступило, запускаем немедленно',
                        next_run=next_run.strftime('%d.%m.%Y %H:%M:%S'),
                    )

                logger.info('📄 Запуск автоматического бекапа...')
                success, message, _ = await self.create_backup()

                if success:
                    logger.info('✅ Автобекап завершен', message=message)
                else:
                    logger.error('❌ Ошибка автобекапа', message=message)

                next_run = next_run + interval

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error('Ошибка в цикле автобекапов', error=e)
                next_run = datetime.now(UTC) + interval

    async def _send_backup_notification(self, event_type: str, message: str, file_path: str = None):
        try:
            if not settings.is_admin_notifications_enabled():
                return

            icons = {'success': '✅', 'error': '❌', 'restore_success': '🔥', 'restore_error': '❌'}

            icon = icons.get(event_type, 'ℹ️')
            safe_message = html_lib.escape(message) if 'error' in event_type else message
            notification_text = f'{icon} <b>СИСТЕМА БЕКАПОВ</b>\n\n{safe_message}'

            if file_path:
                notification_text += f'\n📁 <code>{Path(file_path).name}</code>'

            notification_text += f'\n\n⏰ <i>{datetime.now(UTC).strftime("%d.%m.%Y %H:%M:%S")}</i>'

            try:
                from app.services.admin_notification_service import AdminNotificationService

                admin_service = AdminNotificationService(self.bot)
                await admin_service._send_message(notification_text)
            except Exception as e:
                logger.error('Ошибка отправки уведомления через AdminNotificationService', error=e)

        except Exception as e:
            logger.error('Ошибка отправки уведомления о бекапе', error=e)

    async def _send_backup_file_to_chat(self, file_path: str):
        try:
            if not settings.is_backup_send_enabled():
                return

            chat_id = settings.get_backup_send_chat_id()
            if not chat_id:
                return

            password = settings.get_backup_archive_password()
            file_to_send = file_path
            temp_zip_path = None

            if password:
                temp_zip_path = await self._create_password_protected_archive(file_path, password)
                if temp_zip_path:
                    file_to_send = temp_zip_path

            caption = '📦 <b>Резервная копия</b>\n\n'
            if temp_zip_path:
                caption += '🔐 <b>Архив защищён паролем</b>\n\n'
            caption += f'⏰ <i>{datetime.now(UTC).strftime("%d.%m.%Y %H:%M:%S")}</i>'

            send_kwargs = {
                'chat_id': chat_id,
                'document': FSInputFile(file_to_send),
                'caption': caption,
                'parse_mode': 'HTML',
            }

            if settings.BACKUP_SEND_TOPIC_ID:
                send_kwargs['message_thread_id'] = settings.BACKUP_SEND_TOPIC_ID

            await self.bot.send_document(**send_kwargs)
            logger.info('Бекап отправлен в чат', chat_id=chat_id)

            if temp_zip_path and await asyncio.to_thread(Path(temp_zip_path).exists):
                try:
                    await asyncio.to_thread(Path(temp_zip_path).unlink)
                except Exception as cleanup_error:
                    logger.warning('Не удалось удалить временный архив', cleanup_error=cleanup_error)

        except Exception as e:
            logger.error('Ошибка отправки бекапа в чат', error=e)

    async def _create_password_protected_archive(self, file_path: str, password: str) -> str | None:
        try:
            source_path = Path(file_path)
            if not await asyncio.to_thread(source_path.exists):
                logger.error('Исходный файл бекапа не найден', file_path=file_path)
                return None

            zip_filename = source_path.stem + '.zip'
            zip_path = source_path.parent / zip_filename

            def create_zip():
                with pyzipper.AESZipFile(
                    zip_path, 'w', compression=pyzipper.ZIP_DEFLATED, encryption=pyzipper.WZ_AES
                ) as zf:
                    zf.setpassword(password.encode('utf-8'))
                    zf.write(source_path, arcname=source_path.name)

            await asyncio.to_thread(create_zip)
            logger.info('Создан защищённый паролем архив', zip_path=zip_path)
            return str(zip_path)

        except Exception as e:
            logger.error('Ошибка создания защищённого архива', error=e)
            return None


backup_service = BackupService()
