"""Matching rules for the Remnawave 3.0.0 identity backfill.

A wrong match here silently points a subscription at another person's VPN
account, so every rule that *refuses* to match matters as much as the ones that
succeed.
"""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.services.remnawave_identity_backfill import (
    BackfillReport,
    UnresolvedRow,
    _match_subscription,
    _PanelIndex,
    backfill_remnawave_ids,
)


def _patch_roster(monkeypatch, panel_users):
    """Replace the panel sweep and the configuration check with local data."""
    from app.services.remnawave_service import RemnaWaveService

    target = 'app.services.remnawave_identity_backfill'

    async def fake_roster(_service):
        return panel_users

    # Наследуемся от НАСТОЯЩЕГО класса: самостоятельная заглушка однажды уже
    # «реализовала» метод, которого у RemnaWaveService нет, и спрятала падение
    # CLI по AttributeError. Здесь любой такой вызов упадёт в тестах.
    class _Svc(RemnaWaveService):
        # Наследование намеренное: любой вызов метода, которого у настоящего
        # класса нет, должен падать здесь, а не в проде. `is_configured`
        # переопределён атрибутом класса и перекрывает property родителя.
        is_configured = True
        configuration_error = None

        def __init__(self):
            # Родительский __init__ только читает настройки и складывает
            # параметры — сеть он не трогает, поэтому вызвать его безопасно.
            super().__init__()

    monkeypatch.setattr(f'{target}._load_panel_roster', fake_roster)
    monkeypatch.setattr(f'{target}.RemnaWaveService', _Svc)


def panel_user(user_id, *, short_uuid=None, username=None, telegram_id=None, email=None):
    return SimpleNamespace(
        id=user_id,
        short_uuid=short_uuid,
        username=username,
        telegram_id=telegram_id,
        email=email,
    )


def subscription(sub_id=1, *, user_id=1, short_uuid=None, uuid='legacy-uuid', short_id=None):
    return SimpleNamespace(
        id=sub_id,
        user_id=user_id,
        remnawave_short_uuid=short_uuid,
        remnawave_uuid=uuid,
        remnawave_short_id=short_id,
    )


def bot_user(user_id=1, *, telegram_id=None, email=None, full_name='Test', username=None):
    return SimpleNamespace(
        id=user_id,
        telegram_id=telegram_id,
        email=email,
        full_name=full_name,
        username=username,
    )


def test_short_uuid_is_the_exact_key():
    index = _PanelIndex([panel_user(42, short_uuid='abc123', telegram_id=555)])
    matched, strategy = _match_subscription(subscription(short_uuid='abc123'), bot_user(telegram_id=555), index, {})
    assert strategy == 'short_uuid'
    assert matched.id == 42


def test_stored_short_uuid_unknown_to_panel_does_not_fall_back():
    """The panel user was deleted.

    Falling through to telegram_id would re-link the subscription to a
    *different* panel account of the same person, quietly handing them someone
    else's traffic counters and squads.
    """
    index = _PanelIndex([panel_user(7, short_uuid='other', telegram_id=555)])
    matched, strategy = _match_subscription(subscription(short_uuid='gone'), bot_user(telegram_id=555), index, {})
    assert matched is None
    assert strategy == 'short_uuid_not_found_in_panel'


def test_unique_telegram_id_resolves_when_no_short_uuid():
    index = _PanelIndex([panel_user(9, short_uuid='s9', telegram_id=555)])
    matched, strategy = _match_subscription(subscription(), bot_user(telegram_id=555), index, {})
    assert strategy == 'telegram_id_unique'
    assert matched.id == 9


def test_multi_tariff_ambiguity_is_refused_without_a_discriminator():
    index = _PanelIndex(
        [
            panel_user(1, short_uuid='s1', username='user_555_aaa', telegram_id=555),
            panel_user(2, short_uuid='s2', username='user_555_bbb', telegram_id=555),
        ]
    )
    matched, strategy = _match_subscription(subscription(), bot_user(telegram_id=555), index, {})
    assert matched is None
    assert strategy == 'ambiguous_telegram_id'


def test_multi_tariff_short_id_suffix_disambiguates():
    index = _PanelIndex(
        [
            panel_user(1, username='user_555_aaa', telegram_id=555),
            panel_user(2, username='user_555_bbb', telegram_id=555),
        ]
    )
    matched, strategy = _match_subscription(subscription(short_id='bbb'), bot_user(telegram_id=555), index, {})
    assert strategy == 'telegram_id_plus_short_id'
    assert matched.id == 2


def test_email_only_user_resolves_by_email():
    index = _PanelIndex([panel_user(11, email='A@Example.COM')])
    matched, strategy = _match_subscription(
        subscription(), bot_user(telegram_id=None, email='a@example.com'), index, {}
    )
    assert strategy == 'email_unique'
    assert matched.id == 11


def test_duplicate_email_is_refused():
    index = _PanelIndex([panel_user(11, email='a@e.com'), panel_user(12, email='a@e.com')])
    matched, strategy = _match_subscription(subscription(), bot_user(telegram_id=None, email='a@e.com'), index, {})
    assert matched is None
    assert strategy == 'ambiguous_email'


def test_no_surviving_identifier_is_reported_not_guessed():
    index = _PanelIndex([panel_user(1, short_uuid='s1', telegram_id=999)])
    matched, strategy = _match_subscription(subscription(), bot_user(telegram_id=None, email=None), index, {})
    assert matched is None
    assert strategy == 'no_surviving_identifier'


def test_already_claimed_panel_user_is_not_reused_via_username():
    """Two subscriptions must never converge on one panel account."""
    index = _PanelIndex([panel_user(5, username='user_555', telegram_id=None)])
    matched, _ = _match_subscription(subscription(), bot_user(telegram_id=None, email=None), index, claimed={5: '1'})
    assert matched is None


