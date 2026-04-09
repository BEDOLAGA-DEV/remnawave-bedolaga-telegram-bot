import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.server_squad import get_all_server_squads
from app.database.crud.user import get_user_by_id
from app.database.models import Subscription, SubscriptionStatus, User
from app.external.remnawave_api import RemnaWaveAPI, RemnaWaveAPIError, RemnaWaveUser, TrafficLimitStrategy, UserStatus
from app.utils.subscription_utils import (
    resolve_hwid_device_limit_for_payload,
)


logger = structlog.get_logger(__name__)


def get_traffic_reset_strategy(tariff=None):
    """Получает стратегию сброса трафика.

    Args:
        tariff: Объект тарифа. Если у тарифа задан traffic_reset_mode,
               используется он, иначе глобальная настройка из конфига.

    Returns:
        TrafficLimitStrategy: Стратегия сброса трафика для RemnaWave API.
    """
    from app.config import settings

    strategy_mapping = {
        'NO_RESET': 'NO_RESET',
        'DAY': 'DAY',
        'WEEK': 'WEEK',
        'MONTH': 'MONTH',
        'MONTH_ROLLING': 'MONTH_ROLLING',
    }

    # Проверяем настройку тарифа
    if tariff is not None:
        tariff_mode = getattr(tariff, 'traffic_reset_mode', None)
        if tariff_mode is not None:
            mapped_strategy = strategy_mapping.get(tariff_mode.upper(), 'NO_RESET')
            logger.info(
                '🔄 Стратегия сброса трафика из тарифа',
                value=getattr(tariff, 'name', 'N/A'),
                tariff_mode=tariff_mode,
                mapped_strategy=mapped_strategy,
            )
            return getattr(TrafficLimitStrategy, mapped_strategy)

    # Используем глобальную настройку
    strategy = settings.DEFAULT_TRAFFIC_RESET_STRATEGY.upper()
    mapped_strategy = strategy_mapping.get(strategy, 'NO_RESET')
    logger.info('🔄 Стратегия сброса трафика из конфига', strategy=strategy, mapped_strategy=mapped_strategy)
    return getattr(TrafficLimitStrategy, mapped_strategy)


@dataclass
class PropagateSquadsResult:
    """Результат применения скводов тарифа к подпискам."""

    total: int = 0
    synced: int = 0
    failed_ids: list[int] = field(default_factory=list)


