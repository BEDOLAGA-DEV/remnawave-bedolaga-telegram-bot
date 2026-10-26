"""Build valid current gift callbacks for existing handler/financial scenarios."""

from app.handlers.subscription.gift import _gift_action_callback


async def bind_gift_action(callback, state, action='gift_confirm'):
    callback.data = _gift_action_callback(action, await state.get_data(), await state.get_state())