@pytest.mark.parametrize('telegram_id', [555, None])
def test_index_tolerates_missing_fields(telegram_id):
    """Panel rows legitimately have null telegramId/email; indexing must not crash."""
    index = _PanelIndex([panel_user(1, short_uuid=None, username=None, telegram_id=telegram_id, email=None)])
    assert index.by_short_uuid == {}
    assert index.by_username == {}


def test_exact_only_defers_rows_without_a_short_uuid():
    """Pass 1 must not consume weak matches.

    Otherwise a low-numbered dead row can claim, via telegram_id, the panel
    account that a later row identifies exactly by shortUuid — linking the dead
    subscription to a live panel user and leaving the live one NULL.
    """
    index = _PanelIndex([panel_user(77, short_uuid='abc123', telegram_id=555)])
    matched, strategy = _match_subscription(
        subscription(sub_id=10), bot_user(telegram_id=555), index, {}, exact_only=True
    )
    assert matched is None
    assert strategy == 'no_exact_key'

    # ...and the exact row still wins it in the same pass.
    matched, strategy = _match_subscription(
        subscription(sub_id=20, short_uuid='abc123'), bot_user(telegram_id=555), index, {}, exact_only=True
    )
    assert strategy == 'short_uuid'
    assert matched.id == 77


def test_exact_only_still_reports_a_dead_short_uuid():
    """A stored shortUuid the panel does not know is a verdict, not a deferral."""
    index = _PanelIndex([panel_user(7, short_uuid='other')])
    matched, strategy = _match_subscription(
        subscription(short_uuid='gone'), bot_user(telegram_id=555), index, {}, exact_only=True
    )
    assert matched is None
    assert strategy == 'short_uuid_not_found_in_panel'


# ── Integration: the two-pass loop, claim priming and conflict rollback ──────
#
# These exercise `backfill_remnawave_ids` against a real (in-memory) database,
# because the defects they guard against live in the CALLER, not in
# `_match_subscription`: pass ordering, priming `claimed` from already-persisted
# rows, and refusing to commit a run that produced conflicts.

from app.database.models import GraceAccessSessionModel, Subscription as SubModel, User as UserModel
from tests.fixtures.sqlite_memory import memory_session


async def _seed(session, *, subs, owners=None):
    """`subs` = [(sub_id, short_uuid, legacy_uuid)]; `owners` = {sub_id: user_id}."""
    owners = owners or {}
    for user_id in sorted({*owners.values(), 1}):
        session.add(UserModel(id=user_id, telegram_id=550 + user_id, remnawave_uuid=f'legacy-{user_id}'))
    for sub_id, short_uuid, uuid in subs:
        session.add(
            SubModel(
                id=sub_id,
                user_id=owners.get(sub_id, 1),
                status='active',
                end_date=datetime(2030, 1, 1, tzinfo=UTC),
                remnawave_short_uuid=short_uuid,
                remnawave_uuid=uuid,
                remnawave_short_id=f'sid{sub_id}',
            )
        )
    await session.commit()


@pytest.mark.asyncio
async def test_exact_match_wins_over_a_lower_numbered_weak_match(monkeypatch):
    """The regression this was written for.

    Sub 10 lost its shortUuid (crud/subscription.py:776 clears it) and its panel
    account is gone; sub 20 is the live row. Processing in id order used to let
    sub 10 claim panel 77 by telegram_id, stranding the live row on NULL.
    """
    async with memory_session(
        monkeypatch, [UserModel.__table__, SubModel.__table__, GraceAccessSessionModel.__table__]
    ) as db:
        await _seed(db, subs=[(10, None, 'dead-uuid'), (20, 'abc123', 'live-uuid')])
        _patch_roster(monkeypatch, [panel_user(77, short_uuid='abc123', username='u', telegram_id=555)])

        report = await backfill_remnawave_ids(db, dry_run=False)

        sub10 = await db.get(SubModel, 10)
        sub20 = await db.get(SubModel, 20)
        assert sub20.remnawave_id == 77, 'the exactly-identified row must win the panel account'
        assert sub10.remnawave_id is None, 'the dead row must be left unresolved, not linked'
        assert report.conflicts == []


@pytest.mark.asyncio
async def test_rerun_does_not_reassign_an_already_persisted_panel_id(monkeypatch):
    """`claimed` must be primed from rows a previous run already wrote."""
    async with memory_session(
        monkeypatch, [UserModel.__table__, SubModel.__table__, GraceAccessSessionModel.__table__]
    ) as db:
        await _seed(db, subs=[(10, None, 'dead-uuid'), (20, 'abc123', 'live-uuid')])
        _patch_roster(monkeypatch, [panel_user(77, short_uuid='abc123', username='u', telegram_id=555)])
        await backfill_remnawave_ids(db, dry_run=False)  # pass 1 gives 77 to sub 20

        report = await backfill_remnawave_ids(db, dry_run=False)  # pass 2

        sub10 = await db.get(SubModel, 10)
        assert sub10.remnawave_id is None, 'a re-run must not hand out an owned panel id'
        assert report.conflicts == []


@pytest.mark.asyncio
async def test_dry_run_writes_nothing(monkeypatch):
    async with memory_session(
        monkeypatch, [UserModel.__table__, SubModel.__table__, GraceAccessSessionModel.__table__]
    ) as db:
        await _seed(db, subs=[(20, 'abc123', 'live-uuid')])
        _patch_roster(monkeypatch, [panel_user(77, short_uuid='abc123', username='u', telegram_id=555)])

        report = await backfill_remnawave_ids(db, dry_run=True)

        assert report.subscriptions_resolved == 1
        db.expunge_all()
        assert (await db.get(SubModel, 20)).remnawave_id is None


