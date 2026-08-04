import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.crud.server_squad import get_all_server_squads
from app.database.crud.user import get_user_by_id
from app.database.models import Subscription, SubscriptionStatus, User
from app.external.remnawave_api import (
    RemnaWaveAPI,
    RemnaWaveAPIError,
    RemnaWaveInvalidUserIdError,
    RemnaWaveTransientError,
    RemnaWaveUser,
    TrafficLimitStrategy,
    UserStatus,
    is_user_not_found_error,
)
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
                'RemnaWave API недоступен. Подписочный сервис будет работать в оффлайн-режиме.',
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

            from app.services.grace_access_runtime import lock_grace_sensitive_panel_updates

            await lock_grace_sensitive_panel_updates(db, (subscription.id,))
            validation_success = await self.validate_and_clean_subscription(db, subscription, user)
            if not validation_success:
                logger.error('Ошибка валидации подписки для пользователя', _format_user_log=self._format_user_log(user))
                await db.rollback()
                return None

            open_grace_ids = await lock_grace_sensitive_panel_updates(db, (subscription.id,))
            await db.flush((subscription, user))
            await db.refresh(subscription)
            await db.refresh(user)
            preserve_open_grace = (
                subscription.id in open_grace_ids
                and user.status == 'active'
                and subscription.actual_status in (SubscriptionStatus.EXPIRED.value, SubscriptionStatus.LIMITED.value)
            )

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

                if preserve_open_grace:
                    remnawave_id = (
                        subscription.remnawave_id if settings.is_multi_tariff_enabled() else user.remnawave_id
                    )
                    if not remnawave_id:
                        logger.warning(
                            'Remnawave create/update deferred while grace is open and panel id is missing',
                            subscription_id=subscription.id,
                        )
                        await db.commit()
                        return None
                    metadata_kwargs: dict[str, Any] = {
                        'user_id': remnawave_id,
                        'description': settings.format_remnawave_user_description(
                            full_name=user.full_name,
                            username=user.username,
                            telegram_id=user.telegram_id,
                            email=user.email,
                            user_id=user.id,
                        ),
                    }
                    if user.telegram_id is not None:
                        metadata_kwargs['telegram_id'] = user.telegram_id
                    if user.email is not None:
                        metadata_kwargs['email'] = user.email
                    if hwid_limit is not None:
                        metadata_kwargs['hwid_device_limit'] = hwid_limit
                    if user_tag is not None:
                        metadata_kwargs['tag'] = user_tag
                    updated_user = await api.update_user(**metadata_kwargs)
                    subscription.remnawave_short_uuid = updated_user.short_uuid
                    subscription.subscription_url = updated_user.subscription_url
                    subscription.subscription_crypto_link = updated_user.happ_crypto_link
                    await db.commit()
                    return updated_user

                # Multi-tariff mode: each subscription has its own Remnawave user
                if settings.is_multi_tariff_enabled():
                    updated_user = await self._create_or_update_remnawave_user_multi(
                        api,
                        user,
                        subscription,
                        db=db,
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
                if await self._panel_id_is_free_for(db, subscription, updated_user.id):
                    subscription.remnawave_id = updated_user.id
                else:
                    # `uq_subscriptions_remnawave_id` частично-уникален. В
                    # single-tariff соседняя подписка того же человека уже держит
                    # этот аккаунт — ровно то состояние, которое оставляет бэкфилл.
                    # Записать id второй строке значит словить IntegrityError уже
                    # ПОСЛЕ успешного PATCH в панель и откатить оплату вместе с ним.
                    # Адресация не теряется: остаётся `users.remnawave_id`.
                    logger.warning(
                        '⚠️ Панельный id уже закреплён за другой подпиской — адресуем через пользователя',
                        subscription_id=subscription.id,
                        remnawave_id=updated_user.id,
                    )
                # Legacy field — keep in sync for single-mode backward compat
                if not settings.is_multi_tariff_enabled():
                    user.remnawave_id = updated_user.id

                await db.commit()

                logger.info('✅ Создан/обновлен RemnaWave пользователь для подписки', subscription_id=subscription.id)
                logger.info('🔗 Ссылка на подписку', subscription_url=updated_user.subscription_url)
                strategy_name = settings.DEFAULT_TRAFFIC_RESET_STRATEGY
                logger.info('📊 Стратегия сброса трафика', strategy_name=strategy_name)
                return updated_user

        except asyncio.CancelledError:
            await db.rollback()
            raise
        except RemnaWaveAPIError as e:
            await db.rollback()
            logger.error('Ошибка RemnaWave API', error=e)
            return None
        except Exception as e:
            await db.rollback()
            logger.error('Ошибка создания RemnaWave пользователя', error=e)
            return None

    async def _adopt_panel_user_by_short_uuid(self, api: RemnaWaveAPI, subscription) -> RemnaWaveUser | None:
        """Опознать уже существующего панельного пользователя подписки по shortUuid.

        Нужно ровно для строк, привязанных до апгрейда на 3.0.0: `uuid` из API
        исчез, числовой id ещё не проставлен бэкфилом, но `shortUuid` панель
        по-прежнему знает (`GET /api/users/by-short-uuid/{shortUuid}` сохранился).

        Возвращает None, если опознать нечем или не удалось — вызывающий тогда
        честно создаёт нового пользователя. Транзиентную ошибку пробрасываем:
        «панель моргнула» не должно превращаться в дубль аккаунта.
        """
        short_uuid = (getattr(subscription, 'remnawave_short_uuid', None) or '').strip()
        if not short_uuid:
            return None
        # Отсутствие аккаунта доказывает ТОЛЬКО 404 (его `get_user_by_short_uuid`
        # отдаёт как None). Любой другой ответ — 5xx на рестарте панели, 429,
        # таймаут — означает «не знаем», и проглотить его здесь нельзя: вызывающий
        # воспримет None как «создавать нового» и заведёт дубль рядом с живым
        # оплаченным аккаунтом. Пробрасываем — пусть операция честно упадёт.
        panel_user = await api.get_user_by_short_uuid(short_uuid)
        if panel_user is None:
            return None
        logger.info(
            '🔗 Панельный пользователь опознан по short_uuid — дубль не создаём',
            subscription_id=getattr(subscription, 'id', None),
            remnawave_id=panel_user.id,
        )
        return panel_user

    async def _panel_id_is_free_for(self, db: AsyncSession, subscription, panel_id: int | None) -> bool:
        """Не держит ли этот панельный id уже ДРУГАЯ строка подписок.

        Колонка частично уникальна, и в single-tariff все подписки одного
        человека адресуют один и тот же панельный аккаунт, поэтому конфликт —
        штатная ситуация, а не аномалия.
        """
        if panel_id is None:
            return False
        other = (
            await db.execute(
                select(Subscription.id)
                .where(
                    Subscription.remnawave_id == int(panel_id),
                    Subscription.id != getattr(subscription, 'id', None),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        return other is None

    async def _adopt_panel_id_for_update(self, db: AsyncSession, subscription, user, multi_tariff: bool) -> int | None:
        """Достать числовой id панели по shortUuid и сохранить его на строке.

        Нужно ровно для окна между миграцией 0104 и прогоном бэкфила, когда
        `remnawave_id` пуст у всех доапгрейдных строк. Возвращает None, если
        опознать нечем или панель этот shortUuid не знает — тогда вызывающий
        честно откажется от обновления.
        """
        short_uuid = (getattr(subscription, 'remnawave_short_uuid', None) or '').strip()
        if not short_uuid:
            return None
        try:
            async with self.get_api_client() as api:
                adopted = await api.get_user_by_short_uuid(short_uuid)
        except Exception as error:
            # Транзиент/деградация панели — не повод считать, что аккаунта нет.
            logger.warning(
                '⚠️ Не удалось опознать панельного пользователя по short_uuid для обновления',
                subscription_id=getattr(subscription, 'id', None),
                error=error,
            )
            return None
        if adopted is None:
            return None

        logger.info(
            '🔗 Панельный id восстановлен по short_uuid при обновлении',
            subscription_id=getattr(subscription, 'id', None),
            remnawave_id=adopted.id,
        )
        if multi_tariff:
            # Четвёртый писатель в частично-уникальную колонку. Но здесь мало
            # НЕ ЗАПИСАТЬ колонку: возвращённый id вызывающий тут же отправляет
            # в панель PATCH-ом. Если аккаунт держит соседняя подписка, такой
            # PATCH перепишет ЕЁ срок, лимиты и сквады — и это необратимо, в
            # отличие от отката транзакции. Поэтому отказываемся целиком.
            if not await self._panel_id_is_free_for(db, subscription, adopted.id):
                logger.warning(
                    '⚠️ Панельный id уже закреплён за другой подпиской — обновление отменено',
                    subscription_id=getattr(subscription, 'id', None),
                    remnawave_id=adopted.id,
                )
                return None
            subscription.remnawave_id = adopted.id
        else:
            # Только на User: в single-tariff все подписки одного пользователя
            # указывают на ОДИН панельный аккаунт, а `uq_subscriptions_remnawave_id`
            # частично-уникален — записав id второй строке, мы бы словили
            # IntegrityError и откатили вместе с ним уже применённое пополнение.
            user.remnawave_id = adopted.id
        await db.flush((subscription, user))
        return adopted.id

    async def _create_or_update_remnawave_user_multi(
        self,
        api: RemnaWaveAPI,
        user: User,
        subscription: Subscription,
        *,
        db: AsyncSession | None = None,
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
        now = datetime.now(UTC)
        is_actually_active = (
            user.status == 'active'
            and subscription.actual_status in (SubscriptionStatus.ACTIVE.value, SubscriptionStatus.TRIAL.value)
            and subscription.end_date > now
        )
        common_kwargs = dict(
            status=UserStatus.ACTIVE if is_actually_active else UserStatus.DISABLED,
            expire_at=(
                subscription.end_date if is_actually_active else max(subscription.end_date, now + timedelta(minutes=1))
            ),
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
        if subscription.remnawave_id:
            try:
                existing = await api.get_user_by_id(subscription.remnawave_id)
                if existing:
                    if settings.RESET_DEVICES_ON_RENEWAL:
                        if not await api.reset_user_devices(existing.id):
                            logger.error('⚠️ Не удалось сбросить HWID', panel_user_id=existing.id)

                    updated = await api.update_user(user_id=existing.id, **common_kwargs)
                    if reset_traffic:
                        await self._reset_user_traffic(api, updated.id, user, reset_reason)
                    return updated
            except RemnaWaveInvalidUserIdError:
                # Непригодный локальный id — это баг в данных бота, а не «юзера в
                # панели нет». Уйти отсюда в ветку создания значило бы плодить
                # дубли панельных пользователей на каждом проходе.
                raise
            except RemnaWaveTransientError:
                # Панель недоступна/таймаут — это тоже НЕ «пользователя нет».
                # Создание нового аккаунта на транзиентной ошибке даёт дубль
                # ровно тогда, когда оригинал жив и панель просто моргнула.
                raise
            except Exception:
                logger.warning(
                    '⚠️ Не удалось найти Remnawave юзера по id подписки, создаём нового',
                    subscription_id=subscription.id,
                    remnawave_id=subscription.remnawave_id,
                )

        # Строка могла быть привязана к панели ДО апгрейда на 3.0.0: числового
        # id у неё ещё нет, но shortUuid сохранился и переживает апгрейд.
        # Без этой попытки каждая такая подписка получила бы ВТОРОЙ панельный
        # аккаунт при первом же продлении, а оплаченный оригинал осиротел бы.
        adopted = await self._adopt_panel_user_by_short_uuid(api, subscription)
        if adopted is not None:
            # Та же защита частично-уникального индекса, что и на других
            # писателях: иначе IntegrityError прилетал бы уже ПОСЛЕ update_user,
            # то есть панель изменена, а транзакция отката.
            if db is None or await self._panel_id_is_free_for(db, subscription, adopted.id):
                subscription.remnawave_id = adopted.id
            else:
                logger.warning(
                    '⚠️ Панельный id уже закреплён за другой подпиской — колонку не трогаем',
                    subscription_id=getattr(subscription, 'id', None),
                    remnawave_id=adopted.id,
                )
            if settings.RESET_DEVICES_ON_RENEWAL:
                if not await api.reset_user_devices(adopted.id):
                    logger.error('⚠️ Не удалось сбросить HWID', panel_user_id=adopted.id)
            updated = await api.update_user(user_id=adopted.id, **common_kwargs)
            if reset_traffic:
                await self._reset_user_traffic(api, updated.id, user, reset_reason)
            return updated

        # New subscription — create a NEW Remnawave user.
        # short_id (6 hex chars) приклеивается к base; helper гарантирует, что
        # итоговая длина ≤ REMNAWAVE_USERNAME_MAX_LENGTH (исторический баг с
        # `didykmarin_email_didykmarin_703_49883b` — 38 chars вместо 36).
        #
        # КРИТИЧНО для multi-tariff: суффикс ОБЯЗАН быть уникален per-subscription,
        # иначе два тарифа одного юзера собирают ОДИНАКОВЫЙ username → панель
        # возвращает одного и того же пользователя → общий HWID-лимит (баг «лимит
        # по наименьшему тарифу»). На пустой/legacy short_id ('' из server_default)
        # падаем на детерминированный per-subscription суффикс по id.
        short_suffix = subscription.remnawave_short_id or f'sub{subscription.id}'
        username = settings.build_remnawave_subscription_username(
            full_name=user.full_name,
            username=user.username,
            telegram_id=user.telegram_id,
            email=user.email,
            user_id=user.id,
            suffix=f'_{short_suffix}',
        )

        updated_user = await api.create_user(username=username, **common_kwargs)
        if reset_traffic:
            await self._reset_user_traffic(api, updated_user.id, user, reset_reason)
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

        # Search for existing Remnawave user.
        # Маршруты by-telegram-id/by-email в 3.0.0 удалены — их роль (найти уже
        # существующего панельного юзера, когда локальная привязка потерялась)
        # играют стрим-фильтры. Без этого шага бот создавал бы дубль панельного
        # пользователя на каждом проходе синка.
        existing_users: list[RemnaWaveUser] = []
        # Порядок — по убыванию точности. Сначала два ТОЧНЫХ адреса: id
        # пользователя и id самой подписки. Второй бэкфилл заполняет и в
        # single-tariff (например, перенося его на живую строку), а раньше его
        # тут не спрашивали вовсе.
        for exact_id in (user.remnawave_id, getattr(subscription, 'remnawave_id', None)):
            if existing_users or not exact_id:
                continue
            try:
                existing_user = await api.get_user_by_id(exact_id)
                if existing_user:
                    existing_users = [existing_user]
            except Exception:
                pass

        adoption_error: Exception | None = None
        if not existing_users:
            # shortUuid — тоже точный ключ, и он обязан идти ПЕРЕД поиском по
            # telegramId: у человека может быть несколько панельных аккаунтов, и
            # тогда телеграм-поиск вернёт список, из которого ниже берётся
            # первый попавшийся. Именно поэтому бэкфилл в такой ситуации
            # отказывается угадывать — здесь нельзя вести себя иначе.
            try:
                adopted = await self._adopt_panel_user_by_short_uuid(api, subscription)
            except Exception as error:
                # «Панель моргнула» — это «не знаем», а не «аккаунта нет». В
                # прежней позиции последнего шанса такую ошибку можно было
                # ронять сразу; теперь этот шаг идёт первым, и падение отменяло
                # бы операции, которые прекрасно решаются по telegramId.
                # Поэтому идём дальше, но запоминаем: если больше ничем не
                # опознаем, создавать нового НЕЛЬЗЯ — упадём честно.
                adoption_error = error
                adopted = None
                logger.warning(
                    '⚠️ Не удалось опознать панельного пользователя по short_uuid — пробуем другие ключи',
                    subscription_id=getattr(subscription, 'id', None),
                    error=error,
                )
            if adopted is not None:
                existing_users = [adopted]

        if not existing_users and user.telegram_id:
            existing_users = await api.find_users_by_telegram_id(user.telegram_id)

        if not existing_users and user.email:
            try:
                existing_users = await api.find_users_by_email(user.email)
            except Exception:
                pass

        if not existing_users and adoption_error is not None:
            # Ничем не опознали, а точный ключ остался непроверенным: создание
            # здесь завело бы дубль рядом с живым оплаченным аккаунтом.
            raise adoption_error

        if len(existing_users) > 1:
            logger.warning(
                '⚠️ У пользователя несколько панельных аккаунтов, точного ключа нет — берём первый',
                user_id=user.id,
                subscription_id=getattr(subscription, 'id', None),
                candidates=[u.id for u in existing_users],
            )

        now = datetime.now(UTC)
        is_actually_active = (
            user.status == 'active'
            and subscription.actual_status in (SubscriptionStatus.ACTIVE.value, SubscriptionStatus.TRIAL.value)
            and subscription.end_date > now
        )
        common_kwargs = dict(
            status=UserStatus.ACTIVE if is_actually_active else UserStatus.DISABLED,
            expire_at=(
                subscription.end_date if is_actually_active else max(subscription.end_date, now + timedelta(minutes=1))
            ),
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
            logger.info('🔄 Найден существующий пользователь в панели', _format_user_log=self._format_user_log(user))
            remnawave_user = existing_users[0]

            if settings.RESET_DEVICES_ON_RENEWAL:
                if await api.reset_user_devices(remnawave_user.id):
                    logger.info('🔧 Сброшены HWID устройства', _format_user_log=self._format_user_log(user))
                else:
                    logger.error('⚠️ Не удалось сбросить HWID', panel_user_id=remnawave_user.id)

            updated_user = await api.update_user(user_id=remnawave_user.id, **common_kwargs)
            if reset_traffic:
                await self._reset_user_traffic(api, updated_user.id, user, reset_reason)
            return updated_user

        logger.info('🆕 Создаем нового пользователя в панели', _format_user_log=self._format_user_log(user))
        username = settings.format_remnawave_username(
            full_name=user.full_name,
            username=user.username,
            telegram_id=user.telegram_id,
            email=user.email,
            user_id=user.id,
        )
        updated_user = await api.create_user(username=username, **common_kwargs)
        if reset_traffic:
            await self._reset_user_traffic(api, updated_user.id, user, reset_reason)
        return updated_user

    async def update_remnawave_user(
        self,
        db: AsyncSession,
        subscription: Subscription,
        *,
        reset_traffic: bool = False,
        reset_reason: str | None = None,
        sync_squads: bool = True,
    ) -> RemnaWaveUser | None:
        try:
            user = await get_user_by_id(db, subscription.user_id)
            if not user:
                logger.error('Пользователь не найден', user_id=subscription.user_id)
                return None

            # Resolve the Remnawave panel id: prefer subscription-level in multi-tariff mode
            multi_tariff = settings.is_multi_tariff_enabled()
            remnawave_id = subscription.remnawave_id if multi_tariff else user.remnawave_id

            if not remnawave_id:
                # Строка была привязана к панели до апгрейда на 3.0.0: числового
                # id ей ещё не проставил бэкфил, но shortUuid панель знает.
                # Просто сдаться здесь нельзя — вызывающие этот метод (докупка
                # трафика и устройств, смена сквадов) результат не проверяют:
                # деньги списываются и коммитятся, а в панель не уезжает ничего.
                remnawave_id = await self._adopt_panel_id_for_update(db, subscription, user, multi_tariff)

            if not remnawave_id:
                logger.error(
                    'RemnaWave id не найден для пользователя',
                    user_id=subscription.user_id,
                    subscription_id=subscription.id,
                )
                return None

            # Routine outbound updates must not replace a temporary Telegram-only
            # overlay with the still-expired/limited billing state. A real renewal
            # changes actual_status to active and is intentionally allowed.
            from app.services.grace_access_runtime import (
                apply_recovered_grace_update_locked,
                lock_grace_sensitive_panel_updates,
            )

            open_grace_ids = await lock_grace_sensitive_panel_updates(db, (subscription.id,))
            await db.flush((subscription, user))
            # The caller may have loaded these objects before waiting for the
            # grace lock.  Re-read scalar billing/user state under that lock so
            # an older sync cannot overwrite a renewal that just completed.
            await db.refresh(subscription)
            await db.refresh(user)
            preserve_open_grace = (
                subscription.id in open_grace_ids
                and user.status == 'active'
                and subscription.actual_status in (SubscriptionStatus.EXPIRED.value, SubscriptionStatus.LIMITED.value)
            )
            if preserve_open_grace:
                logger.info(
                    'Routine Remnawave update masks grace-owned fields',
                    subscription_id=subscription.id,
                )
                async with self.get_api_client() as api:
                    metadata_kwargs: dict[str, Any] = {
                        'user_id': remnawave_id,
                        'description': settings.format_remnawave_user_description(
                            full_name=user.full_name,
                            username=user.username,
                            telegram_id=user.telegram_id,
                            email=user.email,
                            user_id=user.id,
                        ),
                    }
                    if user.telegram_id is not None:
                        metadata_kwargs['telegram_id'] = user.telegram_id
                    if user.email is not None:
                        metadata_kwargs['email'] = user.email
                    hwid_limit = resolve_hwid_device_limit_for_payload(subscription)
                    if hwid_limit is not None:
                        metadata_kwargs['hwid_device_limit'] = hwid_limit
                    user_tag = self._resolve_user_tag(subscription)
                    if user_tag is not None:
                        metadata_kwargs['tag'] = user_tag
                    updated_user = await api.update_user(**metadata_kwargs)
                subscription.subscription_url = updated_user.subscription_url
                subscription.subscription_crypto_link = updated_user.happ_crypto_link
                await db.commit()
                return updated_user

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
                    user_id=remnawave_id,
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

                completed_grace = False
                updated_user = None
                if subscription.id in open_grace_ids:
                    completed_grace, updated_user = await apply_recovered_grace_update_locked(
                        db,
                        api,
                        subscription.id,
                        update_kwargs=update_kwargs,
                        source='subscription_service.update_remnawave_user',
                    )
                if not completed_grace:
                    updated_user = await api.update_user(**update_kwargs)
                if updated_user is None:
                    raise RemnaWaveAPIError('Remnawave returned no user after subscription renewal update')

                if reset_traffic:
                    if settings.is_multi_tariff_enabled():
                        reset_id = subscription.remnawave_id
                        if not reset_id:
                            logger.warning(
                                'Multi-tariff: subscription has no remnawave_id, skipping traffic reset',
                                subscription_id=subscription.id,
                                user_id=subscription.user_id,
                            )
                    else:
                        reset_id = user.remnawave_id
                    if reset_id:
                        await self._reset_user_traffic(
                            api,
                            reset_id,
                            user,
                            reset_reason,
                        )

                subscription.subscription_url = updated_user.subscription_url
                subscription.subscription_crypto_link = updated_user.happ_crypto_link
                await db.commit()

                status_text = 'активным' if is_actually_active else 'истёкшим'
                logger.info(
                    '✅ Обновлен RemnaWave пользователь со статусом',
                    remnawave_id=remnawave_id,
                    status_text=status_text,
                )
                strategy_name = settings.DEFAULT_TRAFFIC_RESET_STRATEGY
                logger.info('📊 Стратегия сброса трафика', strategy_name=strategy_name)
                return updated_user

        except asyncio.CancelledError:
            # ``CancelledError`` is not an Exception on supported Python
            # versions.  Timeouts must still release advisory/SQLite locks.
            await db.rollback()
            raise
        except RemnaWaveAPIError as e:
            # Rollback ДО пересоздания: advisory-локи grace должны быть отпущены
            # прежде, чем recreate re-входит в create_remnawave_user со своим локом.
            await db.rollback()
            if is_user_not_found_error(e):
                # Пользователя удалили из панели, пока подписка жива в боте, —
                # пересоздаём вместо ошибки (create-флоу сам найдёт/создаст
                # панель-юзера и сохранит новый id и ссылки в подписку).
                return await self.recreate_deleted_panel_user(
                    db, subscription, reset_traffic=reset_traffic, reset_reason=reset_reason
                )
            logger.error('Ошибка RemnaWave API', error=e)
            return None
        except Exception as e:
            await db.rollback()
            logger.error('Ошибка обновления RemnaWave пользователя', error=e)
            return None

    async def recreate_deleted_panel_user(
        self,
        db: AsyncSession,
        subscription: Subscription,
        *,
        reset_traffic: bool = False,
        reset_reason: str | None = None,
    ) -> RemnaWaveUser | None:
        """Пересоздаёт панель-юзера, удалённого из RemnaWave при живой подписке.

        Только для действующих подписок: пересоздавать DISABLED-юзера ради
        истёкшей подписки не нужно — админ удалил его намеренно.
        """
        is_actually_active = subscription.status in (
            SubscriptionStatus.ACTIVE.value,
            SubscriptionStatus.TRIAL.value,
        ) and subscription.end_date > datetime.now(UTC)
        if not is_actually_active:
            logger.info(
                'Панель-юзер удалён из RemnaWave, подписка неактивна — пересоздание не требуется',
                subscription_id=subscription.id,
                user_id=subscription.user_id,
            )
            return None

        logger.warning(
            '⚠️ Панель-юзер удалён из RemnaWave при активной подписке — пересоздаём',
            subscription_id=subscription.id,
            user_id=subscription.user_id,
        )
        return await self.create_remnawave_user(
            db, subscription, reset_traffic=reset_traffic, reset_reason=reset_reason
        )

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
        panel_user_id: int,
        user,  # User object вместо telegram_id
        reset_reason: str | None = None,
    ) -> None:
        if not panel_user_id:
            return

        try:
            await api.reset_user_traffic(panel_user_id)
            reason_text = f' ({reset_reason})' if reset_reason else ''
            logger.info(
                '🔄 Сброшен трафик RemnaWave', _format_user_log=self._format_user_log(user), reason_text=reason_text
            )
        except Exception as exc:
            logger.warning(
                '⚠️ Не удалось сбросить трафик RemnaWave', _format_user_log=self._format_user_log(user), error=exc
            )

    async def disable_remnawave_user(self, panel_user_id: int, db: AsyncSession | None = None) -> bool:
        """``db`` — сессия вызывающего, уже держащего grace-локи (пути удаления
        после ensure_no_open_grace_*): без её проброса grace-обёртка открыла бы
        вторую сессию и самодедлочилась об advisory-локи первой."""
        try:
            from app.services.grace_access_runtime import set_panel_user_enabled_state_grace_safe

            async with self.get_api_client() as api:
                await set_panel_user_enabled_state_grace_safe(
                    api,
                    panel_user_id,
                    enabled=False,
                    db=db,
                )
                logger.info('✅ Отключен RemnaWave пользователь', panel_user_id=panel_user_id)
                return True

        except Exception as e:
            error_msg = str(e).lower()
            # "User already disabled" - считаем успехом
            if 'already disabled' in error_msg:
                logger.info('✅ RemnaWave пользователь уже отключен', panel_user_id=panel_user_id)
                return True
            logger.error('Ошибка отключения RemnaWave пользователя', error=e)
            return False

    async def delete_remnawave_user(self, panel_user_id: int) -> bool:
        """Полное удаление пользователя из панели RemnaWave (хуки прекращаются)."""
        try:
            async with self.get_api_client() as api:
                await api.delete_user(panel_user_id)
                logger.info('🗑 Удалён RemnaWave пользователь', panel_user_id=panel_user_id)
                return True

        except Exception as e:
            error_msg = str(e).lower()
            if 'not found' in error_msg or 'not exist' in error_msg:
                logger.info('🗑 RemnaWave пользователь уже удалён', panel_user_id=panel_user_id)
                return True
            logger.error('Ошибка удаления RemnaWave пользователя', error=e, panel_user_id=panel_user_id)
            return False

    async def enable_remnawave_user(self, panel_user_id: int, db: AsyncSession | None = None) -> bool:
        """Включить пользователя в RemnaWave (реактивация).

        ``db`` — сессия вызывающего, уже держащего grace-локи этих подписок
        (см. disable_remnawave_user)."""
        try:
            from app.services.grace_access_runtime import set_panel_user_enabled_state_grace_safe

            async with self.get_api_client() as api:
                await set_panel_user_enabled_state_grace_safe(
                    api,
                    panel_user_id,
                    enabled=True,
                    db=db,
                )
                logger.info('✅ Включен RemnaWave пользователь', panel_user_id=panel_user_id)
                return True

        except Exception as e:
            error_msg = str(e).lower()
            # "User already enabled" - считаем успехом
            if 'already enabled' in error_msg:
                logger.info('✅ RemnaWave пользователь уже включен', panel_user_id=panel_user_id)
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
                revoke_id = subscription.remnawave_id
                if not revoke_id:
                    logger.warning(
                        'Multi-tariff: subscription has no remnawave_id, cannot revoke',
                        subscription_id=subscription.id,
                        user_id=subscription.user_id,
                    )
                    return None
            else:
                revoke_id = user.remnawave_id
            if not revoke_id:
                return None

            async with self.get_api_client() as api:
                updated_user = await api.revoke_user_subscription(revoke_id)

                subscription.remnawave_short_uuid = updated_user.short_uuid
                subscription.subscription_url = updated_user.subscription_url
                subscription.subscription_crypto_link = updated_user.happ_crypto_link
                await db.commit()

                logger.info('✅ Обновлена ссылка подписки', _format_user_log=self._format_user_log(user))
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
                sync_id = subscription.remnawave_id
                if not sync_id:
                    logger.warning(
                        'Multi-tariff: subscription has no remnawave_id, cannot sync usage',
                        subscription_id=subscription.id,
                        user_id=subscription.user_id,
                    )
                    return False
            else:
                sync_id = user.remnawave_id
            if not sync_id:
                return False

            async with self.get_api_client() as api:
                remnawave_user = await api.get_user_by_id(sync_id)
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
            panel_user_id = subscription.remnawave_id if settings.is_multi_tariff_enabled() else user.remnawave_id
            needs_sync = not subscription.subscription_url or not panel_user_id

            if not needs_sync:
                # Проверяем, существует ли пользователь в RemnaWave
                try:
                    async with self.get_api_client() as api:
                        remnawave_user = await api.get_user_by_id(panel_user_id)
                        if not remnawave_user:
                            needs_sync = True
                            logger.warning(
                                'Пользователь не найден в RemnaWave, требуется синхронизация',
                                remnawave_id=panel_user_id,
                            )
                except Exception as check_error:
                    logger.warning('Не удалось проверить пользователя в RemnaWave', check_error=check_error)
                    # Продолжаем, возможно проблема временная

            if not needs_sync:
                return True, None

            logger.info(
                'Синхронизация подписки с RemnaWave',
                subscription_id=subscription.id,
                subscription_url=bool(subscription.subscription_url),
                remnawave_id=bool(panel_user_id),
            )

            # Пытаемся синхронизировать
            result = None
            if panel_user_id:
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
                        remnawave_id=panel_user_id,
                    )
                    # Сбрасываем старый id, create_remnawave_user установит новый.
                    # Исторический uuid обнуляем ВМЕСТЕ с ним (до 3.0.0 так и было):
                    # иначе строка останется с uuid удалённого аккаунта при живом
                    # новом id, а бэкфилл строит по этой паре карту uuid -> id и
                    # выдаст grace-сессии мёртвого аккаунта живой, оплаченный.
                    if settings.is_multi_tariff_enabled():
                        subscription.remnawave_id = None
                        subscription.remnawave_uuid = None
                    else:
                        user.remnawave_id = None
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
            # Отдельно от `needs_cleanup`: «панель не знает этот id» и «аккаунт
            # принадлежит другому человеку» — разные вещи. Перепривязка по
            # shortUuid допустима только для первого; для второго она означала бы
            # «оставить чужой аккаунт», ровно то, ради чего проверка и стоит.
            panel_lost_the_id = False
            foreign_short_uuid = False
            user_log = self._format_user_log(user)

            # In multi-tariff mode, validate per-subscription panel id, not user-level id.
            # В single-tariff берём id пользователя, но при пустом — id самой
            # подписки: бэкфилл штатно оставляет строку пользователя
            # неразрешённой, заполнив при этом подписку, и без этого фолбэка
            # проверка считала бы аккаунт отсутствующим и стирала идентичность,
            # которую только что восстановили, — с пересозданием дубля в панели.
            if settings.is_multi_tariff_enabled():
                check_id = subscription.remnawave_id
            else:
                check_id = user.remnawave_id or subscription.remnawave_id

            if check_id:
                try:
                    async with self.get_api_client() as api:
                        remnawave_user = await api.get_user_by_id(check_id)

                        if not remnawave_user:
                            logger.warning(
                                '⚠️ id не найден в панели',
                                user_log=user_log,
                                remnawave_id=check_id,
                            )
                            needs_cleanup = True
                            panel_lost_the_id = True
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
                    # A timeout/5xx is not proof that the panel user vanished.
                    # Preserve the panel id and abort so a retry cannot create a
                    # duplicate Remnawave account.
                    return False

            # Гейт по `panel_lost_the_id`, а НЕ по «не было check_id»: строка с
            # протухшим числовым id (панель его не знает) иначе теряла последний
            # шанс — очистка стирала единственный точный ключ восстановления.
            # И НЕ по `needs_cleanup`: тот же флаг ставит несовпадение владельца,
            # а для него перепривязка недопустима.
            if subscription.remnawave_short_uuid and (panel_lost_the_id or not check_id):
                # Раньше это однозначно значило «мусорные данные». После апгрейда
                # на 3.0.0 у той же комбинации есть второе прочтение: строка была
                # привязана к панели, а числовой id ей ещё не проставил бэкфил.
                # Разница принципиальна — очистка стирает remnawave_short_uuid,
                # единственный точный ключ восстановления связи. Поэтому решает
                # не догадка, а сама панель.
                try:
                    async with self.get_api_client() as api:
                        panel_user = await api.get_user_by_short_uuid(subscription.remnawave_short_uuid)
                except Exception as api_error:
                    logger.error(
                        '❌ Не удалось проверить short_uuid в панели — очистку не делаем',
                        subscription_id=subscription.id,
                        api_error=api_error,
                    )
                    return False

                panel_telegram_id = getattr(panel_user, 'telegram_id', None) if panel_user is not None else None
                if (
                    panel_user is not None
                    and user.telegram_id
                    and panel_telegram_id
                    and panel_telegram_id != user.telegram_id
                ):
                    # Нашли по shortUuid, но аккаунт чужой — привязывать нельзя.
                    logger.warning(
                        '⚠️ Аккаунт по short_uuid принадлежит другому telegram_id — не привязываем',
                        subscription_id=subscription.id,
                        panel_telegram_id=panel_telegram_id,
                    )
                    panel_user = None
                    needs_cleanup = True
                    foreign_short_uuid = True

                if panel_user is not None:
                    logger.info(
                        '🔗 Панельный пользователь опознан по short_uuid, проставляем remnawave_id',
                        subscription_id=subscription.id,
                        remnawave_id=panel_user.id,
                    )
                    # Та же защита частично-уникального индекса, что и в
                    # `create_remnawave_user`: соседняя подписка того же человека
                    # штатно держит этот аккаунт после бэкфила, и безусловная
                    # запись падала бы здесь на IntegrityError.
                    if await self._panel_id_is_free_for(db, subscription, panel_user.id):
                        subscription.remnawave_id = panel_user.id
                    else:
                        logger.warning(
                            '⚠️ Панельный id уже закреплён за другой подпиской — адресуем через пользователя',
                            subscription_id=subscription.id,
                            remnawave_id=panel_user.id,
                        )
                    if not settings.is_multi_tariff_enabled() and not user.remnawave_id:
                        user.remnawave_id = panel_user.id
                    await db.flush((subscription, user))
                    return True

                if foreign_short_uuid:
                    logger.warning('⚠️ short_uuid ведёт на аккаунт другого пользователя — связь рвём')
                else:
                    logger.warning('⚠️ У подписки есть short_uuid, но панель его не знает')
                needs_cleanup = True

            if needs_cleanup:
                logger.info('🧹 Очищаем мусорные данные подписки', user_log=user_log)

                subscription.remnawave_short_uuid = None
                subscription.remnawave_id = None
                # Вместе с id обнуляем и исторический uuid — иначе строка несёт
                # uuid удалённого аккаунта, а следующий id будет уже от нового:
                # бэкфилл строит по этой паре карту и свяжет grace-сессию
                # мёртвого аккаунта с живым. До 3.0.0 uuid чистился здесь же.
                subscription.remnawave_uuid = None
                subscription.subscription_url = ''
                subscription.subscription_crypto_link = ''

                if not settings.is_multi_tariff_enabled():
                    user.remnawave_id = None
                    user.remnawave_uuid = None

                # Keep cleanup in the caller's transaction.  Committing here
                # could burn a coupon/payment if the following panel create
                # fails; create_remnawave_user commits only after API success.
                await db.flush((subscription, user))
                logger.info('✅ Мусорные данные подготовлены к очистке', user_log=user_log)

            return True

        except Exception as e:
            logger.error('❌ Ошибка валидации подписки', _format_user_log=self._format_user_log(user), error=e)
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

        # Параллельная синхронизация: один API-клиент, только HTTP-вызовы внутри gather
        failed_ids: list[int] = []
        synced = 0

        from app.services.grace_access_runtime import update_panel_user_grace_safe

        async with self.get_api_client() as api:
            semaphore = asyncio.Semaphore(concurrency)

            async def _sync_one(sub: Subscription) -> bool:
                async with semaphore:
                    try:
                        user = users_map.get(sub.user_id)
                        if not user:
                            return False
                        if settings.is_multi_tariff_enabled():
                            remnawave_id = sub.remnawave_id
                            if not remnawave_id:
                                logger.warning(
                                    'Multi-tariff: subscription has no remnawave_id, skipping squad sync',
                                    subscription_id=sub.id,
                                    user_id=sub.user_id,
                                )
                                return False
                        else:
                            remnawave_id = user.remnawave_id
                        if not remnawave_id:
                            return False

                        ext_squad_uuid = sub.tariff.external_squad_uuid if sub.tariff else None

                        update_kwargs = dict(
                            user_id=remnawave_id,
                            description=settings.format_remnawave_user_description(
                                full_name=user.full_name,
                                username=user.username,
                                telegram_id=user.telegram_id,
                                email=user.email,
                                user_id=user.id,
                            ),
                        )

                        # Пустой список не шлём: [] снял бы у панель-юзера ВСЕ
                        # сквады (у подписки без connected_squads это не намерение
                        # «отключить», а просто отсутствие данных) — как в dev.
                        if sub.connected_squads:
                            update_kwargs['active_internal_squads'] = sub.connected_squads

                        # Не отправляем null — RemnaWave API не принимает null для externalSquadUuid (A039)
                        if ext_squad_uuid is not None:
                            update_kwargs['external_squad_uuid'] = ext_squad_uuid

                        updated_user = await update_panel_user_grace_safe(
                            api,
                            sub.id,
                            **update_kwargs,
                        )

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


async def reset_subscription_with_panel(db, user: User, subscription: Subscription) -> dict:
    """Обнулить подписку «как будто не оформляли» и снять доступ в панели RemnaWave,
    НЕ удаляя пользователя из БД (тикеты и аккаунт остаются).

    Панельного пользователя ОТКЛЮЧАЕМ (disable), а не удаляем — обратимо. Дальше юзер
    может купить тариф с нуля. Возвращает ``{'panel_disabled': bool, 'panel_user_id': int|None}``.
    """
    from app.database.crud.subscription import reset_subscription
    from app.services.payment.lava import cancel_lava_recurring_for_subscription_safe

    # Подписка обнуляется «как будто не оформляли» — СБП-автопродление Platega
    # обязано умереть вместе с ней, иначе следующий push-коллбек продлит и
    # заново включит только что обнулённую подписку (и банк продолжит списывать).
    from app.services.payment.platega import cancel_platega_recurring_for_subscription_safe

    await cancel_platega_recurring_for_subscription_safe(db, subscription.id)

    await cancel_lava_recurring_for_subscription_safe(db, subscription.id)
    # В мультитарифном режиме у каждой подписки свой панельный id — НЕ откатываемся
    # на user.remnawave_id (это легаси single-tariff id, иначе можно отключить
    # не того панельного пользователя). В single-tariff fallback на user корректен.
    if settings.is_multi_tariff_enabled():
        panel_user_id = getattr(subscription, 'remnawave_id', None)
    else:
        panel_user_id = getattr(subscription, 'remnawave_id', None) or getattr(user, 'remnawave_id', None)

    panel_disabled = False
    if panel_user_id:
        try:
            panel_disabled = await SubscriptionService().disable_remnawave_user(panel_user_id)
        except Exception as e:
            logger.warning('Не удалось отключить пользователя в RemnaWave при обнулении подписки', error=e)
    else:
        logger.warning(
            'Обнуление подписки: панельный id не найден, отключение в панели пропущено',
            subscription_id=getattr(subscription, 'id', None),
        )

    await reset_subscription(db, subscription)
    return {'panel_disabled': panel_disabled, 'panel_user_id': panel_user_id}
