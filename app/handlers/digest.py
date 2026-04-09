import structlog
from aiogram import Dispatcher, F, types
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import User
from app.utils.decorators import error_handler


logger = structlog.get_logger(__name__)


@error_handler
async def digest_off(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    db_user.digest_enabled = False
    await db.commit()
    await callback.answer('\u0414\u0430\u0439\u0434\u0436\u0435\u0441\u0442 \u043e\u0442\u043a\u043b\u044e\u0447\u0451\u043d', show_alert=False)


@error_handler
async def digest_on(callback: types.CallbackQuery, db_user: User, db: AsyncSession):
    db_user.digest_enabled = True
    await db.commit()
    await callback.answer('\u0414\u0430\u0439\u0434\u0436\u0435\u0441\u0442 \u0432\u043a\u043b\u044e\u0447\u0451\u043d', show_alert=False)


def register_handlers(dp: Dispatcher):
    dp.callback_query.register(digest_off, F.data == 'nz!_digest_off')
    dp.callback_query.register(digest_on, F.data == 'nz!_digest_on')
