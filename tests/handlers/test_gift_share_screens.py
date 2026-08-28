"""QR подарка и готовый текст для отправки.

Оба экрана адресуют подарок номером из callback'а, поэтому главное здесь —
что чужой подарок по номеру не открывается, а активированный не показывается
вовсе: его ссылка уже недействительна, и делиться ею значит отправить человека
в тупик.
"""

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.handlers.subscription import gift as screen


def _item(purchase_id: int = 7, *, claimable: bool = True):
    return SimpleNamespace(
        purchase_id=purchase_id,
        token='g' * 64,
        tariff_name='VIP',
        period_days=30,
        is_claimable=claimable,
    )


class _Message:
    def __init__(self):
        self.edit_text = AsyncMock()
        self.edit_media = AsyncMock()
        self.answer_photo = AsyncMock()
        self.delete = AsyncMock()


def _callback(data: str):
    return SimpleNamespace(
        data=data,
        message=_Message(),
        answer=AsyncMock(),
        bot=SimpleNamespace(),
        from_user=SimpleNamespace(id=1),
    )


@pytest.fixture
def wired(monkeypatch, tmp_path):
    store = {'item': _item(), 'asked': []}

    async def fake_get(_db, *, buyer_id, purchase_id):
        store['asked'].append((buyer_id, purchase_id))
        item = store['item']
        return item if item and item.purchase_id == purchase_id else None

    async def fake_channel(*, bot):
        return 'TestGiftBot', 'https://cabinet.example.com'

    monkeypatch.setattr(screen, 'get_sender_gift', fake_get)
    monkeypatch.setattr(screen, 'resolve_gift_claim_channel', fake_channel)
    # QR пишется на диск — уводим в каталог теста, чтобы не трогать data/.
    monkeypatch.chdir(tmp_path)
    return store


def _user(uid: int = 1):
    return SimpleNamespace(id=uid, language='ru')


class TestAccessControl:
    @pytest.mark.asyncio
    @pytest.mark.parametrize('handler', ['handle_gift_my_qr', 'handle_gift_my_text'])
    async def test_someone_elses_gift_is_refused(self, wired, handler):
        """Номер приходит из callback'а — без привязки к покупателю открылся бы чужой."""
        callback = _callback(f'{handler.replace("handle_", "").replace("_my_", "_my_")}:999')
        await getattr(screen, handler)(callback, db_user=_user(), db=None, state=None)

        assert callback.answer.await_args.kwargs.get('show_alert') is True
        callback.message.edit_text.assert_not_awaited()
        callback.message.edit_media.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize('handler', ['handle_gift_my_qr', 'handle_gift_my_text'])
    async def test_owner_is_taken_from_the_session_not_the_callback(self, wired, handler):
        """Покупателя берём из db_user: иначе номер в callback'е решал бы всё."""
        await getattr(screen, handler)(_callback('gift_my_qr:7'), db_user=_user(42), db=None, state=None)
        assert wired['asked'] == [(42, 7)]

    @pytest.mark.asyncio
    @pytest.mark.parametrize('handler', ['handle_gift_my_qr', 'handle_gift_my_text'])
    async def test_activated_gift_is_not_shared(self, wired, handler):
        """Ссылка активированного подарка недействительна — делиться нечем."""
        wired['item'] = _item(claimable=False)
        callback = _callback('gift_my_qr:7')

        await getattr(screen, handler)(callback, db_user=_user(), db=None, state=None)

        assert 'активирован' in callback.answer.await_args.args[0]
        callback.message.edit_text.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize('handler', ['handle_gift_my_qr', 'handle_gift_my_text'])
    async def test_malformed_callback_is_refused(self, wired, handler):
        callback = _callback('gift_my_qr:не-число')
        await getattr(screen, handler)(callback, db_user=_user(), db=None, state=None)

        assert callback.answer.await_args.kwargs.get('show_alert') is True


class TestQrScreen:
    @pytest.mark.asyncio
    async def test_qr_is_generated_for_the_claim_link(self, wired):
        callback = _callback('gift_my_qr:7')
        await screen.handle_gift_my_qr(callback, db_user=_user(), db=None, state=None)

        callback.message.edit_media.assert_awaited()
        files = list((Path('data') / 'gift_qr').glob('*.png'))
        assert len(files) == 1, files
        assert files[0].name.startswith('7_')

    @pytest.mark.asyncio
    async def test_cached_file_is_keyed_by_the_link(self, wired):
        """Сменится канал выдачи — старый QR вёл бы на прежний адрес."""
        from app.utils.gift_links import build_gift_claim_artifacts

        artifacts = build_gift_claim_artifacts(
            token=wired['item'].token, bot_username='TestGiftBot', cabinet_url='https://cabinet.example.com'
        )
        link = artifacts.bot_claim_url or artifacts.cabinet_claim_url
        expected = hashlib.md5(link.encode()).hexdigest()[:8]

        await screen.handle_gift_my_qr(_callback('gift_my_qr:7'), db_user=_user(), db=None, state=None)

        assert (Path('data') / 'gift_qr' / f'7_{expected}.png').exists()

    @pytest.mark.asyncio
    async def test_falls_back_to_a_new_message_when_media_cannot_replace_text(self, wired):
        """Текстовое сообщение нельзя заменить фотографией — Telegram отвечает ошибкой."""
        from aiogram.exceptions import TelegramBadRequest

        callback = _callback('gift_my_qr:7')
        callback.message.edit_media.side_effect = TelegramBadRequest(method=None, message='no media')

        await screen.handle_gift_my_qr(callback, db_user=_user(), db=None, state=None)

        callback.message.delete.assert_awaited()
        callback.message.answer_photo.assert_awaited()