@pytest.mark.asyncio
async def test_conflict_between_different_users_rolls_back(monkeypatch):
    """Один панельный аккаунт, на который претендуют РАЗНЫЕ пользователи.

    Здесь минимум одна запись неверна и указала бы подписку на чужой VPN, —
    коммитить «кто первый успел» нельзя, откатываем весь прогон.
    """
    async with memory_session(
        monkeypatch, [UserModel.__table__, SubModel.__table__, GraceAccessSessionModel.__table__]
    ) as db:
        await _seed(db, subs=[(10, 'dup', 'uuid-a'), (20, 'dup', 'uuid-b')], owners={10: 1, 20: 2})
        _patch_roster(monkeypatch, [panel_user(77, short_uuid='dup', username='u', telegram_id=555)])

        report = await backfill_remnawave_ids(db, dry_run=False)

        assert report.conflicts, 'разные пользователи на один аккаунт — это конфликт'
        db.expunge_all()
        assert (await db.get(SubModel, 10)).remnawave_id is None
        assert (await db.get(SubModel, 20)).remnawave_id is None


@pytest.mark.asyncio
async def test_sibling_rows_of_one_user_are_not_a_conflict(monkeypatch):
    """Штатное состояние single-tariff, а не ошибка.

    Истёк триал — вставляется НОВАЯ строка, старая сохраняет тот же
    `remnawave_short_uuid`, потому что панельный аккаунт у пользователя один.
    Считать это конфликтом означало откатывать весь бэкфил на любой
    инсталляции, которая когда-либо работала в single-tariff, то есть сделать
    обязательный шаг миграции невыполнимым.
    """
    async with memory_session(
        monkeypatch, [UserModel.__table__, SubModel.__table__, GraceAccessSessionModel.__table__]
    ) as db:
        await _seed(db, subs=[(10, 'dup', 'uuid-a'), (20, 'dup', 'uuid-b')])
        _patch_roster(monkeypatch, [panel_user(77, short_uuid='dup', username='u', telegram_id=555)])

        report = await backfill_remnawave_ids(db, dry_run=False)

        assert report.conflicts == [], 'подписки одного пользователя — не конфликт'
        db.expunge_all()
        # Колонка частично уникальна, поэтому id получает ровно одна строка.
        ids = {(await db.get(SubModel, 10)).remnawave_id, (await db.get(SubModel, 20)).remnawave_id}
        assert ids == {77, None}
        # Идентичность пользователя сохранена — в single-tariff она каноническая.
        assert (await db.get(UserModel, 1)).remnawave_id == 77


@pytest.mark.asyncio
async def test_multi_tariff_never_fills_the_user_level_column(monkeypatch):
    """В multi-tariff `users.remnawave_id` обязан остаться пустым.

    Идентичность там живёт на подписке, а колонка на User исторически пуста —
    именно поэтому около десятка фолбэков `sub.remnawave_id or
    user.remnawave_id` безопасно пропускали панель. Заполнив её, бэкфил оживил
    бы их, и подписка с неразрезолвленным id начала бы адресовать панельный
    аккаунт СОСЕДНЕГО тарифа.

    Гейта по `users.remnawave_uuid` мало: инсталляция, начинавшая в
    single-tariff и переключённая на multi, несёт эту колонку заполненной.
    """
    from app.config import Settings

    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: True)
    async with memory_session(
        monkeypatch, [UserModel.__table__, SubModel.__table__, GraceAccessSessionModel.__table__]
    ) as db:
        await _seed(db, subs=[(20, 'abc123', 'legacy-uuid')])
        _patch_roster(monkeypatch, [panel_user(77, short_uuid='abc123', username='u', telegram_id=551)])

        await backfill_remnawave_ids(db, dry_run=False)

        db.expunge_all()
        assert (await db.get(SubModel, 20)).remnawave_id == 77, 'подписка обязана быть привязана'
        assert (await db.get(UserModel, 1)).remnawave_id is None, 'колонка на User должна остаться пустой'


@pytest.mark.asyncio
async def test_rerun_does_not_invent_a_conflict_for_a_sibling(monkeypatch):
    """Повторный прогон — штатный сценарий, а не исключение.

    Первый apply всегда завершается «частично» (у сиблинга нет id), и оператор
    по инструкции перезапускает. `claimed` приминговывался уже записанными id,
    а владелец — нет, поэтому сиблинг выглядел как претензия ДРУГОГО
    пользователя, объявлялся конфликтом и откатывал весь прогон.
    """
    tables = [UserModel.__table__, SubModel.__table__, GraceAccessSessionModel.__table__]
    async with memory_session(monkeypatch, tables) as db:
        await _seed(db, subs=[(10, 'dup', 'uuid-a'), (20, 'dup', 'uuid-b')])
        _patch_roster(monkeypatch, [panel_user(77, short_uuid='dup', username='u', telegram_id=551)])

        first = await backfill_remnawave_ids(db, dry_run=False)
        assert first.conflicts == []

        second = await backfill_remnawave_ids(db, dry_run=False)

        assert second.conflicts == [], 'сиблинг уже записанной строки — не конфликт'
        db.expunge_all()
        ids = {(await db.get(SubModel, 10)).remnawave_id, (await db.get(SubModel, 20)).remnawave_id}
        assert ids == {77, None}, 'повторный прогон не должен ничего терять'


