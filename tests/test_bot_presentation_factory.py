from app.bot_factory import create_bot
from app.middlewares.bot_presentation_request import BotPresentationRequestMiddleware


def test_create_bot_registers_presentation_request_middleware() -> None:
    bot = create_bot(token='123456:TESTTOKEN')

    assert any(isinstance(item, BotPresentationRequestMiddleware) for item in bot.session.middleware)