class TestCopyTextScreen:
    @pytest.mark.asyncio
    async def test_message_carries_link_and_code(self, wired):
        callback = _callback('gift_my_text:7')
        await screen.handle_gift_my_text(callback, db_user=_user(), db=None, state=None)

        text = callback.message.edit_text.await_args.args[0]
        assert 'VIP' in text and '30' in text
        assert 'TestGiftBot' in text, 'ссылка активации обязана быть в тексте'
        assert '<pre>' in text, 'текст должен копироваться одним нажатием'

    @pytest.mark.asyncio
    async def test_tariff_name_is_escaped(self, wired):
        """Название задаёт человек: угловая скобка иначе оборвала бы разметку."""
        wired['item'] = SimpleNamespace(
            purchase_id=7, token='g' * 64, tariff_name='<b>VIP</b>', period_days=30, is_claimable=True
        )
        callback = _callback('gift_my_text:7')

        await screen.handle_gift_my_text(callback, db_user=_user(), db=None, state=None)

        text = callback.message.edit_text.await_args.args[0]
        assert '&lt;b&gt;VIP&lt;/b&gt;' in text
        assert '<b>VIP</b>' not in text.split('<pre>')[1]

    @pytest.mark.asyncio
    async def test_back_button_returns_to_the_gift_card(self, wired):
        """Пользователь пришёл с карточки — туда и возвращаем, а не в список."""
        callback = _callback('gift_my_text:7')
        await screen.handle_gift_my_text(callback, db_user=_user(), db=None, state=None)

        markup = callback.message.edit_text.await_args.kwargs['reply_markup']
        actions = [b.callback_data for row in markup.inline_keyboard for b in row]
        assert actions == ['gift_my_open:7'], actions


def test_both_screens_are_registered():
    """Кнопка без обработчика молча ничего не делает."""
    import inspect

    source = inspect.getsource(screen.register_gift_handlers)
    assert 'handle_gift_my_qr' in source
    assert 'handle_gift_my_text' in source


def test_card_offers_both_buttons():
    from app.services.gift_notification_service import build_gift_history_detail_presentation

    item = SimpleNamespace(
        purchase_id=7,
        token='g' * 64,
        status='paid',
        tariff_name='VIP',
        period_days=30,
        traffic_limit_gb=100,
        device_limit=2,
        created_at=datetime.now(UTC),
        delivered_at=None,
        recipient_display=None,
        is_claimable=True,
        public_code='g' * 12,
    )
    _text, keyboard = build_gift_history_detail_presentation(
        language='ru', item=item, bot_username='TestGiftBot', cabinet_url='https://cabinet.example.com'
    )
    actions = [b.callback_data for row in keyboard.inline_keyboard for b in row if b.callback_data]

    assert 'gift_my_qr:7' in actions
    assert 'gift_my_text:7' in actions


class TestGiftCardOwnership:
    """Открытие карточки подарка тоже адресуется номером из callback'а.

    Проверка владельца там была, но ни один тест её не закреплял: подмена
    ``buyer_id`` на что угодно проходила весь набор незамеченной. Обнаружено
    мутацией, поставленной для соседних экранов.
    """

    @pytest.mark.asyncio
    async def test_owner_is_taken_from_the_session(self, wired):
        callback = _callback('gift_my_open:7')
        wired['item'] = SimpleNamespace(
            purchase_id=7,
            token='g' * 64,
            tariff_name='VIP',
            period_days=30,
            traffic_limit_gb=100,
            device_limit=2,
            created_at=datetime.now(UTC),
            delivered_at=None,
            recipient_display=None,
            is_claimable=True,
            public_code='g' * 12,
        )

        await screen.handle_gift_my_open(callback, db_user=_user(42), db=None, state=None)

        assert wired['asked'] == [(42, 7)], 'покупателя берём из сессии, а не из callback-а'

    @pytest.mark.asyncio
    async def test_someone_elses_gift_is_refused(self, wired):
        callback = _callback('gift_my_open:999')

        await screen.handle_gift_my_open(callback, db_user=_user(), db=None, state=None)

        assert callback.answer.await_args.kwargs.get('show_alert') is True
        callback.message.edit_text.assert_not_awaited()
