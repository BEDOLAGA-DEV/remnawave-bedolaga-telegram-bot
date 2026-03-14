"""Сервис ротации логов с отправкой в Telegram.

Функционал:
- Ежедневная ротация в настроенное время (по умолчанию 00:00)
- Разделение по уровням: info.log, warning.log, error.log
- Отдельный лог платежей: payments.log
- Архивирование всех логов за день в один tar.gz
- Отправка архива в Telegram-канал
- Очистка архивов старше N дней
"""

from __future__ import annotations

import asyncio
import logging
import tarfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import structlog
from aiogram import Bot
from aiogram.types import FSInputFile

from app.config import settings
from app.utils.timezone import get_local_timezone


logger = structlog.get_logger(__name__)


@dataclass
class LogRotationStatus:
    """Статус сервиса ротации логов."""

    enabled: bool
    running: bool
    rotation_time: str
    keep_days: int
    send_to_telegram: bool
    next_rotation: str | None
    log_dir: str
    archive_count: int


class LogRotationService:
    """Сервис ежедневной ротации и архивации логов."""

    def __init__(self, bot: Bot | None = None):
        self.bot = bot
        self._rotation_task: asyncio.Task | None = None
        self._running = False
        self._handlers: list[logging.Handler] = []

        # Пути
        self.log_dir = Path(settings.LOG_DIR).resolve()
        self.current_dir = self.log_dir / 'current'
        self.archive_dir = self.log_dir / 'archive'

    @property
    def log_files(self) -> dict[str, Path]:
        """Пути к текущим лог-файлам."""
        return {
            'bot': self.current_dir / 'bot.log',
            'info': self.current_dir / settings.LOG_INFO_FILE,
            'warning': self.current_dir / settings.LOG_WARNING_FILE,
            'error': self.current_dir / settings.LOG_ERROR_FILE,
            'payments': self.current_dir / settings.LOG_PAYMENTS_FILE,
        }

    def set_bot(self, bot: Bot) -> None:
        """Установить экземпляр бота для отправки логов."""
        self.bot = bot

    def register_handlers(self, handlers: list[logging.Handler]) -> None:
        """Зарегистрировать хэндлеры для управления при ротации."""
        self._handlers = handlers

    async def initialize(self) -> None:
        """Создать необходимые директории."""
        await asyncio.to_thread(lambda: self.current_dir.mkdir(parents=True, exist_ok=True))
        await asyncio.to_thread(lambda: self.archive_dir.mkdir(parents=True, exist_ok=True))

    async def start(self) -> None:
        """Запустить сервис ротации."""
        if self._running:
            return

        self._running = True
        self._rotation_task = asyncio.create_task(self._rotation_loop())
        logger.info('Сервис ротации логов запущен')

    async def stop(self) -> None:
        """Остановить сервис ротации."""
        self._running = False
        if self._rotation_task and not self._rotation_task.done():
            self._rotation_task.cancel()
            try:
                await self._rotation_task
            except asyncio.CancelledError:
                pass
        logger.info('Сервис ротации логов остановлен')

    def is_running(self) -> bool:
        """Проверить, запущен ли сервис."""
        return self._running

    async def _rotation_loop(self) -> None:
        """Основной цикл ожидания времени ротации."""
        while self._running:
            next_rotation = self._calculate_next_rotation_time()
            now = datetime.now(get_local_timezone())
            wait_seconds = (next_rotation - now).total_seconds()

            if wait_seconds > 0:
                logger.info(
                    'Следующая ротация логов: (через часов)',
                    next_rotation=next_rotation.strftime('%Y-%m-%d %H:%M'),
                    wait_seconds=wait_seconds / 3600,
                )
                try:
                    await asyncio.sleep(wait_seconds)
                except asyncio.CancelledError:
                    break

            if self._running:
                await self.rotate_logs()

    def _calculate_next_rotation_time(self) -> datetime:
        """Вычислить время следующей ротации."""
        now = datetime.now(get_local_timezone())

        # Парсим время ротации
        time_str = settings.LOG_ROTATION_TIME
        try:
            hours, minutes = map(int, time_str.split(':'))
        except ValueError:
            hours, minutes = 0, 0
            logger.warning("Некорректное LOG_ROTATION_TIME='', используем 00:00", time_str=time_str)

        next_rotation = now.replace(hour=hours, minute=minutes, second=0, microsecond=0)

        # Если время уже прошло сегодня, ротация завтра
        if next_rotation <= now:
            next_rotation += timedelta(days=1)

        return next_rotation

    async def rotate_logs(self) -> tuple[bool, str]:
        """Выполнить ротацию логов.

        Создаёт один архив logs_YYYY-MM-DD.tar.gz со всеми лог-файлами за день.

        Returns:
            Tuple[bool, str]: (успех, сообщение)
        """
        try:
            logger.info('Начинаем ротацию логов...')

            # Дата для архива (вчера, т.к. логи были за предыдущие сутки)
            yesterday = (datetime.now(get_local_timezone()) - timedelta(days=1)).strftime('%Y-%m-%d')

            # Сбрасываем буферы хэндлеров перед архивацией
            for handler in self._handlers:
                try:
                    handler.flush()
                except Exception:
                    pass

            # Собираем файлы для архивации
            files_to_archive: list[tuple[Path, str]] = []
            for name, log_path in self.log_files.items():
                if await asyncio.to_thread(log_path.exists) and (await asyncio.to_thread(log_path.stat)).st_size > 0:
                    files_to_archive.append((log_path, f'{name}.log'))

            if not files_to_archive:
                message = 'Нет логов для архивации'
                logger.info(message)
                return True, message

            # Создаём один архив со всеми логами
            archive_path = await self._create_archive(files_to_archive, yesterday)

            if archive_path:
                # Очищаем текущие лог-файлы
                for log_path, _ in files_to_archive:
                    await asyncio.to_thread(log_path.write_text, '')

                # Очистка старых архивов
                await self._cleanup_old_archives()

                # Отправка в Telegram
                if settings.LOG_ROTATION_SEND_TO_TELEGRAM and self.bot:
                    await self._send_logs_to_telegram(archive_path, yesterday)

                message = f'Ротация логов завершена. Архив: {archive_path.name}'
                logger.info(message)
                return True, message
            message = 'Ошибка создания архива логов'
            logger.error(message)
            return False, message

        except Exception as error:
            message = f'Ошибка ротации логов: {error}'
            logger.error(message, exc_info=True)
            return False, message

    async def _create_archive(
        self,
        files: list[tuple[Path, str]],
        date_str: str,
    ) -> Path | None:
        """Создать архив со всеми логами за день.

        Args:
            files: список (путь к файлу, имя в архиве)
            date_str: дата в формате YYYY-MM-DD

        Returns:
            Путь к созданному архиву или None при ошибке
        """
        try:
            if settings.LOG_ROTATION_COMPRESS:
                archive_name = f'logs_{date_str}.tar.gz'
                mode = 'w:gz'
            else:
                archive_name = f'logs_{date_str}.tar'
                mode = 'w'

            archive_path = self.archive_dir / archive_name

            def _create_tar():
                with tarfile.open(archive_path, mode) as tar:
                    for file_path, arcname in files:
                        tar.add(file_path, arcname=arcname)

            await asyncio.to_thread(_create_tar)
            logger.debug('Создан архив', archive_path=archive_path)
            return archive_path

        except Exception as error:
            logger.error('Ошибка создания архива', error=error)
            return None

    async def _cleanup_old_archives(self) -> None:
        """Удалить архивы старше LOG_ROTATION_KEEP_DAYS."""
        keep_days = settings.LOG_ROTATION_KEEP_DAYS
        cutoff_date = datetime.now(get_local_timezone()) - timedelta(days=keep_days)

        if not await asyncio.to_thread(self.archive_dir.exists):
            return

        # Ищем файлы вида logs_YYYY-MM-DD.tar.gz или logs_YYYY-MM-DD.tar
        for archive_file in await asyncio.to_thread(lambda: list(self.archive_dir.iterdir())):
            if not await asyncio.to_thread(archive_file.is_file):
                continue

            # Извлекаем дату из имени файла logs_YYYY-MM-DD.tar.gz
            name = archive_file.name
            if not name.startswith('logs_'):
                continue

            try:
                # logs_2025-01-26.tar.gz -> 2025-01-26
                date_part = name.replace('logs_', '').replace('.tar.gz', '').replace('.tar', '')
                file_date = datetime.strptime(date_part, '%Y-%m-%d')
                file_date = file_date.replace(tzinfo=get_local_timezone())

                if file_date < cutoff_date:
                    await asyncio.to_thread(archive_file.unlink)
                    logger.info('Удален старый архив логов', archive_file_name=archive_file.name)
            except ValueError:
                # Пропускаем файлы с некорректным форматом имени
                pass

    async def _send_logs_to_telegram(
        self,
        archive_path: Path,
        date_str: str,
    ) -> None:
        """Отправить архив логов в Telegram."""
        chat_id = settings.get_log_rotation_chat_id()
        if not chat_id:
            logger.warning('LOG_ROTATION_CHAT_ID не задан, пропускаем отправку')
            return

        topic_id = settings.get_log_rotation_topic_id()

        try:
            file_size_kb = (await asyncio.to_thread(archive_path.stat)).st_size / 1024
            caption = (
                f'<b>Логи бота</b>\n'
                f'Дата: {date_str}\n'
                f'Файл: <code>{archive_path.name}</code>\n'
                f'Размер: {file_size_kb:.1f} KB'
            )

            send_kwargs = {
                'chat_id': chat_id,
                'document': FSInputFile(archive_path),
                'caption': caption,
                'parse_mode': 'HTML',
            }

            if topic_id:
                send_kwargs['message_thread_id'] = topic_id

            await self.bot.send_document(**send_kwargs)
            logger.info('Архив логов отправлен', archive_path_name=archive_path.name)

        except Exception as error:
            logger.error('Ошибка отправки архива', archive_path_name=archive_path.name, error=error)

    # === Ручные операции ===

    async def force_rotate(self) -> tuple[bool, str]:
        """Принудительная ротация (для админ-команды)."""
        return await self.rotate_logs()

    def get_status(self) -> LogRotationStatus:
        """Получить статус сервиса."""
        archive_count = 0
        if self.archive_dir.exists():
            # Считаем файлы logs_*.tar.gz или logs_*.tar
            archive_count = len([f for f in self.archive_dir.iterdir() if f.is_file() and f.name.startswith('logs_')])

        next_rotation = None
        if self._running:
            next_rotation = self._calculate_next_rotation_time().isoformat()

        return LogRotationStatus(
            enabled=settings.is_log_rotation_enabled(),
            running=self._running,
            rotation_time=settings.LOG_ROTATION_TIME,
            keep_days=settings.LOG_ROTATION_KEEP_DAYS,
            send_to_telegram=settings.LOG_ROTATION_SEND_TO_TELEGRAM,
            next_rotation=next_rotation,
            log_dir=str(self.log_dir),
            archive_count=archive_count,
        )


# Глобальный экземпляр сервиса
log_rotation_service = LogRotationService()
