"""Unit-тесты чистого билдера статуса СБП-автопродления Platega
(``_platega_sbp_status_text``, ``app/handlers/subscription/autopay.py``).

Чистая функция — не трогает БД/сеть, поэтому тестируется без сессии и моков:
только ``SimpleNamespace``-заглушка записи (или ``None``) и реальный
``get_texts('ru')`` (ключи для этого билдера сознательно не заведены в
locale-файлах — как и у остальных autopay-хендлеров, текст берётся из
inline-дефолта ``texts.t(key, default)``, см. существующие AUTOPAY_* вызовы
в этом же модуле).
"""

from datetime import UTC, datetime
from types import SimpleNamespace

from app.handlers.subscription.autopay import _platega_sbp_status_text
from app.localization.texts import get_texts
from app.utils.formatters import format_datetime


texts = get_texts('ru')


def test_none_record_means_not_connected():
    assert _platega_sbp_status_text(None, texts) == 'не подключено'


def test_pending_status():
    record = SimpleNamespace(status='PENDING', next_charge_at=None)
    assert _platega_sbp_status_text(record, texts) == 'ожидает подтверждения в банке'


def test_active_status_with_next_charge_at():
    next_charge_at = datetime(2026, 9, 1, 12, 30, tzinfo=UTC)
    record = SimpleNamespace(status='ACTIVE', next_charge_at=next_charge_at)

    result = _platega_sbp_status_text(record, texts)

    assert result == f'активно (следующее списание {format_datetime(next_charge_at)})'


def test_active_status_without_next_charge_at_shows_placeholder():
    """ACTIVE достижим и без next_charge_at — например, сразу после коллбека
    SUBSCRIPTION_ACTIVATED, который выставляет статус, но ещё не знает дату
    следующего списания (её сообщает только последующий коллбек по charge).
    """
    record = SimpleNamespace(status='ACTIVE', next_charge_at=None)

    result = _platega_sbp_status_text(record, texts)

    assert result == 'активно (следующее списание уточняется)'


def test_past_due_status():
    record = SimpleNamespace(status='PAST_DUE', next_charge_at=None)
    assert _platega_sbp_status_text(record, texts) == 'просрочено'


def test_cancelled_status():
    record = SimpleNamespace(status='CANCELLED', next_charge_at=None)
    assert _platega_sbp_status_text(record, texts) == 'отменено'


def test_failed_status():
    record = SimpleNamespace(status='FAILED', next_charge_at=None)
    assert _platega_sbp_status_text(record, texts) == 'не удалось подключить'


def test_unknown_status_falls_back_to_raw_value():
    """Защитная ветка: неизвестный статус не должен молча теряться (как и в
    process_platega_subscription_callback, где нераспознанный Status логируется,
    а не игнорируется тихо) — билдер показывает сырое значение статуса.
    """
    record = SimpleNamespace(status='SOMETHING_ELSE', next_charge_at=None)
    assert _platega_sbp_status_text(record, texts) == 'статус неизвестен (SOMETHING_ELSE)'
