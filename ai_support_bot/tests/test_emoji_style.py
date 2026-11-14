from ai_support_bot.app.services.emoji_style import CUSTOM_EMOJI, decorate
from ai_support_bot.app.services.telegram_html import sanitize_telegram_html


def tag(emoji: str) -> str:
    return f'<tg-emoji emoji-id="{CUSTOM_EMOJI[emoji]}">{emoji}</tg-emoji>'


def test_line_leading_emoji_become_premium_and_are_capped_at_two():
    text = '🔑 <b>Ссылка</b> в разделе «Мои подписки».\n🏷️ Стандарт — 219 ₽.\n✅ Готово.'

    assert decorate(text) == f'{tag("🔑")} <b>Ссылка</b> в разделе «Мои подписки».\n{tag("🏷")} Стандарт — 219 ₽.\nГотово.'


def test_emoji_inside_a_sentence_and_foreign_emoji_stay_untouched():
    text = 'Нажмите 🎁 в меню 😀'

    assert decorate(text) == text


def test_same_emoji_on_list_items_is_kept_once():
    text = '- 🛡 Перезапустите приложение\n- 🛡 Смените сервер'

    assert decorate(text) == f'- {tag("🛡")} Перезапустите приложение\n- Смените сервер'


def test_lead_is_added_once_and_plain_answers_lose_all_set_emoji():
    assert decorate('Передаю обращение команде.', lead='🎧') == f'{tag("🎧")} Передаю обращение команде.'
    assert decorate('🎧 Передаю обращение команде.', lead='🎧') == f'{tag("🎧")} Передаю обращение команде.'
    assert decorate('🔑 Пришлите чек этой операции.', lead='🎧', plain=True) == 'Пришлите чек этой операции.'


def test_decorate_is_idempotent_and_survives_sanitizing():
    once = decorate('🚀 Для Android подойдут Incy и Happ.')

    assert decorate(once) == once
    assert sanitize_telegram_html(once) == once


def test_sanitizer_drops_emoji_ids_outside_the_set():
    assert sanitize_telegram_html('<tg-emoji emoji-id="123">😀</tg-emoji> текст') == '😀 текст'