class SubscriptionService:
    def __init__(self):
        self._config_error: str | None = None
        self.api: RemnaWaveAPI | None = None
        self._last_config_signature: tuple[str, ...] | None = None

        self._refresh_configuration()

    def _refresh_configuration(self) -> None:
        auth_params = settings.get_remnawave_auth_params()
        base_url = (auth_params.get('base_url') or '').strip()
        api_key = (auth_params.get('api_key') or '').strip()
        secret_key = (auth_params.get('secret_key') or '').strip() or None
        username = (auth_params.get('username') or '').strip() or None
        password = (auth_params.get('password') or '').strip() or None
        caddy_token = (auth_params.get('caddy_token') or '').strip() or None
        auth_type = (auth_params.get('auth_type') or 'api_key').strip()

        config_signature = (
            base_url,
            api_key,
            secret_key or '',
            username or '',
            password or '',
            caddy_token or '',
            auth_type,
        )

        if config_signature == self._last_config_signature:
            return

        if not base_url:
            self._config_error = 'REMNAWAVE_API_URL не настроен'
            self.api = None
        elif not api_key:
            self._config_error = 'REMNAWAVE_API_KEY не настроен'
            self.api = None
        else:
            self._config_error = None
            self.api = RemnaWaveAPI(
                base_url=base_url,
                api_key=api_key,
                secret_key=secret_key,
                username=username,
                password=password,
                caddy_token=caddy_token,
                auth_type=auth_type,
            )

        if self._config_error:
            logger.warning(
                'RemnaWave API недоступен: . Подписочный сервис будет работать в оффлайн-режиме.',
                config_error=self._config_error,
            )

        self._last_config_signature = config_signature

    @staticmethod
    def _resolve_user_tag(subscription: Subscription) -> str | None:
        if getattr(subscription, 'is_trial', False):
            return settings.get_trial_user_tag()

        return settings.get_paid_subscription_user_tag()

    @property
    def is_configured(self) -> bool:
        return self._config_error is None

    @property
    def configuration_error(self) -> str | None:
        return self._config_error

    def _ensure_configured(self) -> None:
        self._refresh_configuration()
        if not self.api or not self.is_configured:
            raise RemnaWaveAPIError(self._config_error or 'RemnaWave API не настроен')

    @asynccontextmanager
    async def get_api_client(self):
        self._ensure_configured()
        assert self.api is not None
        async with self.api as api:
            yield api

    async def create_remnawave_user(
        self,
        db: AsyncSession,
        subscription: Subscription,
        *,
        reset_traffic: bool = False,
        reset_reason: str | None = None,
    ) -> RemnaWaveUser | None:
        try:
            user = await get_user_by_id(db, subscription.user_id)
            if not user:
                logger.error('Пользователь не найден', user_id=subscription.user_id)
                return None

            validation_success = await self.validate_and_clean_subscription(db, subscription, user)
            if not validation_success:
                logger.error('Ошибка валидации подписки для пользователя', _format_user_log=self._format_user_log(user))
                return None

            # Загружаем tariff заранее, чтобы избежать lazy loading в async контексте
            try:
                await db.refresh(subscription, ['tariff'])
            except Exception:
                pass  # tariff может быть None или уже загружен

            user_tag = self._resolve_user_tag(subscription)

            # Определяем внешний сквад из тарифа
            ext_squad_uuid = subscription.tariff.external_squad_uuid if subscription.tariff else None

            async with self.get_api_client() as api:
                hwid_limit = resolve_hwid_device_limit_for_payload(subscription)

                # Multi-tariff mode: each subscription has its own Remnawave user
                if settings.is_multi_tariff_enabled():
                    updated_user = await self._create_or_update_remnawave_user_multi(
                        api,
                        user,
                        subscription,
                        user_tag=user_tag,
                        hwid_limit=hwid_limit,
                        ext_squad_uuid=ext_squad_uuid,
                        reset_traffic=reset_traffic,
                        reset_reason=reset_reason,
                    )
                else:
                    updated_user = await self._create_or_update_remnawave_user_single(
                        api,
                        user,
                        subscription,
                        user_tag=user_tag,
                        hwid_limit=hwid_limit,
                        ext_squad_uuid=ext_squad_uuid,
                        reset_traffic=reset_traffic,
                        reset_reason=reset_reason,
                    )

                subscription.remnawave_short_uuid = updated_user.short_uuid
                subscription.subscription_url = updated_user.subscription_url
                subscription.subscription_crypto_link = updated_user.happ_crypto_link
                subscription.remnawave_uuid = updated_user.uuid
                # Legacy field — keep in sync for single-mode backward compat
                if not settings.is_multi_tariff_enabled():
                    user.remnawave_uuid = updated_user.uuid

                await db.commit()

                logger.info('✅ Создан/обновлен RemnaWave пользователь для подписки', subscription_id=subscription.id)
                logger.info('🔗 Ссылка на подписку', subscription_url=updated_user.subscription_url)
                strategy_name = settings.DEFAULT_TRAFFIC_RESET_STRATEGY
                logger.info('📊 Стратегия сброса трафика', strategy_name=strategy_name)
                return updated_user

        except RemnaWaveAPIError as e:
            logger.error('Ошибка RemnaWave API', error=e)
            return None
        except Exception as e:
            logger.error('Ошибка создания RemnaWave пользователя', error=e)
            return None

    async def _create_or_update_remnawave_user_multi(
        self,
        api: RemnaWaveAPI,
        user: User,
        subscription: Subscription,
        *,
        user_tag: str | None,
        hwid_limit: int | None,
        ext_squad_uuid: str | None,
        reset_traffic: bool,
        reset_reason: str | None,
    ) -> RemnaWaveUser:
        """Multi-tariff mode: each subscription gets its own Remnawave user."""
        description = settings.format_remnawave_user_description(
            full_name=user.full_name,
            username=user.username,
            telegram_id=user.telegram_id,
            email=user.email,
            user_id=user.id,
        )
        common_kwargs = dict(
            status=UserStatus.ACTIVE,
            expire_at=subscription.end_date,
            traffic_limit_bytes=self._gb_to_bytes(subscription.traffic_limit_gb),
            traffic_limit_strategy=get_traffic_reset_strategy(subscription.tariff),
            telegram_id=user.telegram_id,
            email=user.email,
            description=description,
        )
        if subscription.connected_squads:
            common_kwargs['active_internal_squads'] = subscription.connected_squads
        if user_tag is not None:
            common_kwargs['tag'] = user_tag
        if hwid_limit is not None:
            common_kwargs['hwid_device_limit'] = hwid_limit
        if ext_squad_uuid is not None:
            common_kwargs['external_squad_uuid'] = ext_squad_uuid

        # If this subscription already has a Remnawave user — update it
        if subscription.remnawave_uuid:
            try:
                existing = await api.get_user_by_uuid(subscription.remnawave_uuid)
                if existing:
                    try:
                        await api.reset_user_devices(existing.uuid)
                    except Exception as hwid_error:
                        logger.warning('⚠️ Не удалось сбросить HWID', hwid_error=hwid_error)

                    updated = await api.update_user(uuid=existing.uuid, **common_kwargs)
                    if reset_traffic:
                        await self._reset_user_traffic(api, updated.uuid, user, reset_reason)
                    return updated
            except Exception:
                logger.warning(
                    '⚠️ Не удалось найти Remnawave юзера по UUID подписки, создаём нового',
                    subscription_id=subscription.id,
                    remnawave_uuid=subscription.remnawave_uuid,
                )

        # New subscription — create a NEW Remnawave user
        base_username = settings.format_remnawave_username(
            full_name=user.full_name,
            username=user.username,
            telegram_id=user.telegram_id,
            email=user.email,
            user_id=user.id,
        )
        # Use permanent short_id from subscription (generated at creation time)
        username = f'{base_username}_{subscription.remnawave_short_id}'

        updated_user = await api.create_user(username=username, **common_kwargs)
        if reset_traffic:
            await self._reset_user_traffic(api, updated_user.uuid, user, reset_reason)
        return updated_user

    async def _create_or_update_remnawave_user_single(
        self,
        api: RemnaWaveAPI,
        user: User,
        subscription: Subscription,
        *,
        user_tag: str | None,
        hwid_limit: int | None,
        ext_squad_uuid: str | None,
        reset_traffic: bool,
        reset_reason: str | None,
    ) -> RemnaWaveUser:
        """Single-subscription mode (legacy): one Remnawave user per bot user."""
        description = settings.format_remnawave_user_description(
            full_name=user.full_name,
            username=user.username,
            telegram_id=user.telegram_id,
            email=user.email,
            user_id=user.id,
        )

        # Search for existing Remnawave user
        existing_users = []
        if user.remnawave_uuid:
            try:
                existing_user = await api.get_user_by_uuid(user.remnawave_uuid)
                if existing_user:
                    existing_users = [existing_user]
            except Exception:
                pass

        if not existing_users and user.telegram_id:
            existing_users = await api.get_user_by_telegram_id(user.telegram_id)

        if not existing_users and user.email:
            try:
                existing_users = await api.get_user_by_email(user.email)
            except Exception:
                pass

        common_kwargs = dict(
            status=UserStatus.ACTIVE,
            expire_at=subscription.end_date,
            traffic_limit_bytes=self._gb_to_bytes(subscription.traffic_limit_gb),
            traffic_limit_strategy=get_traffic_reset_strategy(subscription.tariff),
            telegram_id=user.telegram_id,
            email=user.email,
            description=description,
        )
        if subscription.connected_squads:
            common_kwargs['active_internal_squads'] = subscription.connected_squads
        if user_tag is not None:
            common_kwargs['tag'] = user_tag
        if hwid_limit is not None:
            common_kwargs['hwid_device_limit'] = hwid_limit
        if ext_squad_uuid is not None:
            common_kwargs['external_squad_uuid'] = ext_squad_uuid

        if existing_users:
            logger.info(
                '🔄 Найден существующий пользователь в панели для', _format_user_log=self._format_user_log(user)
            )
            remnawave_user = existing_users[0]

            try:
                await api.reset_user_devices(remnawave_user.uuid)
                logger.info('🔧 Сброшены HWID устройства для', _format_user_log=self._format_user_log(user))
            except Exception as hwid_error:
                logger.warning('⚠️ Не удалось сбросить HWID', hwid_error=hwid_error)

            updated_user = await api.update_user(uuid=remnawave_user.uuid, **common_kwargs)
            if reset_traffic:
                await self._reset_user_traffic(api, updated_user.uuid, user, reset_reason)
            return updated_user

        logger.info('🆕 Создаем нового пользователя в панели для', _format_user_log=self._format_user_log(user))
        username = settings.format_remnawave_username(
            full_name=user.full_name,
            username=user.username,
            telegram_id=user.telegram_id,
            email=user.email,
            user_id=user.id,
        )
        updated_user = await api.create_user(username=username, **common_kwargs)
        if reset_traffic:
            await self._reset_user_traffic(api, updated_user.uuid, user, reset_reason)
        return updated_user

    async def update_remnawave_user(
        self,
        db: AsyncSession,
        subscription: Subscription,
        *,
        reset_traffic: bool = False,
        reset_reason: str | None = None,
        sync_squads: bool = False,
    ) -> RemnaWaveUser | None:
        try:
            user = await get_user_by_id(db, subscription.user_id)
            if not user:
                logger.error('Пользователь не найден', user_id=subscription.user_id)
                return None

            # Resolve the Remnawave UUID: prefer subscription-level in multi-tariff mode
            if settings.is_multi_tariff_enabled():
                remnawave_uuid = subscription.remnawave_uuid
                if not remnawave_uuid:
                    logger.warning(
                        'Multi-tariff: subscription has no remnawave_uuid, cannot update panel',
                        subscription_id=subscription.id,
                        user_id=subscription.user_id,
                    )
                    return None
            else:
                remnawave_uuid = user.remnawave_uuid
            if not remnawave_uuid:
                logger.error('RemnaWave UUID не найден для пользователя', user_id=subscription.user_id)
                return None

            # Загружаем tariff заранее, чтобы избежать lazy loading в async контексте
            try:
                await db.refresh(subscription, ['tariff'])
            except Exception:
                pass  # tariff может быть None или уже загружен

            current_time = datetime.now(UTC)
            # Определяем актуальный статус для отправки в RemnaWave
            # НЕ меняем статус подписки здесь - это задача scheduled job
            is_actually_active = (
                subscription.status in (SubscriptionStatus.ACTIVE.value, SubscriptionStatus.TRIAL.value)
                and subscription.end_date > current_time
            )

            # Логируем если статус и end_date не согласованы (для отладки)
            if (
                subscription.status in (SubscriptionStatus.ACTIVE.value, SubscriptionStatus.TRIAL.value)
                and subscription.end_date <= current_time
            ):
                logger.warning(
                    '⚠️ update_remnawave_user: подписка имеет статус ACTIVE, но end_date <= now. Отправляем в RemnaWave как DISABLED, но НЕ меняем статус в БД.',
                    subscription_id=subscription.id,
                    end_date=subscription.end_date,
                    current_time=current_time,
                )

            user_tag = self._resolve_user_tag(subscription)

            # Определяем внешний сквад из тарифа
            ext_squad_uuid = subscription.tariff.external_squad_uuid if subscription.tariff else None

            async with self.get_api_client() as api:
                hwid_limit = resolve_hwid_device_limit_for_payload(subscription)

                update_kwargs = dict(
                    uuid=remnawave_uuid,
                    status=UserStatus.ACTIVE if is_actually_active else UserStatus.DISABLED,
                    expire_at=subscription.end_date
                    if is_actually_active
                    else max(subscription.end_date, current_time + timedelta(minutes=1)),
                    traffic_limit_bytes=self._gb_to_bytes(subscription.traffic_limit_gb),
                    traffic_limit_strategy=get_traffic_reset_strategy(subscription.tariff),
                    telegram_id=user.telegram_id,
                    email=user.email,
                    description=settings.format_remnawave_user_description(
                        full_name=user.full_name,
                        username=user.username,
                        telegram_id=user.telegram_id,
                        email=user.email,
                        user_id=user.id,
                    ),
                )

                # Сквады отправляем только при явном sync_squads=True (propagate_squads и пр.)
                # В рутинных обновлениях пропускаем — сквады уже назначены при создании подписки,
                # а пересылка стейловых UUID вызывает FK violation → A039 в RemnaWave
                if sync_squads and subscription.connected_squads:
                    update_kwargs['active_internal_squads'] = subscription.connected_squads

                if user_tag is not None:
                    update_kwargs['tag'] = user_tag

                if hwid_limit is not None:
                    update_kwargs['hwid_device_limit'] = hwid_limit

                # Внешний сквад НЕ пересылаем в рутинных обновлениях — он уже назначен
                # при создании подписки. Стейловый UUID вызывает FK violation → A039.
                # Синхронизация сквадов происходит только при sync_squads=True.
                if sync_squads and ext_squad_uuid is not None:
                    update_kwargs['external_squad_uuid'] = ext_squad_uuid

                updated_user = await api.update_user(**update_kwargs)

                if reset_traffic:
                    if settings.is_multi_tariff_enabled():
                        reset_uuid = subscription.remnawave_uuid
                        if not reset_uuid:
                            logger.warning(
                                'Multi-tariff: subscription has no remnawave_uuid, skipping traffic reset',
                                subscription_id=subscription.id,
                                user_id=subscription.user_id,
                            )
                    else:
                        reset_uuid = user.remnawave_uuid
                    if reset_uuid:
                        await self._reset_user_traffic(
                            api,
                            reset_uuid,
                            user,
                            reset_reason,
                        )

                subscription.subscription_url = updated_user.subscription_url
                subscription.subscription_crypto_link = updated_user.happ_crypto_link
                await db.commit()

                status_text = 'активным' if is_actually_active else 'истёкшим'
                logger.info(
                    '✅ Обновлен RemnaWave пользователь со статусом',
                    remnawave_uuid=remnawave_uuid,
                    status_text=status_text,
                )
                strategy_name = settings.DEFAULT_TRAFFIC_RESET_STRATEGY
                logger.info('📊 Стратегия сброса трафика', strategy_name=strategy_name)
                return updated_user

        except RemnaWaveAPIError as e:
            logger.error('Ошибка RemnaWave API', error=e)
            return None
        except Exception as e:
            logger.error('Ошибка обновления RemnaWave пользователя', error=e)
            return None

    @staticmethod
    def _format_user_log(user) -> str:
        """Форматирует идентификатор пользователя для логов."""
        if user.telegram_id:
            return f'user {user.telegram_id}'
        if user.email:
            return f'user {user.id} ({user.email})'
        return f'user {user.id}'

    async def _reset_user_traffic(
        self,
        api: RemnaWaveAPI,
        user_uuid: str,
        user,  # User object вместо telegram_id
        reset_reason: str | None = None,
    ) -> None:
        if not user_uuid:
            return

        try:
            await api.reset_user_traffic(user_uuid)
            reason_text = f' ({reset_reason})' if reset_reason else ''
            logger.info(
                '🔄 Сброшен трафик RemnaWave для', _format_user_log=self._format_user_log(user), reason_text=reason_text
            )
        except Exception as exc:
            logger.warning(
                '⚠️ Не удалось сбросить трафик RemnaWave для', _format_user_log=self._format_user_log(user), error=exc
            )

    async def disable_remnawave_user(self, user_uuid: str) -> bool:
        try:
            async with self.get_api_client() as api:
                await api.disable_user(user_uuid)
                logger.info('✅ Отключен RemnaWave пользователь', user_uuid=user_uuid)
                return True

        except Exception as e:
            error_msg = str(e).lower()
            # "User already disabled" - считаем успехом
            if 'already disabled' in error_msg:
                logger.info('✅ RemnaWave пользователь уже отключен', user_uuid=user_uuid)
                return True
            logger.error('Ошибка отключения RemnaWave пользователя', error=e)
            return False

    async def delete_remnawave_user(self, user_uuid: str) -> bool:
        """Полное удаление пользователя из панели RemnaWave (хуки прекращаются)."""
        try:
            async with self.get_api_client() as api:
                await api.delete_user(user_uuid)
                logger.info('🗑 Удалён RemnaWave пользователь', user_uuid=user_uuid)
                return True

        except Exception as e:
            error_msg = str(e).lower()
            if 'not found' in error_msg or 'not exist' in error_msg:
                logger.info('🗑 RemnaWave пользователь уже удалён', user_uuid=user_uuid)
                return True
            logger.error('Ошибка удаления RemnaWave пользователя', error=e, user_uuid=user_uuid)
            return False

    async def enable_remnawave_user(self, user_uuid: str) -> bool:
        """Включить пользователя в RemnaWave (реактивация)."""
        try:
            async with self.get_api_client() as api:
                await api.enable_user(user_uuid)
                logger.info('✅ Включен RemnaWave пользователь', user_uuid=user_uuid)
                return True

        except Exception as e:
            error_msg = str(e).lower()
            # "User already enabled" - считаем успехом
            if 'already enabled' in error_msg:
                logger.info('✅ RemnaWave пользователь уже включен', user_uuid=user_uuid)
                return True
            logger.error('Ошибка включения RemnaWave пользователя', error=e)
            return False

    async def get_remnawave_squads(self) -> list[dict] | None:
        """Получить список internal squads из RemnaWave."""
        try:
            async with self.get_api_client() as api:
                squads = await api.get_internal_squads()
                # Преобразуем в формат для sync_with_remnawave
                result = []
                for squad in squads:
                    result.append(
                        {
                            'uuid': squad.uuid,
                            'name': squad.name,
                        }
                    )
                logger.info('✅ Получено серверов из RemnaWave', result_count=len(result))
                return result

        except Exception as e:
            logger.error('Ошибка получения серверов из RemnaWave', error=e)
            return None

    async def revoke_subscription(self, db: AsyncSession, subscription: Subscription) -> str | None:
        try:
            user = await get_user_by_id(db, subscription.user_id)
            if not user:
                return None
            if settings.is_multi_tariff_enabled():
                revoke_uuid = subscription.remnawave_uuid
                if not revoke_uuid:
                    logger.warning(
                        'Multi-tariff: subscription has no remnawave_uuid, cannot revoke',
                        subscription_id=subscription.id,
                        user_id=subscription.user_id,
                    )
                    return None
            else:
                revoke_uuid = user.remnawave_uuid
            if not revoke_uuid:
                return None

            async with self.get_api_client() as api:
                updated_user = await api.revoke_user_subscription(revoke_uuid)

                subscription.remnawave_short_uuid = updated_user.short_uuid
                subscription.subscription_url = updated_user.subscription_url
                subscription.subscription_crypto_link = updated_user.happ_crypto_link
                await db.commit()

                logger.info('✅ Обновлена ссылка подписки для', _format_user_log=self._format_user_log(user))
                return updated_user.subscription_url

        except Exception as e:
            logger.error('Ошибка обновления ссылки подписки', error=e)
            return None

    async def get_subscription_info(self, short_uuid: str) -> dict | None:
        try:
            async with self.get_api_client() as api:
                info = await api.get_subscription_info(short_uuid)
                return info

        except Exception as e:
            logger.error('Ошибка получения информации о подписке', error=e)
            return None

    async def sync_subscription_usage(self, db: AsyncSession, subscription: Subscription) -> bool:
        try:
            user = await get_user_by_id(db, subscription.user_id)
            if not user:
                return False
            if settings.is_multi_tariff_enabled():
                sync_uuid = subscription.remnawave_uuid
                if not sync_uuid:
                    logger.warning(
                        'Multi-tariff: subscription has no remnawave_uuid, cannot sync usage',
                        subscription_id=subscription.id,
                        user_id=subscription.user_id,
                    )
                    return False
            else:
                sync_uuid = user.remnawave_uuid
            if not sync_uuid:
                return False

            async with self.get_api_client() as api:
                remnawave_user = await api.get_user_by_uuid(sync_uuid)
                if not remnawave_user:
                    return False

                used_gb = self._bytes_to_gb(remnawave_user.used_traffic_bytes)
                subscription.traffic_used_gb = used_gb

                await db.commit()

                logger.debug('Синхронизирован трафик для подписки ГБ', subscription_id=subscription.id, used_gb=used_gb)
                return True

        except Exception as e:
            logger.error('Ошибка синхронизации трафика', error=e)
            return False

    async def ensure_subscription_synced(
        self,
        db: AsyncSession,
        subscription: Subscription,
    ) -> tuple[bool, str | None]:
        """
        Проверяет и синхронизирует подписку с RemnaWave при необходимости.

        Если subscription_url отсутствует или данные не синхронизированы,
        пытается обновить/создать пользователя в RemnaWave.

        Returns:
            Tuple[bool, Optional[str]]: (успех, сообщение об ошибке)
        """
        try:
            user = await get_user_by_id(db, subscription.user_id)
            if not user:
                logger.error('Пользователь не найден для подписки', subscription_id=subscription.id)
                return False, 'user_not_found'

            # Проверяем, нужна ли синхронизация
            sub_uuid = subscription.remnawave_uuid if settings.is_multi_tariff_enabled() else user.remnawave_uuid
            needs_sync = not subscription.subscription_url or not sub_uuid

            if not needs_sync:
                # Проверяем, существует ли пользователь в RemnaWave
                try:
                    async with self.get_api_client() as api:
                        remnawave_user = await api.get_user_by_uuid(sub_uuid)
                        if not remnawave_user:
                            needs_sync = True
                            logger.warning(
                                'Пользователь не найден в RemnaWave, требуется синхронизация',
                                remnawave_uuid=sub_uuid,
                            )
                except Exception as check_error:
                    logger.warning('Не удалось проверить пользователя в RemnaWave', check_error=check_error)
                    # Продолжаем, возможно проблема временная

            if not needs_sync:
                return True, None

            logger.info(
                'Синхронизация подписки с RemnaWave (subscription_url=, remnawave_uuid=)',
                subscription_id=subscription.id,
                subscription_url=bool(subscription.subscription_url),
                remnawave_uuid=bool(sub_uuid),
            )

            # Пытаемся синхронизировать
            result = None
            if sub_uuid:
                # Пробуем обновить существующего пользователя
                result = await self.update_remnawave_user(
                    db,
                    subscription,
                    reset_traffic=False,
                )
                # Если update не удался (пользователь удалён из RemnaWave) — пробуем создать
                if not result:
                    logger.warning(
                        'Не удалось обновить пользователя в RemnaWave, пробуем создать заново',
                        remnawave_uuid=sub_uuid,
                    )
                    # Сбрасываем старый UUID, create_remnawave_user установит новый
                    if settings.is_multi_tariff_enabled():
                        subscription.remnawave_uuid = None
                    else:
                        user.remnawave_uuid = None
                    result = await self.create_remnawave_user(
                        db,
                        subscription,
                        reset_traffic=False,
                    )
            else:
                # Создаём нового пользователя
                result = await self.create_remnawave_user(
                    db,
                    subscription,
                    reset_traffic=False,
                )

            if result:
                await db.refresh(subscription)
                await db.refresh(user)
                logger.info(
                    'Подписка успешно синхронизирована с RemnaWave. URL',
                    subscription_id=subscription.id,
                    subscription_url=subscription.subscription_url,
                )
                return True, None
            logger.error('Не удалось синхронизировать подписку с RemnaWave', subscription_id=subscription.id)
            return False, 'sync_failed'

        except RemnaWaveAPIError as api_error:
            logger.error(
                'Ошибка RemnaWave API при синхронизации подписки', subscription_id=subscription.id, api_error=api_error
            )
            return False, 'api_error'
        except Exception as e:
            logger.error('Ошибка синхронизации подписки', subscription_id=subscription.id, error=e)
            return False, 'unknown_error'

    async def validate_and_clean_subscription(self, db: AsyncSession, subscription: Subscription, user: User) -> bool:
        try:
            needs_cleanup = False
            user_log = self._format_user_log(user)

            # In multi-tariff mode, validate per-subscription UUID, not user-level UUID
            check_uuid = subscription.remnawave_uuid if settings.is_multi_tariff_enabled() else user.remnawave_uuid

            if check_uuid:
                try:
                    async with self.get_api_client() as api:
                        remnawave_user = await api.get_user_by_uuid(check_uuid)

                        if not remnawave_user:
                            logger.warning(
                                '⚠️ UUID не найден в панели',
                                user_log=user_log,
                                remnawave_uuid=check_uuid,
                            )
                            needs_cleanup = True
                        elif (
                            user.telegram_id
                            and remnawave_user.telegram_id
                            and remnawave_user.telegram_id != user.telegram_id
                        ):
                            logger.warning(
                                '⚠️ Несоответствие telegram_id для panel',
                                user_log=user_log,
                                telegram_id=remnawave_user.telegram_id,
                            )
                            needs_cleanup = True
                except Exception as api_error:
                    logger.error('❌ Ошибка проверки пользователя в панели', api_error=api_error)
                    needs_cleanup = True

            if subscription.remnawave_short_uuid and not check_uuid:
                logger.warning('⚠️ У подписки есть short_uuid, но нет remnawave_uuid')
                needs_cleanup = True

            if needs_cleanup:
                logger.info('🧹 Очищаем мусорные данные подписки для', user_log=user_log)

                subscription.remnawave_short_uuid = None
                subscription.remnawave_uuid = None
                subscription.subscription_url = ''
                subscription.subscription_crypto_link = ''

                if not settings.is_multi_tariff_enabled():
                    user.remnawave_uuid = None

                await db.commit()
                logger.info('✅ Мусорные данные очищены для', user_log=user_log)

            return True

        except Exception as e:
            logger.error('❌ Ошибка валидации подписки для', _format_user_log=self._format_user_log(user), error=e)
            await db.rollback()
            return False

    async def get_countries_price_by_uuids(
        self,
        country_uuids: list[str],
        db: AsyncSession,
        *,
        promo_group_id: int | None = None,
    ) -> tuple[int, list[int]]:
        try:
            from app.database.crud.server_squad import get_server_squad_by_uuid

            total_price = 0
            prices_list = []

            for country_uuid in country_uuids:
                server = await get_server_squad_by_uuid(db, country_uuid)
                is_allowed = True
                if promo_group_id is not None and server:
                    allowed_ids = {pg.id for pg in server.allowed_promo_groups}
                    is_allowed = promo_group_id in allowed_ids

                if server and server.is_available and not server.is_full and is_allowed:
                    price = server.price_kopeks
                    total_price += price
                    prices_list.append(price)
                    logger.debug('🏷️ Страна ₽', display_name=server.display_name, price=price / 100)
                else:
                    default_price = 0
                    total_price += default_price
                    prices_list.append(default_price)
                    logger.warning(
                        '⚠️ Сервер недоступен, используем базовую цену: ₽',
                        country_uuid=country_uuid,
                        default_price=default_price / 100,
                    )

            logger.info('💰 Общая стоимость стран: ₽', total_price=total_price / 100)
            return total_price, prices_list

        except Exception as e:
            logger.error('Ошибка получения цен стран', error=e)
            default_prices = [0] * len(country_uuids)
            return sum(default_prices), default_prices

    def _gb_to_bytes(self, gb: int | None) -> int:
        if not gb:  # None or 0
            return 0
        return gb * 1024 * 1024 * 1024

    def _bytes_to_gb(self, bytes_value: int) -> float:
        if bytes_value == 0:
            return 0.0
        return bytes_value / (1024 * 1024 * 1024)

    async def propagate_tariff_squads(
        self, db: AsyncSession, tariff_id: int, new_squads: list[str], *, concurrency: int = 5
    ) -> PropagateSquadsResult:
        """Применяет изменение серверов тарифа к активным подпискам и синхронизирует с RemnaWave.

        Если new_squads пустой — означает "все серверы", будут подставлены все доступные.
        Синхронизация с RemnaWave выполняется параллельно с ограничением concurrency.
        Паттерн: предзагрузка данных → параллельные API-вызовы → один commit.
        """
        squads_to_set = list(new_squads)
        if not squads_to_set:
            all_servers, _ = await get_all_server_squads(db, available_only=True, limit=10000)
            squads_to_set = [s.squad_uuid for s in all_servers if s.squad_uuid]

        result = await db.execute(
            select(Subscription).where(
                Subscription.tariff_id == tariff_id,
                Subscription.status.in_([SubscriptionStatus.ACTIVE.value, SubscriptionStatus.TRIAL.value]),
            )
        )
        subscriptions = result.scalars().all()

        if not subscriptions:
            return PropagateSquadsResult(total=0, synced=0)

        for sub in subscriptions:
            sub.connected_squads = squads_to_set
        await db.commit()

        # Предзагружаем пользователей и тарифы — никаких DB-операций внутри gather
        user_ids = [sub.user_id for sub in subscriptions]
        users_result = await db.execute(select(User).where(User.id.in_(user_ids)))
        users_map = {u.id: u for u in users_result.scalars().all()}

        for sub in subscriptions:
            try:
                await db.refresh(sub, ['tariff'])
            except Exception as exc:
                logger.warning('Не удалось предзагрузить тариф подписки', subscription_id=sub.id, error=exc)

        # Вычисляем стратегию сброса трафика один раз — все подписки одного тарифа
        sample_tariff = subscriptions[0].tariff or None
        traffic_strategy = get_traffic_reset_strategy(sample_tariff)

        # Параллельная синхронизация: один API-клиент, только HTTP-вызовы внутри gather
        failed_ids: list[int] = []
        synced = 0

        async with self.get_api_client() as api:
            semaphore = asyncio.Semaphore(concurrency)

            async def _sync_one(sub: Subscription) -> bool:
                async with semaphore:
                    try:
                        user = users_map.get(sub.user_id)
                        if not user:
                            return False
                        if settings.is_multi_tariff_enabled():
                            remnawave_uuid = sub.remnawave_uuid
                            if not remnawave_uuid:
                                logger.warning(
                                    'Multi-tariff: subscription has no remnawave_uuid, skipping squad sync',
                                    subscription_id=sub.id,
                                    user_id=sub.user_id,
                                )
                                return False
                        else:
                            remnawave_uuid = user.remnawave_uuid
                        if not remnawave_uuid:
                            return False

                        current_time = datetime.now(UTC)
                        is_actually_active = (
                            sub.status in (SubscriptionStatus.ACTIVE.value, SubscriptionStatus.TRIAL.value)
                            and sub.end_date > current_time
                        )

                        user_tag = self._resolve_user_tag(sub)
                        ext_squad_uuid = sub.tariff.external_squad_uuid if sub.tariff else None
                        hwid_limit = resolve_hwid_device_limit_for_payload(sub)

                        update_kwargs = dict(
                            uuid=remnawave_uuid,
                            status=UserStatus.ACTIVE if is_actually_active else UserStatus.DISABLED,
                            expire_at=sub.end_date
                            if is_actually_active
                            else max(sub.end_date, current_time + timedelta(minutes=1)),
                            traffic_limit_bytes=self._gb_to_bytes(sub.traffic_limit_gb),
                            traffic_limit_strategy=traffic_strategy,
                            telegram_id=user.telegram_id,
                            email=user.email,
                            description=settings.format_remnawave_user_description(
                                full_name=user.full_name,
                                username=user.username,
                                telegram_id=user.telegram_id,
                                email=user.email,
                                user_id=user.id,
                            ),
                        )

                        if sub.connected_squads:
                            update_kwargs['active_internal_squads'] = sub.connected_squads

                        if user_tag is not None:
                            update_kwargs['tag'] = user_tag

                        if hwid_limit is not None:
                            update_kwargs['hwid_device_limit'] = hwid_limit

                        # Не отправляем null — RemnaWave API не принимает null для externalSquadUuid (A039)
                        if ext_squad_uuid is not None:
                            update_kwargs['external_squad_uuid'] = ext_squad_uuid

                        updated_user = await api.update_user(**update_kwargs)

                        # Сохраняем в памяти — commit будет после gather
                        sub.subscription_url = updated_user.subscription_url
                        sub.subscription_crypto_link = updated_user.happ_crypto_link
                        return True

                    except Exception as e:
                        logger.warning(
                            'Не удалось обновить сквады в RemnaWave',
                            subscription_id=sub.id,
                            user_id=sub.user_id,
                            error=e,
                        )
                        return False

            results = await asyncio.gather(*[_sync_one(sub) for sub in subscriptions])

        for i, success in enumerate(results):
            if success:
                synced += 1
            else:
                failed_ids.append(subscriptions[i].id)

        # Один commit после всех API-вызовов
        try:
            await db.commit()
        except Exception as commit_error:
            logger.error('Ошибка фиксации транзакции при синхронизации скводов', error=commit_error)
            await db.rollback()
            failed_ids = [sub.id for sub in subscriptions]
            synced = 0

        propagate_result = PropagateSquadsResult(total=len(subscriptions), synced=synced, failed_ids=failed_ids)

        if failed_ids:
            logger.warning(
                'Частичная синхронизация скводов с RemnaWave',
                tariff_id=tariff_id,
                total=propagate_result.total,
                synced=synced,
                failed_ids=failed_ids,
            )
        else:
            logger.info(
                'Обновлены сквады подписок для тарифа',
                tariff_id=tariff_id,
                total=propagate_result.total,
                synced=synced,
            )

        return propagate_result