@pytest.mark.asyncio
async def test_live_subscription_wins_the_panel_id_not_the_expired_one(monkeypatch):
    """id достаётся живой строке, а не самой старой.

    По `order_by(id)` его получал конвертированный триал, и тогда grace-сессия
    текущей подписки оставалась без идентичности (для ридера грейса это жёсткий
    data fault), а вебхук резолвил события на мёртвую строку.
    """
    tables = [UserModel.__table__, SubModel.__table__, GraceAccessSessionModel.__table__]
    async with memory_session(monkeypatch, tables) as db:
        # Живая строка намеренно с МЕНЬШИМ id: иначе тест проходил бы на одном
        # только тайбрейке `id DESC`, не проверяя ключ сортировки по статусу.
        await _seed(db, subs=[(10, 'dup', 'uuid-a'), (20, 'dup', 'uuid-b')])
        (await db.get(SubModel, 10)).status = 'active'
        (await db.get(SubModel, 20)).status = 'expired'
        await db.commit()
        _patch_roster(monkeypatch, [panel_user(77, short_uuid='dup', username='u', telegram_id=551)])

        await backfill_remnawave_ids(db, dry_run=False)

        db.expunge_all()
        assert (await db.get(SubModel, 10)).remnawave_id == 77, 'живая подписка обязана получить id'
        assert (await db.get(SubModel, 20)).remnawave_id is None


@pytest.mark.asyncio
async def test_multi_tariff_shared_account_is_skipped_locally_not_aborted(monkeypatch):
    """Общий аккаунт в multi-tariff ненормален, но не должен ронять весь прогон.

    Инсталляция, переключённая из single-tariff в multi, штатно несёт такие
    пары. Откат из-за одной пары заблокировал бы миграцию целиком, а строка и
    так остаётся без id — записывать нечего.
    """
    from app.config import Settings

    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: True)
    tables = [UserModel.__table__, SubModel.__table__, GraceAccessSessionModel.__table__]
    async with memory_session(monkeypatch, tables) as db:
        await _seed(db, subs=[(10, 'dup', 'uuid-a'), (20, 'dup', 'uuid-b')])
        _patch_roster(monkeypatch, [panel_user(77, short_uuid='dup', username='u', telegram_id=551)])

        report = await backfill_remnawave_ids(db, dry_run=False)

        assert report.conflicts == [], 'одна пара не должна ронять весь прогон'
        db.expunge_all()
        ids = {(await db.get(SubModel, 10)).remnawave_id, (await db.get(SubModel, 20)).remnawave_id}
        assert ids == {77, None}, 'id достаётся одной строке, вторая остаётся в отчёте'


@pytest.mark.asyncio
async def test_id_moves_to_the_live_row_when_only_the_dead_one_kept_the_short_uuid(monkeypatch):
    """Самая частая форма в single-tariff, и сортировкой её не решить.

    При замене подписки новая строка вставляется БЕЗ `remnawave_short_uuid`, а
    конвертированный триал свой сохраняет. Первый проход берёт только точные
    совпадения, поэтому id уходит мёртвой строке независимо от сортировки —
    а grace-сессия текущей подписки остаётся без идентичности и становится
    нечитаемой.
    """
    tables = [UserModel.__table__, SubModel.__table__, GraceAccessSessionModel.__table__]
    async with memory_session(monkeypatch, tables) as db:
        await _seed(db, subs=[(10, 'dup', 'uuid-a'), (20, None, 'uuid-b')])
        (await db.get(SubModel, 10)).status = 'expired'
        (await db.get(SubModel, 20)).status = 'active'
        await db.commit()
        _patch_roster(monkeypatch, [panel_user(77, short_uuid='dup', username='u', telegram_id=551)])

        await backfill_remnawave_ids(db, dry_run=False)

        db.expunge_all()
        assert (await db.get(SubModel, 20)).remnawave_id == 77, 'id обязан переехать на живую строку'
        assert (await db.get(SubModel, 10)).remnawave_id is None


@pytest.mark.asyncio
async def test_between_two_live_rows_the_later_end_date_wins(monkeypatch):
    """Пинит саму сортировку, а не перенос id.

    Когда обе строки живые, `_prefer_alive_sibling` неприменим (переносить
    некуда), и решает только ORDER BY. Без этого случая ключ сортировки
    проходил бы на одном тайбрейке по id.
    """
    tables = [UserModel.__table__, SubModel.__table__, GraceAccessSessionModel.__table__]
    async with memory_session(monkeypatch, tables) as db:
        await _seed(db, subs=[(10, 'dup', 'uuid-a'), (20, 'dup', 'uuid-b')])
        # Актуальна строка 10 (позже заканчивается), хотя её id МЕНЬШЕ.
        sub10, sub20 = await db.get(SubModel, 10), await db.get(SubModel, 20)
        sub10.status, sub20.status = 'active', 'active'
        sub10.end_date = datetime(2031, 1, 1, tzinfo=UTC)
        sub20.end_date = datetime(2027, 1, 1, tzinfo=UTC)
        await db.commit()
        _patch_roster(monkeypatch, [panel_user(77, short_uuid='dup', username='u', telegram_id=551)])

        await backfill_remnawave_ids(db, dry_run=False)

        db.expunge_all()
        assert (await db.get(SubModel, 10)).remnawave_id == 77
        assert (await db.get(SubModel, 20)).remnawave_id is None


@pytest.mark.asyncio
async def test_transfer_does_not_leave_the_target_marked_unresolved(monkeypatch):
    """Отчёт — единственная поверхность принятия решения для оператора.

    Строка-приёмник помечается нерешённой в основном проходе; после переноса
    id она решена, и оставлять её в списке значит отправлять оператора
    разбираться с тем, что уже в порядке.
    """
    tables = [UserModel.__table__, SubModel.__table__, GraceAccessSessionModel.__table__]
    async with memory_session(monkeypatch, tables) as db:
        await _seed(db, subs=[(10, 'dup', 'uuid-a'), (20, None, 'uuid-b')])
        (await db.get(SubModel, 10)).status = 'expired'
        (await db.get(SubModel, 20)).status = 'active'
        await db.commit()
        _patch_roster(monkeypatch, [panel_user(77, short_uuid='dup', username='u', telegram_id=551)])

        report = await backfill_remnawave_ids(db, dry_run=False)

        target_rows = [r for r in report.unresolved if r.kind == 'subscription' and r.row_id == 20]
        assert target_rows == [], 'решённая строка не должна оставаться в списке нерешённых'


