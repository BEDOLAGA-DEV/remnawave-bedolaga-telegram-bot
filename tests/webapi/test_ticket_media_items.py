"""Галерея сообщения доходит до внешнего API, а не только до кабинета.

Клиент присылает несколько скриншотов одной пачкой — в базе это `media_items`.
Схема ответа поле объявляла, но сериализатор его не заполнял, поэтому внешняя
интеграция видела ровно один файл и не могла добраться до остальных. Здесь
сторож на то, что список доезжает целиком и что мусор в нём не роняет ответ.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from app.webapi.routes.tickets import _serialize_message


def _message(**overrides):
    base = dict(
        id=1,
        user_id=7,
        message_text='скриншоты',
        is_from_admin=False,
        has_media=True,
        media_type='photo',
        media_file_id='AgACfirst',
        media_caption=None,
        media_items=None,
        created_at=datetime.now(UTC),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_gallery_is_serialized():
    message = _message(
        media_items=[
            {'type': 'photo', 'file_id': 'AgACfirst', 'caption': 'первый'},
            {'type': 'photo', 'file_id': 'AgACsecond', 'caption': None},
            {'type': 'document', 'file_id': 'BQAClog'},
        ]
    )

    response = _serialize_message(message)

    assert [item.file_id for item in response.media_items] == ['AgACfirst', 'AgACsecond', 'BQAClog']
    assert response.media_items[0].caption == 'первый'
    assert response.media_items[2].type == 'document'


def test_single_file_message_has_no_gallery():
    response = _serialize_message(_message())

    assert response.media_items is None
    assert response.media_file_id == 'AgACfirst'


def test_gallery_alone_still_counts_as_media():
    """У пачки может не быть основного file_id — сообщение всё равно с медиа."""
    message = _message(has_media=False, media_file_id=None, media_items=[{'type': 'photo', 'file_id': 'AgACx'}])

    response = _serialize_message(message)

    assert response.has_media is True
    assert len(response.media_items) == 1


def test_broken_gallery_does_not_break_the_answer():
    message = _message(media_items=[{'type': 'photo'}])  # без file_id

    response = _serialize_message(message)

    assert response.media_items is None
    assert response.media_file_id == 'AgACfirst'
