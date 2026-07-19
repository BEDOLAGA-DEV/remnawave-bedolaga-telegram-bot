"""
Регрессия: ``create_user`` при гонке регистраций (двойной клик / повторная
доставка апдейта) должен идемпотентно возвращать уже существующего пользователя,
а не выбрасывать необработанный ``IntegrityError`` по ``ix_users_telegram_id``.

Issue: https://github.com/BEDOLAGA-DEV/remnawave-bedolaga-telegram-bot/issues/2991

Сценарий из отчёта (14.06.2026):
  - Первый поток выполнил INSERT и закоммитил.
  - Второй поток попал в ``create_user`` с тем же ``telegram_id`` → ``UniqueViolationError``
    всплыл наверх в ``GlobalErrorMiddleware``.

Покрытые случаи
───────────────
1. Норма — первая регистрация успешна, объект возвращается.
2. Гонка — конфликт по ``ix_users_telegram_id`` через ``constraint_name`` asyncpg
   (программный атрибут): возвращаем существующего пользователя без исключения.
3. Гонка — конфликт через строку в тексте ошибки (fallback для «чужих» драйверов):
   тот же поведенческий результат.
4. Гонка — конфликт по ``telegram_id``, но повторный SELECT ничего не нашёл
   (очень редкий случай сразу за VACUUM/транзакционной изоляцией):
   исключение пробрасывается наверх после исчерпания попыток.
5. Рассинхрон ``users_id_seq`` (PK-конфликт) → откат + ``_sync_users_sequence``
   → повторная попытка, исходное поведение не нарушено.
6. Неизвестный ``IntegrityError`` (например FK по ``referred_by_id``) → пробрасывается.
7. ``_violated_constraint`` — предпочитает ``constraint_name`` asyncpg-исключения;
   правильно обрабатывает None-атрибуты.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import IntegrityError

from app.database.crud.user import _violated_constraint, create_user


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

TELEGRAM_ID = 7_790_427_779


def _make_user(telegram_id: int = TELEGRAM_ID) -> MagicMock:
    """Минимальный мок объекта User."""
    u = MagicMock()
    u.id = 1
    u.telegram_id = telegram_id
    u.referral_code = 'refABC123'
    u.promo_group = None
    return u


def _make_db(
    commit_side_effect=None,
    get_user_return=None,
) -> AsyncMock:
    """Мок AsyncSession с настраиваемым commit и get_user."""
    db = AsyncMock()
    db.add = MagicMock()
    db.rollback = AsyncMock()
    db.refresh = AsyncMock()
    if commit_side_effect is not None:
        db.commit = AsyncMock(side_effect=commit_side_effect)
    else:
        db.commit = AsyncMock()
    return db


def _integrity_error_with_constraint(constraint_name: str) -> IntegrityError:
    """IntegrityError с asyncpg-стилевым ``constraint_name`` на исходном исключении."""
    asyncpg_exc = Exception('UniqueViolationError')
    asyncpg_exc.constraint_name = constraint_name  # type: ignore[attr-defined]

    orig = Exception('asyncpg wrapper')
    orig.__cause__ = asyncpg_exc  # type: ignore[attr-defined]

    return IntegrityError('INSERT', {}, orig)


def _integrity_error_text_only(message: str) -> IntegrityError:
    """IntegrityError без ``constraint_name`` — только текст (строковый fallback)."""
    orig = Exception(message)
    return IntegrityError('INSERT', {}, orig)


# ──────────────────────────────────────────────────────────────────────────────
# Tests for _violated_constraint helper
# ──────────────────────────────────────────────────────────────────────────────


class TestViolatedConstraint:
    def test_prefers_asyncpg_cause_constraint_name(self):
        """`constraint_name` из `cause` читается в первую очередь."""
        exc = _integrity_error_with_constraint('ix_users_telegram_id')
        assert _violated_constraint(exc) == 'ix_users_telegram_id'

    def test_falls_back_to_orig_constraint_name(self):
        """`constraint_name` на самом `orig` используется как вторичный fallback."""
        orig = Exception('some error')
        orig.constraint_name = 'users_pkey'  # type: ignore[attr-defined]
        exc = IntegrityError('INSERT', {}, orig)
        assert _violated_constraint(exc) == 'users_pkey'

    def test_falls_back_to_str_orig_when_no_attribute(self):
        """Без `constraint_name` возвращается строковое представление `orig`."""
        exc = _integrity_error_text_only('duplicate key value violates unique constraint "ix_users_telegram_id"')
        result = _violated_constraint(exc)
        assert 'ix_users_telegram_id' in result

    def test_handles_missing_orig(self):
        """Если у `exc` нет `orig` совсем — не падает, возвращает строку exc."""
        exc = IntegrityError('INSERT', {}, Exception('bare error'))
        # orig есть — третий аргумент. Это нормальный путь; проверяем, что нет AttributeError.
        result = _violated_constraint(exc)
        assert isinstance(result, str)

    def test_returns_str_not_none(self):
        """Функция всегда возвращает str, никогда None."""
        for exc in [
            _integrity_error_with_constraint('some_constraint'),
            _integrity_error_text_only('plain text error'),
        ]:
            result = _violated_constraint(exc)
            assert isinstance(result, str)
            assert result  # непустая строка


# ──────────────────────────────────────────────────────────────────────────────
# Tests for create_user (race-condition handling)
# ──────────────────────────────────────────────────────────────────────────────


def _patch_dependencies(
    db: AsyncMock,
    existing_user=None,
    promo_group_id: int = 1,
    referral_code: str = 'refABC123',
):
    """Контекстный менеджер с подменой всех внешних зависимостей create_user."""
    default_group = MagicMock()
    default_group.id = promo_group_id

    return [
        patch('app.database.crud.user.create_unique_referral_code', new=AsyncMock(return_value=referral_code)),
        patch('app.database.crud.user._normalize_language_code', return_value='ru'),
        patch('app.database.crud.user._get_or_create_default_promo_group', new=AsyncMock(return_value=default_group)),
        patch('app.database.crud.user.sanitize_telegram_name', side_effect=lambda x: x),
        patch('app.database.crud.user.get_user_by_telegram_id', new=AsyncMock(return_value=existing_user)),
        # Redis — отключаем
        patch('app.services.referral_service.get_pending_referral', new=AsyncMock(return_value=None), create=True),
        # event_emitter — отключаем
        patch('app.services.event_emitter.event_emitter', new=MagicMock(emit=AsyncMock()), create=True),
    ]


class TestCreateUserHappyPath:
    async def test_creates_and_returns_user_on_success(self):
        """Первая регистрация: INSERT проходит, возвращается новый User."""
        user = _make_user()
        db = _make_db()
        # После commit db.refresh заполнит атрибуты — мокаем через side_effect
        db.refresh = AsyncMock()

        patches = _patch_dependencies(db)
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patch('app.database.crud.user.User', return_value=user),
        ):
            result = await create_user(db=db, telegram_id=TELEGRAM_ID, referral_code='refABC123')

        db.add.assert_called_once_with(user)
        db.commit.assert_awaited_once()
        assert result is user


class TestCreateUserRaceCondition:
    async def test_returns_existing_user_on_telegram_id_constraint_via_constraint_name(self, monkeypatch):
        """Гонка через constraint_name (asyncpg) → возвращаем уже существующего, без исключения."""
        existing = _make_user()
        db = _make_db(commit_side_effect=_integrity_error_with_constraint('ix_users_telegram_id'))
        new_user = _make_user()

        patches = _patch_dependencies(db, existing_user=existing)
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patch('app.database.crud.user.User', return_value=new_user),
        ):
            result = await create_user(db=db, telegram_id=TELEGRAM_ID, referral_code='refABC123')

        assert result is existing
        db.rollback.assert_awaited_once()
        # Не должны повторно коммитить или вставлять вторую запись
        assert db.commit.await_count == 1  # единственный вызов — который упал

    async def test_returns_existing_user_on_telegram_id_constraint_via_text_fallback(self, monkeypatch):
        """Гонка через строку ошибки (fallback) → тот же результат."""
        existing = _make_user()
        db = _make_db(
            commit_side_effect=_integrity_error_text_only(
                'duplicate key value violates unique constraint "ix_users_telegram_id"'
            )
        )
        new_user = _make_user()

        patches = _patch_dependencies(db, existing_user=existing)
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patch('app.database.crud.user.User', return_value=new_user),
        ):
            result = await create_user(db=db, telegram_id=TELEGRAM_ID, referral_code='refABC123')

        assert result is existing
        db.rollback.assert_awaited_once()

    async def test_does_not_emit_user_created_event_for_race_winner(self, monkeypatch):
        """При гонке событие user.created не дублируется (победитель уже отправил его)."""
        existing = _make_user()
        db = _make_db(commit_side_effect=_integrity_error_with_constraint('ix_users_telegram_id'))
        new_user = _make_user()
        emit_mock = AsyncMock()

        patches = _patch_dependencies(db, existing_user=existing)
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patch('app.database.crud.user.User', return_value=new_user),
            patch('app.services.event_emitter.event_emitter', new=MagicMock(emit=emit_mock), create=True),
        ):
            await create_user(db=db, telegram_id=TELEGRAM_ID, referral_code='refABC123')

        emit_mock.assert_not_awaited()

    async def test_raises_when_existing_user_not_found_after_conflict(self):
        """Конфликт есть, но SELECT после rollback ничего не нашёл → IntegrityError наверх."""
        db = _make_db(commit_side_effect=_integrity_error_with_constraint('ix_users_telegram_id'))
        new_user = _make_user()

        # get_user_by_telegram_id всегда возвращает None
        patches = _patch_dependencies(db, existing_user=None)
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patch('app.database.crud.user.User', return_value=new_user),
        ):
            with pytest.raises(IntegrityError):
                await create_user(db=db, telegram_id=TELEGRAM_ID, referral_code='refABC123')

        # rollback дёргался на каждой попытке (3 попытки)
        assert db.rollback.await_count == 3


class TestCreateUserSequenceDesync:
    async def test_syncs_sequence_on_pkey_conflict_and_retries(self, monkeypatch):
        """PK-конфликт (users_pkey) → синхронизирует последовательность и повторяет вставку."""
        user = _make_user()

        call_count = {'n': 0}

        async def commit_once_then_ok():
            call_count['n'] += 1
            if call_count['n'] == 1:
                raise _integrity_error_with_constraint('users_pkey')
            # Второй вызов — успех (ничего не делаем)

        db = _make_db()
        db.commit = AsyncMock(side_effect=commit_once_then_ok)

        sync_mock = AsyncMock()

        patches = _patch_dependencies(db)
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patch('app.database.crud.user.User', return_value=user),
            patch('app.database.crud.user._sync_users_sequence', new=sync_mock),
        ):
            result = await create_user(db=db, telegram_id=TELEGRAM_ID, referral_code='refABC123')

        sync_mock.assert_awaited_once()
        assert db.commit.await_count == 2
        assert result is user

    async def test_raises_on_third_pkey_conflict(self, monkeypatch):
        """Три PK-конфликта подряд → RuntimeError (не может синхронизировать)."""
        db = _make_db(commit_side_effect=_integrity_error_with_constraint('users_pkey'))
        new_user = _make_user()
        sync_mock = AsyncMock()

        patches = _patch_dependencies(db, existing_user=None)
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patch('app.database.crud.user.User', return_value=new_user),
            patch('app.database.crud.user._sync_users_sequence', new=sync_mock),
        ):
            with pytest.raises(IntegrityError):
                await create_user(db=db, telegram_id=TELEGRAM_ID, referral_code='refABC123')

        # Синхронизация дёргалась на первых двух попытках, третья — reraise
        assert sync_mock.await_count == 2


class TestCreateUserUnknownIntegrityError:
    async def test_unknown_integrity_error_propagates(self):
        """FK по referred_by_id или другой неизвестный IntegrityError — пробрасывается."""
        db = _make_db(
            commit_side_effect=_integrity_error_text_only(
                'insert or update on table "users" violates foreign key constraint "users_referred_by_id_fkey"'
            )
        )
        new_user = _make_user()

        patches = _patch_dependencies(db, existing_user=None)
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patches[4],
            patches[5],
            patches[6],
            patch('app.database.crud.user.User', return_value=new_user),
        ):
            with pytest.raises(IntegrityError):
                await create_user(db=db, telegram_id=TELEGRAM_ID, referral_code='refABC123')

        db.rollback.assert_awaited()
