from types import SimpleNamespace

from app.database.crud.user import emit_user_created_event


async def test_emit_user_created_event_uses_persisted_user(monkeypatch):
    calls = []

    async def emit(name, payload, db):
        calls.append((name, payload, db))

    monkeypatch.setattr('app.services.event_emitter.event_emitter.emit', emit)
    db = object()
    user = SimpleNamespace(
        id=9,
        telegram_id=123,
        username='u',
        first_name='F',
        last_name='L',
        referral_code='r',
        referred_by_id=None,
    )

    await emit_user_created_event(db, user)

    assert calls == [
        (
            'user.created',
            {
                'user_id': 9,
                'telegram_id': 123,
                'username': 'u',
                'first_name': 'F',
                'last_name': 'L',
                'referral_code': 'r',
                'referred_by_id': None,
            },
            db,
        )
    ]
