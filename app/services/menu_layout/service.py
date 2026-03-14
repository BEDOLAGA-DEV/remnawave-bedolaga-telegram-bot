"""Основной сервис конструктора меню."""

from __future__ import annotations

import asyncio
import copy
import json
from datetime import UTC, datetime
from typing import Any

import structlog
from aiogram import types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.system_setting import upsert_system_setting
from app.database.models import SystemSetting
from app.localization.texts import get_texts

from .constants import (
    AVAILABLE_CALLBACKS,
    BUILTIN_BUTTONS_INFO,
    DEFAULT_MENU_CONFIG,
    DYNAMIC_PLACEHOLDERS,
    MENU_LAYOUT_CONFIG_KEY,
)
from .context import MenuContext
from .history_service import MenuLayoutHistoryService
from .stats_service import MenuLayoutStatsService


logger = structlog.get_logger(__name__)


class MenuLayoutService:
    """Сервис для управления конфигурацией меню."""

    _cache: dict[str, Any] | None = None
    _cache_updated_at: datetime | None = None
    _lock: asyncio.Lock = asyncio.Lock()

    # --- Управление кешем ---

    @classmethod
    def invalidate_cache(cls) -> None:
        """Инвалидировать кеш конфигурации."""
        cls._cache = None
        cls._cache_updated_at = None

    # --- Получение констант и информации ---

    @classmethod
    def get_default_config(cls) -> dict[str, Any]:
        """Получить дефолтную конфигурацию."""
        return copy.deepcopy(DEFAULT_MENU_CONFIG)

    @classmethod
    def get_builtin_buttons_info(cls) -> list[dict[str, Any]]:
        """Получить информацию о встроенных кнопках."""
        return BUILTIN_BUTTONS_INFO.copy()

    @classmethod
    async def get_available_callbacks(cls, db: AsyncSession) -> list[dict[str, Any]]:
        """Получить список всех доступных callback_data."""
        config = await cls.get_config(db)
        buttons_in_menu = set(config.get('buttons', {}).keys())

        # Добавляем встроенные callback_data
        builtin_callbacks = {btn['callback_data'] for btn in BUILTIN_BUTTONS_INFO}

        result = []
        for cb in AVAILABLE_CALLBACKS:
            is_in_menu = cb['callback_data'] in buttons_in_menu or cb['callback_data'] in builtin_callbacks
            result.append(
                {
                    **cb,
                    'is_in_menu': is_in_menu,
                    'default_text': cb.get('text'),
                    'default_icon': cb.get('icon'),
                }
            )

        return result

    @classmethod
    def get_dynamic_placeholders(cls) -> list[dict[str, Any]]:
        """Получить список доступных динамических плейсхолдеров."""
        return DYNAMIC_PLACEHOLDERS.copy()

    # --- Экспорт/импорт ---

    @classmethod
    async def export_config(cls, db: AsyncSession) -> dict[str, Any]:
        """Экспортировать конфигурацию меню."""
        config = await cls.get_config(db)
        return {
            'version': config.get('version', 1),
            'rows': config.get('rows', []),
            'buttons': config.get('buttons', {}),
            'exported_at': datetime.now(UTC).isoformat(),
        }

    @classmethod
    async def import_config(
        cls,
        db: AsyncSession,
        import_data: dict[str, Any],
        merge_mode: str = 'replace',
    ) -> dict[str, Any]:
        """Импортировать конфигурацию меню."""
        warnings = []

        if merge_mode == 'replace':
            # Полная замена
            new_config = {
                'version': import_data.get('version', 1),
                'rows': import_data.get('rows', []),
                'buttons': import_data.get('buttons', {}),
            }
        else:
            # Объединение
            current_config = await cls.get_config(db)
            new_config = current_config.copy()

            # Добавляем новые кнопки (не перезаписываем существующие)
            for btn_id, btn_config in import_data.get('buttons', {}).items():
                if btn_id not in new_config['buttons']:
                    new_config['buttons'][btn_id] = btn_config
                else:
                    warnings.append(f"Button '{btn_id}' already exists, skipped")

            # Добавляем новые строки
            existing_row_ids = {row['id'] for row in new_config.get('rows', [])}
            for row in import_data.get('rows', []):
                if row['id'] not in existing_row_ids:
                    new_config['rows'].append(row)
                else:
                    warnings.append(f"Row '{row['id']}' already exists, skipped")

        await cls.save_config(db, new_config)

        return {
            'success': True,
            'imported_rows': len(import_data.get('rows', [])),
            'imported_buttons': len(import_data.get('buttons', {})),
            'warnings': warnings,
        }

    # --- Валидация ---

    @classmethod
    def validate_config(cls, config: dict[str, Any]) -> dict[str, Any]:
        """Валидировать конфигурацию меню."""
        errors = []
        warnings = []

        rows = config.get('rows', [])
        buttons = config.get('buttons', {})

        # Проверяем уникальность ID строк
        row_ids = [row.get('id') for row in rows]
        duplicate_rows = [rid for rid in row_ids if row_ids.count(rid) > 1]
        if duplicate_rows:
            errors.append(
                {
                    'field': 'rows',
                    'message': f'Duplicate row IDs: {set(duplicate_rows)}',
                    'severity': 'error',
                }
            )

        # Проверяем ссылки на кнопки
        for row in rows:
            for btn_id in row.get('buttons', []):
                if btn_id not in buttons:
                    errors.append(
                        {
                            'field': f'rows.{row.get("id")}.buttons',
                            'message': f"Button '{btn_id}' not found",
                            'severity': 'error',
                        }
                    )

        # Проверяем пустые строки
        for row in rows:
            if not row.get('buttons'):
                warnings.append(
                    {
                        'field': f'rows.{row.get("id")}',
                        'message': 'Row has no buttons',
                        'severity': 'warning',
                    }
                )

        # Проверяем отключенные кнопки
        disabled_count = sum(1 for btn in buttons.values() if not btn.get('enabled', True))
        if disabled_count > 0:
            warnings.append(
                {
                    'field': 'buttons',
                    'message': f'{disabled_count} buttons are disabled',
                    'severity': 'warning',
                }
            )

        return {
            'is_valid': len(errors) == 0,
            'errors': errors,
            'warnings': warnings,
        }

    # --- Работа с конфигурацией ---

    @classmethod
    async def get_config(cls, db: AsyncSession) -> dict[str, Any]:
        """Получить конфигурацию меню."""
        if cls._cache is not None:
            return cls._cache

        async with cls._lock:
            if cls._cache is not None:
                return cls._cache

            result = await db.execute(select(SystemSetting).where(SystemSetting.key == MENU_LAYOUT_CONFIG_KEY))
            setting = result.scalar_one_or_none()

            if setting and setting.value:
                try:
                    cls._cache = json.loads(setting.value)
                    cls._cache_updated_at = setting.updated_at
                except json.JSONDecodeError:
                    logger.warning('Invalid menu layout config JSON, using default')
                    cls._cache = cls.get_default_config()
                    cls._cache_updated_at = None
            else:
                cls._cache = cls.get_default_config()
                cls._cache_updated_at = None

            return cls._cache

    @classmethod
    async def get_config_updated_at(cls, db: AsyncSession) -> datetime | None:
        """Получить время последнего обновления конфигурации."""
        await cls.get_config(db)  # Ensure cache is loaded
        return cls._cache_updated_at

    @classmethod
    async def save_config(cls, db: AsyncSession, config: dict[str, Any]) -> None:
        """Сохранить конфигурацию меню."""
        config_json = json.dumps(config, ensure_ascii=False, indent=2)
        await upsert_system_setting(
            db,
            MENU_LAYOUT_CONFIG_KEY,
            config_json,
            description='Конфигурация конструктора меню',
        )
        await db.commit()
        cls.invalidate_cache()

    @classmethod
    async def reset_to_default(cls, db: AsyncSession) -> dict[str, Any]:
        """Сбросить конфигурацию к дефолтной."""
        default_config = cls.get_default_config()
        await cls.save_config(db, default_config)
        return default_config

    # --- Обновление кнопок ---

    @classmethod
    async def update_button(
        cls,
        db: AsyncSession,
        button_id: str,
        updates: dict[str, Any],
    ) -> dict[str, Any]:
        """Обновить конфигурацию кнопки."""
        config = await cls.get_config(db)
        config = config.copy()
        buttons = config.get('buttons', {})

        # Улучшенное определение кнопки connect для разных форматов ID
        actual_button_id = button_id
        if button_id not in buttons:
            # Пробуем найти кнопку connect по разным форматам
            if 'connect' in button_id.lower():
                # Проверяем разные варианты: connect, callback:connect и т.д.
                for key in buttons.keys():
                    if key == 'connect' or buttons[key].get('builtin_id') == 'connect':
                        actual_button_id = key
                        logger.info(
                            '🔗 Найдена кнопка connect по ID', button_id=button_id, actual_button_id=actual_button_id
                        )
                        break
                else:
                    # Если не нашли, пробуем найти по builtin_id
                    for key, button in buttons.items():
                        if (
                            button.get('builtin_id') == 'connect'
                            or 'connect' in str(button.get('builtin_id', '')).lower()
                        ):
                            actual_button_id = key
                            logger.info(
                                '🔗 Найдена кнопка connect по builtin_id',
                                button_id=button_id,
                                actual_button_id=actual_button_id,
                            )
                            break
                    else:
                        raise KeyError(f"Button '{button_id}' not found")

        if actual_button_id not in buttons:
            raise KeyError(f"Button '{actual_button_id}' not found")

        button = buttons[actual_button_id].copy()

        # Логирование для отладки
        if 'connect' in actual_button_id.lower() or button.get('builtin_id') == 'connect':
            logger.info(
                '🔗 Обновление кнопки connect (ID: ): open_mode=, action=, webapp_url',
                actual_button_id=actual_button_id,
                get=updates.get('open_mode'),
                get_2=updates.get('action'),
                get_3=updates.get('webapp_url'),
            )

        # Применяем обновления
        if 'text' in updates and updates['text'] is not None:
            button['text'] = updates['text']
        if 'icon' in updates:
            button['icon'] = updates['icon']
        if 'enabled' in updates and updates['enabled'] is not None:
            button['enabled'] = updates['enabled']
        if 'visibility' in updates and updates['visibility'] is not None:
            button['visibility'] = updates['visibility']
        if 'conditions' in updates:
            button['conditions'] = updates['conditions']
        if 'dynamic_text' in updates and updates['dynamic_text'] is not None:
            button['dynamic_text'] = updates['dynamic_text']
        if 'description' in updates:
            button['description'] = updates['description']
        if 'sort_order' in updates:
            button['sort_order'] = updates['sort_order']
        if 'action' in updates and updates['action'] is not None:
            # Для URL/MiniApp/callback кнопок можно менять action
            if button.get('type') in ('url', 'mini_app', 'callback') or (
                button.get('type') == 'builtin' and updates.get('open_mode') == 'direct'
            ):
                button['action'] = updates['action']
        if 'open_mode' in updates and updates['open_mode'] is not None:
            button['open_mode'] = updates['open_mode']
        if 'webapp_url' in updates:
            button['webapp_url'] = updates['webapp_url']

        buttons[actual_button_id] = button
        config['buttons'] = buttons

        await cls.save_config(db, config)
        return button

    # --- Управление строками ---

    @classmethod
    async def reorder_rows(
        cls,
        db: AsyncSession,
        ordered_ids: list[str],
    ) -> list[dict[str, Any]]:
        """Изменить порядок строк."""
        config = await cls.get_config(db)
        config = config.copy()
        rows = config.get('rows', [])

        # Создаем словарь для быстрого поиска
        rows_map = {row['id']: row for row in rows}

        # Проверяем что все ID существуют
        for row_id in ordered_ids:
            if row_id not in rows_map:
                raise KeyError(f"Row '{row_id}' not found")

        # Переупорядочиваем
        new_rows = [rows_map[row_id] for row_id in ordered_ids]

        # Добавляем строки которые не были в списке (в конец)
        for row in rows:
            if row['id'] not in ordered_ids:
                new_rows.append(row)

        config['rows'] = new_rows
        await cls.save_config(db, config)
        return new_rows

    @classmethod
    async def add_row(
        cls,
        db: AsyncSession,
        row_config: dict[str, Any],
        position: int | None = None,
    ) -> dict[str, Any]:
        """Добавить новую строку."""
        config = await cls.get_config(db)
        config = config.copy()
        rows = config.get('rows', [])

        # Проверяем уникальность ID
        existing_ids = {row['id'] for row in rows}
        if row_config['id'] in existing_ids:
            raise ValueError(f"Row with id '{row_config['id']}' already exists")

        new_row = {
            'id': row_config['id'],
            'buttons': row_config.get('buttons', []),
            'conditions': row_config.get('conditions'),
            'max_per_row': row_config.get('max_per_row', 2),
        }

        if position is not None and 0 <= position < len(rows):
            rows.insert(position, new_row)
        else:
            rows.append(new_row)

        config['rows'] = rows
        await cls.save_config(db, config)
        return new_row

    @classmethod
    async def delete_row(cls, db: AsyncSession, row_id: str) -> None:
        """Удалить строку."""
        config = await cls.get_config(db)
        config = config.copy()
        rows = config.get('rows', [])

        new_rows = [row for row in rows if row['id'] != row_id]
        if len(new_rows) == len(rows):
            raise KeyError(f"Row '{row_id}' not found")

        config['rows'] = new_rows
        await cls.save_config(db, config)

    # --- Кастомные кнопки ---

    @classmethod
    async def add_custom_button(
        cls,
        db: AsyncSession,
        button_id: str,
        button_config: dict[str, Any],
        row_id: str | None = None,
    ) -> dict[str, Any]:
        """Добавить кастомную кнопку."""
        config = await cls.get_config(db)
        config = config.copy()
        buttons = config.get('buttons', {})

        if button_id in buttons:
            raise ValueError(f"Button with id '{button_id}' already exists")

        # URL, MiniApp и callback кнопки могут быть добавлены
        allowed_types = ('url', 'mini_app', 'callback')
        if button_config.get('type') not in allowed_types:
            raise ValueError(f'Only {allowed_types} buttons can be added')

        buttons[button_id] = {
            'type': button_config['type'],
            'builtin_id': None,
            'text': button_config['text'],
            'icon': button_config.get('icon'),
            'action': button_config['action'],
            'enabled': button_config.get('enabled', True),
            'visibility': button_config.get('visibility', 'all'),
            'conditions': button_config.get('conditions'),
            'dynamic_text': button_config.get('dynamic_text', False),
            'description': button_config.get('description'),
        }

        config['buttons'] = buttons

        # Добавляем в строку если указана
        if row_id:
            rows = config.get('rows', [])
            for row in rows:
                if row['id'] == row_id:
                    row['buttons'].append(button_id)
                    break

        await cls.save_config(db, config)
        return buttons[button_id]

    @classmethod
    async def delete_custom_button(cls, db: AsyncSession, button_id: str) -> None:
        """Удалить кастомную кнопку."""
        config = await cls.get_config(db)
        config = config.copy()
        buttons = config.get('buttons', {})

        if button_id not in buttons:
            raise KeyError(f"Button '{button_id}' not found")

        # Нельзя удалять встроенные кнопки
        if buttons[button_id].get('type') == 'builtin':
            raise ValueError('Cannot delete builtin buttons')

        del buttons[button_id]
        config['buttons'] = buttons

        # Удаляем из всех строк
        rows = config.get('rows', [])
        for row in rows:
            if button_id in row.get('buttons', []):
                row['buttons'].remove(button_id)

        await cls.save_config(db, config)

    # --- Перемещение кнопок ---

    @classmethod
    def _find_button_row(cls, config: dict[str, Any], button_id: str) -> int | None:
        """Найти индекс строки содержащей кнопку."""
        rows = config.get('rows', [])
        for i, row in enumerate(rows):
            if button_id in row.get('buttons', []):
                return i
        return None

    @classmethod
    async def move_button_up(cls, db: AsyncSession, button_id: str) -> dict[str, Any]:
        """Переместить кнопку на строку выше."""
        config = await cls.get_config(db)
        config = config.copy()
        rows = config.get('rows', [])

        current_row_idx = cls._find_button_row(config, button_id)
        if current_row_idx is None:
            raise KeyError(f"Button '{button_id}' not found in any row")

        if current_row_idx == 0:
            raise ValueError('Button is already in the top row')

        # Удаляем из текущей строки
        rows[current_row_idx]['buttons'].remove(button_id)

        # Добавляем в строку выше
        rows[current_row_idx - 1]['buttons'].append(button_id)

        # Удаляем пустые строки
        config['rows'] = [row for row in rows if row.get('buttons')]

        await cls.save_config(db, config)
        return {'button_id': button_id, 'new_row_index': current_row_idx - 1}

    @classmethod
    async def move_button_down(cls, db: AsyncSession, button_id: str) -> dict[str, Any]:
        """Переместить кнопку на строку ниже."""
        config = await cls.get_config(db)
        config = config.copy()
        rows = config.get('rows', [])

        current_row_idx = cls._find_button_row(config, button_id)
        if current_row_idx is None:
            raise KeyError(f"Button '{button_id}' not found in any row")

        if current_row_idx >= len(rows) - 1:
            raise ValueError('Button is already in the bottom row')

        # Удаляем из текущей строки
        rows[current_row_idx]['buttons'].remove(button_id)

        # Добавляем в строку ниже
        rows[current_row_idx + 1]['buttons'].append(button_id)

        # Удаляем пустые строки
        config['rows'] = [row for row in rows if row.get('buttons')]

        await cls.save_config(db, config)
        return {'button_id': button_id, 'new_row_index': current_row_idx + 1}

    @classmethod
    async def move_button_to_row(
        cls,
        db: AsyncSession,
        button_id: str,
        target_row_id: str,
        position: int | None = None,
    ) -> dict[str, Any]:
        """Переместить кнопку в указанную строку."""
        config = await cls.get_config(db)
        config = config.copy()
        rows = config.get('rows', [])

        # Находим целевую строку
        target_row_idx = None
        for i, row in enumerate(rows):
            if row['id'] == target_row_id:
                target_row_idx = i
                break

        if target_row_idx is None:
            raise KeyError(f"Row '{target_row_id}' not found")

        # Удаляем кнопку из текущей строки
        current_row_idx = cls._find_button_row(config, button_id)
        if current_row_idx is not None:
            rows[current_row_idx]['buttons'].remove(button_id)

        # Добавляем в целевую строку
        target_buttons = rows[target_row_idx]['buttons']
        if position is not None and 0 <= position <= len(target_buttons):
            target_buttons.insert(position, button_id)
        else:
            target_buttons.append(button_id)

        # Удаляем пустые строки
        config['rows'] = [row for row in rows if row.get('buttons')]

        await cls.save_config(db, config)
        return {'button_id': button_id, 'target_row_id': target_row_id, 'position': position}

    @classmethod
    async def reorder_buttons_in_row(
        cls,
        db: AsyncSession,
        row_id: str,
        ordered_button_ids: list[str],
    ) -> dict[str, Any]:
        """Изменить порядок кнопок внутри строки."""
        config = await cls.get_config(db)
        config = config.copy()
        rows = config.get('rows', [])

        # Находим строку
        target_row = None
        for row in rows:
            if row['id'] == row_id:
                target_row = row
                break

        if target_row is None:
            raise KeyError(f"Row '{row_id}' not found")

        # Проверяем что все кнопки принадлежат строке
        current_buttons = set(target_row['buttons'])
        ordered_buttons = set(ordered_button_ids)

        if current_buttons != ordered_buttons:
            missing = current_buttons - ordered_buttons
            extra = ordered_buttons - current_buttons
            errors = []
            if missing:
                errors.append(f'missing: {missing}')
            if extra:
                errors.append(f'extra: {extra}')
            raise ValueError(f'Button mismatch: {", ".join(errors)}')

        target_row['buttons'] = ordered_button_ids

        await cls.save_config(db, config)
        return {'row_id': row_id, 'buttons': ordered_button_ids}

    @classmethod
    async def swap_buttons(
        cls,
        db: AsyncSession,
        button_id_1: str,
        button_id_2: str,
    ) -> dict[str, Any]:
        """Поменять две кнопки местами."""
        config = await cls.get_config(db)
        config = config.copy()
        rows = config.get('rows', [])

        # Находим позиции обеих кнопок
        pos1 = None
        pos2 = None

        for row_idx, row in enumerate(rows):
            buttons = row.get('buttons', [])
            for btn_idx, btn_id in enumerate(buttons):
                if btn_id == button_id_1:
                    pos1 = (row_idx, btn_idx)
                elif btn_id == button_id_2:
                    pos2 = (row_idx, btn_idx)

        if pos1 is None:
            raise KeyError(f"Button '{button_id_1}' not found")
        if pos2 is None:
            raise KeyError(f"Button '{button_id_2}' not found")

        # Меняем местами
        rows[pos1[0]]['buttons'][pos1[1]] = button_id_2
        rows[pos2[0]]['buttons'][pos2[1]] = button_id_1

        await cls.save_config(db, config)
        return {
            'button_1': {'id': button_id_1, 'new_row': pos2[0], 'new_position': pos2[1]},
            'button_2': {'id': button_id_2, 'new_row': pos1[0], 'new_position': pos1[1]},
        }

    # --- Проверка условий ---

    @classmethod
    def _evaluate_conditions(
        cls,
        conditions: dict[str, Any] | None,
        context: MenuContext,
    ) -> bool:
        """Проверить условия показа."""
        if not conditions:
            return True

        # has_active_subscription
        if conditions.get('has_active_subscription') is True:
            if not context.has_active_subscription:
                return False

        # subscription_is_active
        if conditions.get('subscription_is_active') is True:
            if not context.subscription_is_active:
                return False

        # has_traffic_limit - подписка с лимитом трафика
        if conditions.get('has_traffic_limit') is True:
            if not context.subscription:
                return False
            traffic_limit = getattr(context.subscription, 'traffic_limit_gb', 0)
            is_trial = getattr(context.subscription, 'is_trial', False)
            if is_trial or traffic_limit <= 0:
                return False

        # traffic_topup_enabled - функция докупки трафика включена
        if conditions.get('traffic_topup_enabled') is True:
            if not settings.is_traffic_topup_enabled():
                return False
            # В режиме тарифов докупка трафика недоступна
            if settings.is_tariffs_mode():
                return False

        # is_admin
        if conditions.get('is_admin') is True:
            if not context.is_admin:
                return False

        # is_moderator
        if conditions.get('is_moderator') is True:
            if context.is_admin:  # Админ не показывает кнопку модератора
                return False
            if not context.is_moderator:
                return False

        # referral_enabled
        if conditions.get('referral_enabled') is True:
            if not settings.is_referral_program_enabled():
                return False

        # contests_visible
        if conditions.get('contests_visible') is True:
            if not settings.CONTESTS_BUTTON_VISIBLE:
                return False

        # support_enabled
        if conditions.get('support_enabled') is True:
            try:
                from app.services.support_settings_service import SupportSettingsService

                if not SupportSettingsService.is_support_menu_enabled():
                    return False
            except Exception:
                if not settings.SUPPORT_MENU_ENABLED:
                    return False

        # language_selection_enabled
        if conditions.get('language_selection_enabled') is True:
            if not settings.is_language_selection_enabled():
                return False

        # happ_enabled
        if conditions.get('happ_enabled') is True:
            if not settings.is_happ_download_button_enabled():
                return False

        # simple_subscription_enabled
        if conditions.get('simple_subscription_enabled') is True:
            if not settings.SIMPLE_SUBSCRIPTION_ENABLED:
                return False

        # show_trial
        if conditions.get('show_trial') is True:
            if context.has_had_paid_subscription or context.has_active_subscription:
                return False

        # show_buy
        if conditions.get('show_buy') is True:
            if context.has_active_subscription and context.subscription_is_active:
                return False

        # has_saved_cart
        if conditions.get('has_saved_cart') is True:
            if not context.has_saved_cart and not context.show_resume_checkout:
                return False

        # --- Расширенные условия ---

        # min_balance_kopeks
        min_balance = conditions.get('min_balance_kopeks')
        if min_balance is not None:
            if context.balance_kopeks < min_balance:
                return False

        # max_balance_kopeks
        max_balance = conditions.get('max_balance_kopeks')
        if max_balance is not None:
            if context.balance_kopeks > max_balance:
                return False

        # min_registration_days
        min_reg_days = conditions.get('min_registration_days')
        if min_reg_days is not None:
            if context.registration_days < min_reg_days:
                return False

        # max_registration_days
        max_reg_days = conditions.get('max_registration_days')
        if max_reg_days is not None:
            if context.registration_days > max_reg_days:
                return False

        # min_referrals
        min_refs = conditions.get('min_referrals')
        if min_refs is not None:
            if context.referral_count < min_refs:
                return False

        # has_referrals
        if conditions.get('has_referrals') is True:
            if context.referral_count <= 0:
                return False

        # promo_group_ids - пользователь должен быть в одной из указанных групп
        promo_groups = conditions.get('promo_group_ids')
        if promo_groups and isinstance(promo_groups, list):
            if context.promo_group_id not in promo_groups:
                return False

        # exclude_promo_group_ids - исключить пользователей из этих групп
        exclude_groups = conditions.get('exclude_promo_group_ids')
        if exclude_groups and isinstance(exclude_groups, list):
            if context.promo_group_id in exclude_groups:
                return False

        # has_subscription_days_left - минимум дней до окончания
        min_sub_days = conditions.get('has_subscription_days_left')
        if min_sub_days is not None:
            if context.subscription_days < min_sub_days:
                return False

        # max_subscription_days_left
        max_sub_days = conditions.get('max_subscription_days_left')
        if max_sub_days is not None:
            if context.subscription_days > max_sub_days:
                return False

        # is_trial_user
        if conditions.get('is_trial_user') is True:
            if not context.subscription:
                return False
            is_trial = getattr(context.subscription, 'is_trial', False)
            if not is_trial:
                return False

        # has_autopay
        if conditions.get('has_autopay') is True:
            if not context.has_autopay:
                return False

        return True

    @classmethod
    def _check_visibility(
        cls,
        visibility: str,
        context: MenuContext,
    ) -> bool:
        """Проверить видимость кнопки."""
        if visibility == 'all':
            return True
        if visibility == 'admins':
            return context.is_admin
        if visibility == 'moderators':
            return context.is_moderator and not context.is_admin
        if visibility == 'subscribers':
            return context.has_active_subscription and context.subscription_is_active
        return True

    # --- Форматирование текста ---

    # Список всех поддерживаемых плейсхолдеров
    _PLACEHOLDERS = (
        '{balance}',
        '{username}',
        '{subscription_days}',
        '{traffic_used}',
        '{traffic_left}',
        '{referral_count}',
        '{referral_earnings}',
    )

    @classmethod
    def _text_has_placeholders(cls, text_config: dict[str, str]) -> bool:
        """Проверить, содержит ли текст динамические плейсхолдеры."""
        if not text_config or not isinstance(text_config, dict):
            return False

        for lang_text in text_config.values():
            if not isinstance(lang_text, str):
                continue
            for placeholder in cls._PLACEHOLDERS:
                if placeholder in lang_text:
                    return True
        return False

    @classmethod
    def _get_localized_text(
        cls,
        text_config: dict[str, str],
        language: str,
        fallback_language: str = 'en',
    ) -> str:
        """Получить локализованный текст."""
        # Пробуем запрошенный язык
        if language in text_config:
            return text_config[language]
        # Пробуем fallback
        if fallback_language in text_config:
            return text_config[fallback_language]
        # Возвращаем первый доступный
        if text_config:
            return next(iter(text_config.values()))
        return ''

    @classmethod
    def _format_dynamic_text(
        cls,
        text: str,
        context: MenuContext,
        texts: Any,
    ) -> str:
        """Форматировать динамический текст с плейсхолдерами."""
        # Баланс
        if '{balance}' in text:
            formatted_balance = texts.format_price(context.balance_kopeks)
            text = text.replace('{balance}', formatted_balance)

        # Имя пользователя
        if '{username}' in text:
            text = text.replace('{username}', context.username or 'User')

        # Дней до окончания подписки
        if '{subscription_days}' in text:
            text = text.replace('{subscription_days}', str(context.subscription_days))

        # Использованный трафик
        if '{traffic_used}' in text:
            traffic = f'{context.traffic_used_gb:.1f} GB'
            text = text.replace('{traffic_used}', traffic)

        # Оставшийся трафик
        if '{traffic_left}' in text:
            traffic = f'{context.traffic_left_gb:.1f} GB'
            text = text.replace('{traffic_left}', traffic)

        # Количество рефералов
        if '{referral_count}' in text:
            text = text.replace('{referral_count}', str(context.referral_count))

        # Заработок с рефералов
        if '{referral_earnings}' in text:
            formatted_earnings = texts.format_price(context.referral_earnings_kopeks)
            text = text.replace('{referral_earnings}', formatted_earnings)

        return text

    # --- Построение кнопок ---

    @classmethod
    def _build_button(
        cls,
        button_config: dict[str, Any],
        context: MenuContext,
        texts: Any,
        button_id: str = '',
    ) -> InlineKeyboardButton | None:
        """Построить кнопку из конфигурации.

        Args:
            button_config: Конфигурация кнопки
            context: Контекст пользователя
            texts: Локализованные тексты
            button_id: ID кнопки (ключ в словаре buttons)
        """
        button_type = button_config.get('type', 'builtin')
        # Используем переданный button_id или fallback на builtin_id
        effective_button_id = button_id or button_config.get('builtin_id', '')
        text_config = button_config.get('text', {})
        action = button_config.get('action', '')
        open_mode = button_config.get('open_mode', 'callback')
        webapp_url = button_config.get('webapp_url')
        icon = button_config.get('icon', '')

        # Логирование для отладки кнопки connect
        is_connect_button = (
            effective_button_id == 'connect'
            or 'connect' in str(effective_button_id).lower()
            or action == 'subscription_connect'
            or 'connect' in str(action).lower()
        )

        if is_connect_button:
            logger.info(
                '🔗 Построение кнопки connect: button_id=, type=, open_mode=, action=, webapp_url',
                effective_button_id=effective_button_id,
                button_type=button_type,
                open_mode=open_mode,
                action=action,
                webapp_url=webapp_url,
            )

        # Получаем текст
        text = cls._get_localized_text(text_config, context.language)
        if not text:
            return None

        # Добавляем иконку если есть и текст не начинается с неё
        if icon and not text.startswith(icon):
            text = f'{icon} {text}'

        # Форматируем динамический текст
        if button_config.get('dynamic_text'):
            text = cls._format_dynamic_text(text, context, texts)

        # Строим кнопку в зависимости от типа
        if button_type == 'url':
            return InlineKeyboardButton(text=text, url=action)
        if button_type == 'mini_app':
            return InlineKeyboardButton(text=text, web_app=types.WebAppInfo(url=action))
        if button_type == 'callback':
            # Кастомная кнопка с callback_data
            return InlineKeyboardButton(text=text, callback_data=action)
        # builtin - проверяем open_mode
        if open_mode == 'direct':
            # Прямое открытие Mini App через WebAppInfo
            # Используем webapp_url, если указан, иначе action (если это URL)
            url = webapp_url or action

            # Для кнопки connect: если URL не указан или это callback_data,
            # пытаемся получить URL из подписки пользователя
            if is_connect_button and (not url or not (url.startswith('http://') or url.startswith('https://'))):
                if context.subscription:
                    from app.utils.subscription_utils import get_display_subscription_link

                    subscription_url = get_display_subscription_link(context.subscription)
                    if subscription_url:
                        url = subscription_url
                        logger.info('🔗 Кнопка connect: получен URL из подписки: ...', url=url[:50])
                # Если все еще нет URL, пробуем использовать настройку MINIAPP_CUSTOM_URL
                if not url or not (url.startswith('http://') or url.startswith('https://')):
                    if settings.MINIAPP_CUSTOM_URL:
                        url = settings.MINIAPP_CUSTOM_URL
                        logger.info('🔗 Кнопка connect: использован MINIAPP_CUSTOM_URL: ...', url=url[:50])

            # Проверяем, что это действительно URL
            if url and (url.startswith('http://') or url.startswith('https://')):
                logger.info('🔗 Кнопка connect: open_mode=direct, используем URL: ...', url=url[:50])
                return InlineKeyboardButton(text=text, web_app=types.WebAppInfo(url=url))
            logger.warning(
                '🔗 Кнопка connect: open_mode=direct, но URL не найден. webapp_url=, action=, subscription_url',
                webapp_url=webapp_url,
                action=action,
                value='есть' if context.subscription else 'нет',
            )
            # Fallback на callback_data
            return InlineKeyboardButton(text=text, callback_data=action)
        # Стандартный callback_data
        logger.debug('Кнопка connect: open_mode=, используем callback_data', open_mode=open_mode, action=action)
        return InlineKeyboardButton(text=text, callback_data=action)

    # --- Построение клавиатуры ---

    @classmethod
    async def build_keyboard(
        cls,
        db: AsyncSession,
        context: MenuContext,
    ) -> InlineKeyboardMarkup:
        """Построить клавиатуру меню на основе конфигурации."""
        config = await cls.get_config(db)
        texts = get_texts(context.language)

        keyboard_rows: list[list[InlineKeyboardButton]] = []
        rows_config = config.get('rows', [])
        buttons_config = config.get('buttons', {})

        for row_config in rows_config:
            # Проверяем условия строки
            row_conditions = row_config.get('conditions')
            if not cls._evaluate_conditions(row_conditions, context):
                continue

            row_buttons: list[InlineKeyboardButton] = []
            max_per_row = row_config.get('max_per_row', 2)

            for button_id in row_config.get('buttons', []):
                if button_id not in buttons_config:
                    continue

                button_cfg = buttons_config[button_id]

                # Проверяем включена ли кнопка
                if not button_cfg.get('enabled', True):
                    continue

                # Проверяем видимость
                visibility = button_cfg.get('visibility', 'all')
                if not cls._check_visibility(visibility, context):
                    continue

                # Проверяем условия кнопки
                button_conditions = button_cfg.get('conditions')
                if not cls._evaluate_conditions(button_conditions, context):
                    continue

                # Строим кнопку (передаём button_id для кастомных кнопок)
                button = cls._build_button(button_cfg, context, texts, button_id=button_id)
                if button:
                    row_buttons.append(button)

            # Добавляем кнопки с учетом max_per_row
            if row_buttons:
                for i in range(0, len(row_buttons), max_per_row):
                    keyboard_rows.append(row_buttons[i : i + max_per_row])

        return InlineKeyboardMarkup(inline_keyboard=keyboard_rows)

    @classmethod
    async def preview_keyboard(
        cls,
        db: AsyncSession,
        context: MenuContext,
    ) -> list[dict[str, Any]]:
        """Предпросмотр меню (возвращает структуру для API)."""
        config = await cls.get_config(db)
        texts = get_texts(context.language)

        preview_rows: list[dict[str, Any]] = []
        rows_config = config.get('rows', [])
        buttons_config = config.get('buttons', {})

        for row_config in rows_config:
            row_conditions = row_config.get('conditions')
            if not cls._evaluate_conditions(row_conditions, context):
                continue

            row_buttons: list[dict[str, Any]] = []
            max_per_row = row_config.get('max_per_row', 2)

            for button_id in row_config.get('buttons', []):
                if button_id not in buttons_config:
                    continue

                button_cfg = buttons_config[button_id]

                if not button_cfg.get('enabled', True):
                    continue

                visibility = button_cfg.get('visibility', 'all')
                if not cls._check_visibility(visibility, context):
                    continue

                button_conditions = button_cfg.get('conditions')
                if not cls._evaluate_conditions(button_conditions, context):
                    continue

                text_config = button_cfg.get('text', {})
                text = cls._get_localized_text(text_config, context.language)

                if button_cfg.get('dynamic_text'):
                    text = cls._format_dynamic_text(text, context, texts)

                row_buttons.append(
                    {
                        'text': text,
                        'action': button_cfg.get('action', ''),
                        'type': button_cfg.get('type', 'builtin'),
                    }
                )

            if row_buttons:
                for i in range(0, len(row_buttons), max_per_row):
                    preview_rows.append({'buttons': row_buttons[i : i + max_per_row]})

        return preview_rows

    # --- История изменений (делегируем в MenuLayoutHistoryService) ---

    @classmethod
    async def save_history(
        cls,
        db: AsyncSession,
        config: dict[str, Any],
        action: str,
        changes_summary: str | None = None,
        user_info: str | None = None,
    ):
        """Сохранить запись в историю изменений."""
        return await MenuLayoutHistoryService.save_history(db, config, action, changes_summary, user_info)

    @classmethod
    async def get_history(
        cls,
        db: AsyncSession,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Получить историю изменений."""
        return await MenuLayoutHistoryService.get_history(db, limit, offset)

    @classmethod
    async def get_history_count(cls, db: AsyncSession) -> int:
        """Получить общее количество записей истории."""
        return await MenuLayoutHistoryService.get_history_count(db)

    @classmethod
    async def get_history_entry(
        cls,
        db: AsyncSession,
        history_id: int,
    ) -> dict[str, Any] | None:
        """Получить конкретную запись истории с конфигурацией."""
        return await MenuLayoutHistoryService.get_history_entry(db, history_id)

    @classmethod
    async def rollback_to_history(
        cls,
        db: AsyncSession,
        history_id: int,
        user_info: str | None = None,
    ) -> dict[str, Any]:
        """Откатить конфигурацию к записи из истории."""
        return await MenuLayoutHistoryService.rollback_to_history(
            db, history_id, cls.get_config, cls.save_config, user_info
        )

    # --- Статистика кликов (делегируем в MenuLayoutStatsService) ---

    @classmethod
    async def log_button_click(
        cls,
        db: AsyncSession,
        button_id: str,
        user_id: int | None = None,
        callback_data: str | None = None,
        button_type: str | None = None,
        button_text: str | None = None,
    ):
        """Записать клик по кнопке."""
        return await MenuLayoutStatsService.log_button_click(
            db, button_id, user_id, callback_data, button_type, button_text
        )

    @classmethod
    async def get_button_stats(
        cls,
        db: AsyncSession,
        button_id: str,
        days: int = 30,
    ) -> dict[str, Any]:
        """Получить статистику кликов по конкретной кнопке."""
        return await MenuLayoutStatsService.get_button_stats(db, button_id, days)

    @classmethod
    async def get_button_clicks_by_day(
        cls,
        db: AsyncSession,
        button_id: str,
        days: int = 30,
    ) -> list[dict[str, Any]]:
        """Получить статистику кликов по дням."""
        return await MenuLayoutStatsService.get_button_clicks_by_day(db, button_id, days)

    @classmethod
    async def get_all_buttons_stats(
        cls,
        db: AsyncSession,
        days: int = 30,
    ) -> list[dict[str, Any]]:
        """Получить статистику по всем кнопкам."""
        return await MenuLayoutStatsService.get_all_buttons_stats(db, days)

    @classmethod
    async def get_total_clicks(
        cls,
        db: AsyncSession,
        days: int = 30,
    ) -> int:
        """Получить общее количество кликов за период."""
        return await MenuLayoutStatsService.get_total_clicks(db, days)

    @classmethod
    async def get_stats_by_button_type(
        cls,
        db: AsyncSession,
        days: int = 30,
    ) -> list[dict[str, Any]]:
        """Получить статистику кликов по типам кнопок."""
        return await MenuLayoutStatsService.get_stats_by_button_type(db, days)

    @classmethod
    async def get_clicks_by_hour(
        cls,
        db: AsyncSession,
        button_id: str | None = None,
        days: int = 30,
    ) -> list[dict[str, Any]]:
        """Получить статистику кликов по часам дня."""
        return await MenuLayoutStatsService.get_clicks_by_hour(db, button_id, days)

    @classmethod
    async def get_clicks_by_weekday(
        cls,
        db: AsyncSession,
        button_id: str | None = None,
        days: int = 30,
    ) -> list[dict[str, Any]]:
        """Получить статистику кликов по дням недели."""
        return await MenuLayoutStatsService.get_clicks_by_weekday(db, button_id, days)

    @classmethod
    async def get_top_users(
        cls,
        db: AsyncSession,
        button_id: str | None = None,
        limit: int = 10,
        days: int = 30,
    ) -> list[dict[str, Any]]:
        """Получить топ пользователей по количеству кликов."""
        return await MenuLayoutStatsService.get_top_users(db, button_id, limit, days)

    @classmethod
    async def get_period_comparison(
        cls,
        db: AsyncSession,
        button_id: str | None = None,
        current_days: int = 7,
        previous_days: int = 7,
    ) -> dict[str, Any]:
        """Сравнить статистику текущего и предыдущего периода."""
        return await MenuLayoutStatsService.get_period_comparison(db, button_id, current_days, previous_days)

    @classmethod
    async def get_user_click_sequences(
        cls,
        db: AsyncSession,
        user_id: int,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Получить последовательности кликов пользователя."""
        return await MenuLayoutStatsService.get_click_sequences(db, user_id, limit)