@pytest.mark.asyncio
async def test_grace_session_on_the_donor_row_still_gets_an_id(monkeypatch):
    """Перенос id не должен обесточивать grace-сессию строки-донора.

    Ровно тот сценарий, ради которого перенос и существует: истёк триал, на нём
    открыт грейс, покупка вставила новую строку без shortUuid. Перенос забирает
    id у триала — и сессия остаётся с пустой колонкой, то есть нечитаемой
    навсегда: `_model_to_session` бросает исключение, а повторный прогон уже
    не поможет, id занят живой строкой.
    """
    tables = [UserModel.__table__, SubModel.__table__, GraceAccessSessionModel.__table__]
    async with memory_session(monkeypatch, tables) as db:
        await _seed(db, subs=[(10, 'dup', 'uuid-a'), (20, None, 'uuid-b')])
        (await db.get(SubModel, 10)).status = 'expired'
        (await db.get(SubModel, 20)).status = 'active'
        db.add(
            GraceAccessSessionModel(
                id='g-old',
                subscription_id=10,
                remnawave_uuid='uuid-a',
                reason='expired',
                incident_key='inc-1',
                state='active',
                billing_before={},
                panel_before={},
                overlay={},
                started_at=datetime(2026, 1, 1, tzinfo=UTC),
                grace_until=datetime(2026, 2, 1, tzinfo=UTC),
                updated_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )
        await db.commit()
        _patch_roster(monkeypatch, [panel_user(77, short_uuid='dup', username='u', telegram_id=551)])

        await backfill_remnawave_ids(db, dry_run=False)

        db.expunge_all()
        session = await db.get(GraceAccessSessionModel, 'g-old')
        assert session.remnawave_id == 77, 'сессия донора обязана получить идентичность'


def _grace(session_id, subscription_id, remnawave_uuid, *, state='active'):
    return GraceAccessSessionModel(
        id=session_id,
        subscription_id=subscription_id,
        remnawave_uuid=remnawave_uuid,
        reason='expired',
        incident_key=f'inc-{session_id}',
        state=state,
        billing_before={},
        panel_before={},
        overlay={},
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        grace_until=datetime(2026, 2, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_multi_tariff_sibling_session_resolves_by_its_own_uuid(monkeypatch):
    """Мультитариф: сессия на «сиблинге» тоже обязана получить идентичность.

    Две подписки одного пользователя смотрят на один панельный аккаунт. Колонка
    частично уникальна, поэтому id достаётся ровно одной строке, а вторая честно
    отмечается `sibling_shares_panel_account` — и это единственная причина, при
    которой инструкция разрешает продолжать. Но grace-сессия второй строки
    раньше не имела ни одного источника: фолбэк по владельцу в мультитарифе
    выключен намеренно. Пустая колонка делает сессию нечитаемой навсегда.
    Её собственный uuid — точный ключ, и он у неё есть.
    """
    from app.config import Settings

    monkeypatch.setattr(Settings, 'is_multi_tariff_enabled', lambda self: True)
    tables = [UserModel.__table__, SubModel.__table__, GraceAccessSessionModel.__table__]
    async with memory_session(monkeypatch, tables) as db:
        await _seed(db, subs=[(10, 'shared', 'uuid-shared'), (20, None, 'uuid-shared')])
        db.add(_grace('g-sibling', 20, 'uuid-shared'))
        await db.commit()
        _patch_roster(monkeypatch, [panel_user(77, short_uuid='shared', username='u', telegram_id=551)])

        await backfill_remnawave_ids(db, dry_run=False)

        db.expunge_all()
        assert (await db.get(GraceAccessSessionModel, 'g-sibling')).remnawave_id == 77


@pytest.mark.asyncio
async def test_session_of_a_deleted_panel_account_is_not_given_the_live_one(monkeypatch):
    """Сессия удалённого аккаунта не должна получить живой аккаунт владельца.

    Аккаунт пересоздавали: у пользователя новый панельный аккаунт, а сессия
    хранит uuid старого. Приписать ей текущий — значит разрешить грейсу писать
    в ОПЛАЧЕННЫЙ аккаунт: `_activate_pending` зовёт `apply_billing_state`, а он
    для не-LIMITED вызывает update_user без compare-and-set. Пусть лучше строка
    останется неразрешённой — нечитаемая сессия инертна, а чужая запись нет.
    """
    tables = [UserModel.__table__, SubModel.__table__, GraceAccessSessionModel.__table__]
    async with memory_session(monkeypatch, tables) as db:
        await _seed(db, subs=[(10, 'gone', 'uuid-dead'), (20, 'live', 'uuid-live')])
        (await db.get(SubModel, 10)).status = 'expired'
        db.add(_grace('g-dead', 10, 'uuid-of-deleted-account'))
        await db.commit()
        _patch_roster(monkeypatch, [panel_user(200, short_uuid='live', username='u', telegram_id=551)])

        report = await backfill_remnawave_ids(db, dry_run=False)

        db.expunge_all()
        session = await db.get(GraceAccessSessionModel, 'g-dead')
        assert session.remnawave_id is None, 'нельзя приписывать сессии чужой живой аккаунт'
        assert any(r.kind == 'grace_session' and r.row_id == 'g-dead' for r in report.unresolved)


@pytest.mark.asyncio
async def test_single_tariff_session_resolves_by_the_user_uuid_it_actually_stores(monkeypatch):
    """Реальная форма однотарифных данных: в сессии лежит uuid ПОЛЬЗОВАТЕЛЯ.

    `_subscription_to_billing` до 3.0.0 клала в сессию uuid подписки только в
    мультитарифе, а в однотарифном — `user.remnawave_uuid`. Поэтому карты одних
    только подписочных uuid недостаточно: у строки-донора после переноса id
    сессия не нашла бы себя по своему же ключу.
    """
    tables = [UserModel.__table__, SubModel.__table__, GraceAccessSessionModel.__table__]
    async with memory_session(monkeypatch, tables) as db:
        await _seed(db, subs=[(10, 'dup', 'uuid-a'), (20, None, 'uuid-b')])
        (await db.get(SubModel, 10)).status = 'expired'
        (await db.get(SubModel, 20)).status = 'active'
        # `_seed` даёт пользователю 1 именно 'legacy-1' — это и есть тот uuid,
        # который однотарифный грейс записал бы в сессию.
        db.add(_grace('g-user-uuid', 10, 'legacy-1'))
        await db.commit()
        _patch_roster(monkeypatch, [panel_user(77, short_uuid='dup', username='u', telegram_id=551)])

        await backfill_remnawave_ids(db, dry_run=False)

        db.expunge_all()
        assert (await db.get(GraceAccessSessionModel, 'g-user-uuid')).remnawave_id == 77


@pytest.mark.asyncio
async def test_session_resolves_when_its_subscription_was_linked_by_an_earlier_run(monkeypatch):
    """Карта uuid обязана праймиться из уже записанных строк.

    Повторный прогон здесь штатный: первый почти всегда завершается «частично»,
    а бот к этому моменту мог уже проставить идентичность сам. Такие строки
    отфильтрованы условием IS NULL и в прогоне не участвуют — если не запраймить
    карту, их uuid в неё не попадёт, и grace-сессия останется без источника
    навсегда: следующий прогон её тоже не починит.
    """
    tables = [UserModel.__table__, SubModel.__table__, GraceAccessSessionModel.__table__]
    async with memory_session(monkeypatch, tables) as db:
        await _seed(db, subs=[(10, 'dup', 'uuid-a'), (20, None, 'uuid-b')])
        (await db.get(SubModel, 10)).status = 'expired'
        # Предыдущий прогон (или уже поднятый бот) связал живую строку и юзера.
        (await db.get(SubModel, 20)).remnawave_id = 77
        (await db.get(UserModel, 1)).remnawave_id = 77
        db.add(_grace('g-earlier', 10, 'legacy-1'))
        await db.commit()
        _patch_roster(monkeypatch, [panel_user(77, short_uuid='dup', username='u', telegram_id=551)])

        await backfill_remnawave_ids(db, dry_run=False)

        db.expunge_all()
        assert (await db.get(GraceAccessSessionModel, 'g-earlier')).remnawave_id == 77


@pytest.mark.asyncio
async def test_panel_id_already_owned_by_another_user_is_reported_not_assigned(monkeypatch):
    """`users.remnawave_id` уникальна глобально — второй претендент идёт в отчёт.

    Без учёта занятых id прогон присваивал дубликат и падал на IntegrityError
    целиком: оператор получал трейсбек вместо списка неразрешённых строк, причём
    и на холостом прогоне тоже.
    """
    tables = [UserModel.__table__, SubModel.__table__, GraceAccessSessionModel.__table__]
    async with memory_session(monkeypatch, tables) as db:
        db.add(UserModel(id=1, telegram_id=551, remnawave_uuid='legacy-1', remnawave_id=77))
        db.add(UserModel(id=2, telegram_id=999, remnawave_uuid='legacy-2'))
        await db.commit()
        # Панельный аккаунт 77 несёт telegramId юзера 2, но уже принадлежит юзеру 1.
        _patch_roster(monkeypatch, [panel_user(77, short_uuid='s77', username='u', telegram_id=999)])

        report = await backfill_remnawave_ids(db, dry_run=False)

        db.expunge_all()
        assert (await db.get(UserModel, 2)).remnawave_id is None
        assert any(r.kind == 'user' and r.row_id == 2 for r in report.unresolved)


@pytest.mark.asyncio
async def test_blocked_strongest_evidence_does_not_fall_through_to_a_weaker_one(monkeypatch):
    """Заблокированная подписочная улика НЕ должна спускаться к телеграм-ветке.

    Если аккаунт, на который указывает собственная подписка пользователя, занят,
    единственный честный исход — строка в отчёте. Спуск к более слабой стратегии
    привязывает ДРУГОЙ панельный аккаунт, противоречащий его же подписке, и
    отчёт при этом называет прогон полным.
    """
    tables = [UserModel.__table__, SubModel.__table__, GraceAccessSessionModel.__table__]
    async with memory_session(monkeypatch, tables) as db:
        # Аккаунт 77 уже закреплён за другим бот-пользователем.
        db.add(UserModel(id=9, telegram_id=999, remnawave_uuid='legacy-9', remnawave_id=77))
        await _seed(db, subs=[(10, 'dup', 'uuid-a')])
        (await db.get(UserModel, 1)).telegram_id = 551
        await db.commit()
        # Подписка юзера 1 указывает на 77 (занят), а по telegram_id он матчится на 88.
        _patch_roster(
            monkeypatch,
            [
                panel_user(77, short_uuid='dup', username='u77', telegram_id=551),
                panel_user(88, short_uuid='other', username='u88', telegram_id=551),
            ],
        )

        report = await backfill_remnawave_ids(db, dry_run=False)

        db.expunge_all()
        user1 = await db.get(UserModel, 1)
        assert user1.remnawave_id != 88, 'нельзя привязывать аккаунт, противоречащий собственной подписке'
        assert user1.remnawave_id is None
        assert not report.complete, 'прогон с заблокированной строкой не может считаться полным'
        assert any(
            r.kind == 'user' and r.row_id == 1 and r.reason == 'panel_account_owned_by_another_user'
            for r in report.unresolved
        ), [r.reason for r in report.unresolved]


@pytest.mark.asyncio
async def test_account_claimed_by_another_users_subscription_is_not_given_away(monkeypatch):
    """Аккаунт, разобранный подпиской ДРУГОГО пользователя, нельзя отдать по telegram_id.

    `_backfill_users` не видел `claimed_owner`, поэтому пользователь без подписки
    мог забрать по слабой улике аккаунт, который в этом же прогоне точно связан
    с подпиской другого человека. Отчёт при этом оставался «полным».
    Порядок важен: юзер 1 обрабатывается раньше, и на тот момент аккаунт ещё не
    записан ни за кем — блокировать может только `claimed_owner`.
    """
    tables = [UserModel.__table__, SubModel.__table__, GraceAccessSessionModel.__table__]
    async with memory_session(monkeypatch, tables) as db:
        await _seed(db, subs=[(10, 'dup', 'uuid-a')], owners={10: 2})
        await db.commit()
        # У панельного аккаунта 77 telegramId юзера 1, но подписка на него — у юзера 2.
        _patch_roster(monkeypatch, [panel_user(77, short_uuid='dup', username='u77', telegram_id=551)])

        report = await backfill_remnawave_ids(db, dry_run=False)

        db.expunge_all()
        assert (await db.get(UserModel, 2)).remnawave_id == 77, 'владелец подписки должен получить аккаунт'
        assert (await db.get(UserModel, 1)).remnawave_id is None, 'чужой аккаунт по telegram_id отдавать нельзя'
        assert any(
            r.kind == 'user' and r.row_id == 1 and r.reason == 'panel_account_owned_by_another_user'
            for r in report.unresolved
        ), [(r.row_id, r.reason) for r in report.unresolved]


@pytest.mark.asyncio
async def test_one_uuid_pointing_at_two_accounts_is_dropped_not_coin_flipped(monkeypatch):
    """Противоречивый uuid нельзя разрешать «как повезёт».

    Строку могли пересоздать, обнулив id и оставив старый uuid. Тогда один uuid
    встречается на двух строках с РАЗНЫМИ панельными id, а запрос идёт без
    ORDER BY — победитель случаен. Ценой ошибки будет grace-сессия, привязанная
    к чужому живому аккаунту, поэтому такой uuid выбрасывается из карты целиком.
    """
    tables = [UserModel.__table__, SubModel.__table__, GraceAccessSessionModel.__table__]
    async with memory_session(monkeypatch, tables) as db:
        await _seed(db, subs=[(10, 'a', 'uuid-clash'), (20, 'b', 'uuid-clash'), (30, None, 'uuid-clash')])
        for sub_id, panel_id in ((10, 101), (20, 202)):
            (await db.get(SubModel, sub_id)).remnawave_id = panel_id
        # Сессия висит на строке 30, у которой своего id нет — единственный
        # оставшийся источник это карта, и она обязана промолчать.
        db.add(_grace('g-clash', 30, 'uuid-clash'))
        await db.commit()
        _patch_roster(monkeypatch, [panel_user(101, short_uuid='a'), panel_user(202, short_uuid='b')])

        report = await backfill_remnawave_ids(db, dry_run=False)

        db.expunge_all()
        session = await db.get(GraceAccessSessionModel, 'g-clash')
        assert session.remnawave_id is None, 'противоречивый uuid не должен резолвиться наугад'
        assert any(r.kind == 'grace_session' and r.row_id == 'g-clash' for r in report.unresolved)


@pytest.mark.asyncio
async def test_uuid_clash_discovered_during_matching_is_also_dropped(monkeypatch):
    """Коллизия, всплывшая в `assign`, а не при прайминге, — тот же случай.

    На ПЕРВОМ прогоне (главном) в БД ещё ничего не записано, поэтому все
    противоречия обнаруживаются именно во время сопоставления. Отложенная
    зачистка карты после прайминга здесь не срабатывала бы вовсе, и uuid остался
    бы указывать на случайный из двух аккаунтов.
    """
    tables = [UserModel.__table__, SubModel.__table__, GraceAccessSessionModel.__table__]
    async with memory_session(monkeypatch, tables) as db:
        # Ничего не проставлено заранее — чистая первая миграция.
        await _seed(db, subs=[(10, 'a', 'uuid-clash'), (20, 'b', 'uuid-clash'), (30, None, 'uuid-clash')])
        db.add(_grace('g-first-run', 30, 'uuid-clash'))
        await db.commit()
        _patch_roster(monkeypatch, [panel_user(101, short_uuid='a'), panel_user(202, short_uuid='b')])

        report = await backfill_remnawave_ids(db, dry_run=False)

        db.expunge_all()
        assert (await db.get(SubModel, 10)).remnawave_id == 101
        assert (await db.get(SubModel, 20)).remnawave_id == 202
        session = await db.get(GraceAccessSessionModel, 'g-first-run')
        assert session.remnawave_id is None, 'противоречивый uuid не должен резолвиться наугад'
        assert any(r.kind == 'grace_session' and r.row_id == 'g-first-run' for r in report.unresolved)


@pytest.mark.asyncio
async def test_user_takes_the_exact_id_from_a_previously_linked_subscription(monkeypatch):
    """Точный id соседней строки бьёт догадку по telegram_id.

    Подписки, связанные предыдущим прогоном (или уже поднятым ботом),
    отфильтрованы условием IS NULL и в выборку не попадают. Пока их не
    учитывали, пользователь уходил в телеграм-ветку и мог получить ДРУГОЙ
    аккаунт, хотя точный ответ лежал строкой рядом.
    """
    tables = [UserModel.__table__, SubModel.__table__, GraceAccessSessionModel.__table__]
    async with memory_session(monkeypatch, tables) as db:
        await _seed(db, subs=[(10, 'linked', 'uuid-a')])
        (await db.get(SubModel, 10)).remnawave_id = 505  # связана раньше
        await db.commit()
        # 505 в панели ЖИВ, а по telegram_id панель отдаёт ДРУГОЙ аккаунт.
        _patch_roster(
            monkeypatch,
            [
                panel_user(505, short_uuid='linked', username='u505'),
                panel_user(909, short_uuid='other', username='u', telegram_id=551),
            ],
        )

        await backfill_remnawave_ids(db, dry_run=False)

        db.expunge_all()
        assert (await db.get(UserModel, 1)).remnawave_id == 505


@pytest.mark.asyncio
async def test_user_with_two_different_panel_accounts_is_reported_not_guessed(monkeypatch):
    """Две подписки на РАЗНЫЕ аккаунты — угадывать нечего, нужна строка в отчёте.

    Ветка `len(candidates) == 1` не покрывала этот случай, и он проваливался в
    телеграм-догадку: она вернула бы один из аккаунтов наугад и записала адрес,
    противоречащий части подписок, а отчёт назвал бы прогон полным.
    """
    tables = [UserModel.__table__, SubModel.__table__, GraceAccessSessionModel.__table__]
    async with memory_session(monkeypatch, tables) as db:
        await _seed(db, subs=[(10, 'a', 'uuid-a'), (20, 'b', 'uuid-b')])
        await db.commit()
        _patch_roster(
            monkeypatch,
            [
                panel_user(101, short_uuid='a', username='u1'),
                panel_user(202, short_uuid='b', username='u2'),
                panel_user(303, short_uuid='c', username='u3', telegram_id=551),
            ],
        )

        report = await backfill_remnawave_ids(db, dry_run=False)

        db.expunge_all()
        assert (await db.get(UserModel, 1)).remnawave_id is None, 'нельзя выбирать один из двух наугад'
        assert not report.complete
        assert any(r.kind == 'user' and r.row_id == 1 for r in report.unresolved)


@pytest.mark.asyncio
async def test_a_dead_persisted_id_does_not_beat_a_live_telegram_match(monkeypatch):
    """Сохранённый id, которого в панели уже нет, не должен ничего решать.

    Подмешивая ранее связанные подписки, легко втянуть протухший id: аккаунт
    удалили, а колонка осталась. Такой id становился бы каноническим адресом
    пользователя и бил бы корректное совпадение по telegram_id — либо, попав в
    компанию к живому, объявлял бы пользователя неоднозначным и блокировал бы
    разбор целиком. Поэтому подмешиваем только то, что панель подтверждает.
    """
    tables = [UserModel.__table__, SubModel.__table__, GraceAccessSessionModel.__table__]
    async with memory_session(monkeypatch, tables) as db:
        await _seed(db, subs=[(10, 'gone', 'uuid-a')])
        (await db.get(SubModel, 10)).remnawave_id = 505  # аккаунта 505 в панели больше нет
        (await db.get(SubModel, 10)).status = 'expired'
        await db.commit()
        _patch_roster(monkeypatch, [panel_user(909, short_uuid='live', username='u', telegram_id=551)])

        await backfill_remnawave_ids(db, dry_run=False)

        db.expunge_all()
        assert (await db.get(UserModel, 1)).remnawave_id == 909, 'живой аккаунт должен победить мёртвый id'


@pytest.mark.asyncio
async def test_every_write_is_recorded_in_the_audit_trail(monkeypatch):
    """Прогон обязан оставлять построчный след того, что записал.

    Без него на вопрос «какие строки прогон изменил и на что» после факта
    ответить нечем: отчёт нёс одни счётчики, а откатить ошибочную привязку
    вручную можно только зная строку и присвоенный аккаунт.
    """
    tables = [UserModel.__table__, SubModel.__table__, GraceAccessSessionModel.__table__]
    async with memory_session(monkeypatch, tables) as db:
        await _seed(db, subs=[(10, 'abc', 'uuid-a')])
        db.add(_grace('g-audit', 10, 'uuid-a'))
        await db.commit()
        _patch_roster(monkeypatch, [panel_user(77, short_uuid='abc', username='u', telegram_id=551)])

        report = await backfill_remnawave_ids(db, dry_run=False)

        kinds = {row.kind for row in report.applied}
        assert kinds == {'subscription', 'user', 'grace_session'}, kinds
        sub_row = next(r for r in report.applied if r.kind == 'subscription')
        assert sub_row.row_id == 10
        assert sub_row.panel_id == 77
        assert sub_row.strategy == 'short_uuid'
        # Полный след сериализуем без усечений — именно он уходит в файл.
        audit = report.as_audit()
        assert len(audit['applied']) == len(report.applied)
        assert audit['summary']['applied'] == len(report.applied)


def test_audit_filename_reflects_the_kind_of_run_not_the_commit_flag(tmp_path, monkeypatch):
    """Холостой прогон — это `dryrun`, а не `conflicts`.

    Имя строилось по флагу «записано ли», а холостой прогон не записывает
    по определению — и его файл получал имя `conflicts` при нуле конфликтов.
    Оператор видит в отчёте слово «конфликты» там, где их нет.
    """
    import importlib.util

    monkeypatch.setenv('BACKFILL_AUDIT_DIR', str(tmp_path))
    spec = importlib.util.spec_from_file_location('bf_cli', 'scripts/backfill_remnawave_ids.py')
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)

    dry = BackfillReport(dry_run=True)
    dry.unresolved.append(UnresolvedRow(kind='subscription', row_id=1, reason='short_uuid_not_found_in_panel'))
    assert 'dryrun' in cli._write_audit(dry, committed=False)

    applied = BackfillReport(dry_run=False)
    assert 'apply' in cli._write_audit(applied, committed=True)

    clashed = BackfillReport(dry_run=False)
    clashed.conflicts.append('sub#1 vs sub#2')
    assert 'conflicts' in cli._write_audit(clashed, committed=False)
