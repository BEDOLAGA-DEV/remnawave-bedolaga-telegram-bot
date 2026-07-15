from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerInlineQuery, SendMediaGroup, SendMessage, SendPhoto, SendPoll, SendRichMessage
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputMediaPhoto,
    InputPollOption,
    InputRichMessage,
    InputTextMessageContent,
    MessageEntity,
)

from app.middlewares.bot_presentation_request import (
    BotPresentationRequestMiddleware,
    apply_method_presentation,
)
from app.services.bot_presentation_service import (
    BotPresentationConfig,
    clear_bot_presentation_cache,
    decorate_localized_text,
    set_bot_presentation_cache,
)


CUSTOM_ID = '5368324170671202286'
KEY = 'PROMO_GROUP_DISCOUNT_TRAFFIC'
TOKEN = f'{KEY}#0:📊'


def teardown_function() -> None:
    clear_bot_presentation_cache()


def marked(value: str = '📊 Трафик') -> str:
    set_bot_presentation_cache(BotPresentationConfig(emoji_overrides={TOKEN: CUSTOM_ID}))
    return decorate_localized_text('ru', KEY, value)


def test_send_message_is_decorated_at_request_boundary() -> None:
    source = marked('📊 <b>Трафик</b>')
    method = SendMessage(
        chat_id=1,
        text=source,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=marked(), callback_data='traffic')]]
        ),
    )

    rendered = apply_method_presentation(method, default_parse_mode='HTML')

    assert rendered.text.startswith(f'<tg-emoji emoji-id="{CUSTOM_ID}">📊</tg-emoji>')
    assert rendered.reply_markup.inline_keyboard[0][0].text == 'Трафик'
    assert rendered.reply_markup.inline_keyboard[0][0].icon_custom_emoji_id == CUSTOM_ID
    assert method.text == source


def test_explicit_entities_restore_unicode_and_offsets() -> None:
    method = SendMessage(
        chat_id=1,
        text=marked(),
        entities=[MessageEntity(type='bold', offset=3, length=6)],
    )

    rendered = apply_method_presentation(method, default_parse_mode='HTML')

    assert rendered.text == '📊 Трафик'
    assert rendered.entities == method.entities


def test_explicit_parse_mode_none_restores_unicode_without_html() -> None:
    method = SendMessage(chat_id=1, text=marked('📊 plain'), parse_mode=None)
    rendered = apply_method_presentation(method, default_parse_mode='HTML')
    assert rendered.text == '📊 plain'
    assert '<tg-emoji' not in rendered.text


def test_explicit_caption_parse_mode_none_restores_unicode() -> None:
    method = SendPhoto(chat_id=1, photo='file-id', caption=marked('📊 plain'), parse_mode=None)
    rendered = apply_method_presentation(method, default_parse_mode='HTML')
    assert rendered.caption == '📊 plain'


def test_explicit_input_media_parse_mode_none_restores_unicode() -> None:
    method = SendMediaGroup(
        chat_id=1,
        media=[InputMediaPhoto(media='file-id', caption=marked('📊 plain'), parse_mode=None)],
    )
    rendered = apply_method_presentation(method, default_parse_mode='HTML')
    assert rendered.media[0].caption == '📊 plain'


def test_poll_question_options_and_explanation_are_rendered_recursively() -> None:
    method = SendPoll(
        chat_id=1,
        question=marked('📊 Вопрос'),
        options=[InputPollOption(text=marked('📊 Ответ'))],
        explanation=marked('📊 Пояснение'),
    )

    rendered = apply_method_presentation(method, default_parse_mode='HTML')
    fallback = apply_method_presentation(method, default_parse_mode='HTML', custom=False)

    assert '<tg-emoji' in rendered.question
    assert '<tg-emoji' in rendered.options[0].text
    assert '<tg-emoji' in rendered.explanation
    assert fallback.question == '📊 Вопрос'
    assert fallback.options[0].text == '📊 Ответ'
    assert fallback.explanation == '📊 Пояснение'


def test_inline_query_input_message_content_is_rendered_recursively() -> None:
    method = AnswerInlineQuery(
        inline_query_id='query',
        results=[
            InlineQueryResultArticle(
                id='result',
                title='title',
                input_message_content=InputTextMessageContent(message_text=marked('📊 Текст')),
            )
        ],
    )

    rendered = apply_method_presentation(method, default_parse_mode='HTML')
    fallback = apply_method_presentation(method, default_parse_mode='HTML', custom=False)
    rendered_content = rendered.results[0].input_message_content
    fallback_content = fallback.results[0].input_message_content

    assert isinstance(rendered_content, InputTextMessageContent)
    assert isinstance(fallback_content, InputTextMessageContent)
    assert '<tg-emoji' in rendered_content.message_text
    assert fallback_content.message_text == '📊 Текст'


async def test_middleware_retries_unicode_fallback_when_custom_request_is_rejected() -> None:
    method = SendMessage(chat_id=1, text=marked('📊 test'))
    calls = []

    async def make_request(bot, candidate):
        calls.append(candidate)
        if len(calls) == 1:
            raise TelegramBadRequest(method=candidate, message='CUSTOM_EMOJI_INVALID')
        return 'ok'

    bot = type('BotStub', (), {'default': type('DefaultStub', (), {'parse_mode': 'HTML'})()})()
    result = await BotPresentationRequestMiddleware()(make_request, bot, method)

    assert result == 'ok'
    assert '<tg-emoji' in calls[0].text
    assert calls[1].text == '📊 test'
    assert '\ue000' not in calls[1].text


def test_rich_message_html_is_decorated() -> None:
    method = SendRichMessage(chat_id=1, rich_message=InputRichMessage(html=f'<p>{marked()} </p>'))
    rendered = apply_method_presentation(method, default_parse_mode='HTML')
    assert rendered.rich_message.html == (
        f'<p><tg-emoji emoji-id="{CUSTOM_ID}">📊</tg-emoji> Трафик </p>'
    )
