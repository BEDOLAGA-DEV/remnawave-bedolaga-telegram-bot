from structlog.testing import capture_logs

from app.services.platega_service import PlategaService


def test_sanitize_description_limits_utf8_bytes() -> None:
    original = 'Интернет-сервис - Пополнение баланса на 50 ₽ и ещё чуть-чуть'

    with capture_logs() as captured:
        trimmed = PlategaService._sanitize_description(original, 64)

    assert len(trimmed.encode('utf-8')) <= 64
    assert trimmed != original
    # Production now logs the trim event via structlog (not stdlib logging),
    # so assert against structlog's captured events rather than caplog.records.
    assert any('trimmed' in event.get('event', '') for event in captured)


def test_sanitize_description_returns_clean_value() -> None:
    original = '  Обычное описание  '

    trimmed = PlategaService._sanitize_description(original, 64)

    assert trimmed == 'Обычное описание'
    assert len(trimmed.encode('utf-8')) <= 64
