import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ai_support_bot.app.bot import bot as bot_mod
from ai_support_bot.app.bot.message_queue import MessageQueue
from ai_support_bot.tests.test_bot_escalation_delivery import FakeMessage, _patch_settings


@pytest.mark.asyncio
@pytest.mark.parametrize('photos', [1, 2])
async def test_screenshots_and_following_text_are_one_request(monkeypatch, photos):
    _patch_settings(monkeypatch, {'THROTTLE_SECONDS': 4, 'DAILY_MESSAGE_LIMIT': 0, 'VISION_ENABLED': 1})
    queue = MessageQueue(quiet_seconds=0.01, max_wait=0.05)
    monkeypatch.setattr(bot_mod, '_message_queue', queue)
    monkeypatch.setattr(bot_mod, '_processed_updates', {})
    monkeypatch.setattr(
        bot_mod, '_download_image_data_url', AsyncMock(side_effect=lambda bot, file_id: f'data:{file_id}')
    )
    captured = []

    async def generate(db, user_id, question, image_url=None, **kwargs):
        captured.append((question, image_url, kwargs))
        return {'answer': 'Ответ по скриншотам', 'escalate': False}

    monkeypatch.setattr(bot_mod.support_agent, 'generate_answer', generate)
    messages = []
    for index in range(photos):
        msg = FakeMessage(555, '', message_id=index + 1)
        msg.photo = [SimpleNamespace(file_id=f'photo-{index}')]
        msg.caption = 'Incy на iPhone' if index == 0 else None
        messages.append(msg)
    messages.append(FakeMessage(555, 'Ничего не заработало', message_id=photos + 1))
    await asyncio.gather(*(bot_mod.queue_message(msg) for msg in messages))
    assert len(captured) == 1
    question, images, options = captured[0]
    assert question == 'Incy на iPhone\nНичего не заработало'
    assert images == ('data:photo-0' if photos == 1 else ['data:photo-0', 'data:photo-1'])
    assert options['media_file_id'] == ('photo-0' if photos == 1 else ['photo-0', 'photo-1'])
    assert [answer for msg in messages for answer in msg.answers] == ['Ответ по скриншотам']
    assert not queue._states


@pytest.mark.asyncio
async def test_arrivals_during_generation_are_processed_next_without_parallelism():
    queue = MessageQueue(quiet_seconds=0.001, max_wait=0.01)
    entered, release = asyncio.Event(), asyncio.Event()
    batches = []

    async def process(batch):
        batches.append(batch)
        if len(batches) == 1:
            entered.set()
            await release.wait()

    first = asyncio.create_task(queue.submit('chat', 1, process))
    await asyncio.wait_for(entered.wait(), 1)
    second = asyncio.create_task(queue.submit('chat', 2, process))
    third = asyncio.create_task(queue.submit('chat', 3, process))
    await asyncio.sleep(0)
    assert batches == [[1]]
    release.set()
    await asyncio.gather(first, second, third)
    assert batches == [[1], [2, 3]]


@pytest.mark.asyncio
async def test_chat_keys_are_isolated_and_batch_size_is_bounded():
    queue = MessageQueue(quiet_seconds=0.001, max_wait=0.01, batch_size=2)
    batches = []

    async def process(batch):
        batches.append(batch)

    await asyncio.gather(*(queue.submit('chat-a', i, process) for i in range(5)), queue.submit('chat-b', 100, process))
    assert all(len(batch) <= 2 for batch in batches)
    assert [100] in batches
    assert sorted(i for batch in batches for i in batch) == [0, 1, 2, 3, 4, 100]


@pytest.mark.asyncio
async def test_interval_delays_next_request_instead_of_rejecting_it():
    queue = MessageQueue(quiet_seconds=0, max_wait=0)
    starts = []

    async def process(batch):
        starts.append(asyncio.get_running_loop().time())

    await queue.submit('chat', 1, process, interval=0.03)
    await queue.submit('chat', 2, process, interval=0.03)
    assert len(starts) == 2
    assert starts[1] - starts[0] >= 0.025


@pytest.mark.asyncio
async def test_close_cancels_pending_requests_and_cleans_workers():
    queue = MessageQueue(quiet_seconds=60)
    task = asyncio.create_task(queue.submit('chat', 1, AsyncMock()))
    await asyncio.sleep(0)
    await queue.close()
    assert isinstance((await asyncio.gather(task, return_exceptions=True))[0], asyncio.CancelledError)
    assert not queue._states


@pytest.mark.asyncio
async def test_flood_queue_is_bounded():
    queue = MessageQueue(quiet_seconds=60, pending_limit=2)
    tasks = [asyncio.create_task(queue.submit('chat', i, AsyncMock())) for i in range(2)]
    await asyncio.sleep(0)
    with pytest.raises(OverflowError):
        await queue.submit('chat', 3, AsyncMock())
    await queue.close()
    await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_failed_request_does_not_block_queued_followup():
    queue = MessageQueue(quiet_seconds=0, max_wait=0)
    entered, release = asyncio.Event(), asyncio.Event()
    batches = []

    async def process(batch):
        batches.append(batch)
        if batch == [1]:
            entered.set()
            await release.wait()
            raise RuntimeError('Temporary failure')

    first = asyncio.create_task(queue.submit('chat', 1, process))
    await asyncio.wait_for(entered.wait(), 1)
    second = asyncio.create_task(queue.submit('chat', 2, process))
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(first, second, return_exceptions=True)
    assert isinstance(results[0], RuntimeError)
    assert results[1] is None
    assert batches == [[1], [2]]
