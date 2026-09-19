"""Event bus and SSE.

httpx's ASGITransport runs the whole app before returning a response, so these
tests use `/events?limit=N` to make the stream finite and publish from a
concurrent task while the app is still awaiting its next frame. Never drop the
`limit`: without it the request cannot complete in-process and pytest hangs
instead of failing.
"""

from __future__ import annotations

import asyncio

import httpx

from app.agent.state import EventBus
from app.main import app


def test_bus_fans_out_to_every_subscriber():
    async def run():
        bus = EventBus()
        first, second = bus.subscribe(), bus.subscribe()
        bus.publish({"type": "ping"})
        assert first.get_nowait() == {"type": "ping"}
        assert second.get_nowait() == {"type": "ping"}

    asyncio.run(run())


def test_unsubscribe_stops_delivery():
    async def run():
        bus = EventBus()
        queue = bus.subscribe()
        bus.unsubscribe(queue)
        bus.publish({"type": "ping"})
        assert queue.empty()
        assert bus.subscriber_count == 0

    asyncio.run(run())


def test_slow_subscriber_is_dropped_not_buffered_forever():
    async def run():
        bus = EventBus(maxsize=2)
        queue = bus.subscribe()
        for _ in range(3):
            bus.publish({"type": "ping"})
        assert bus.subscriber_count == 0
        assert queue.qsize() == 2

    asyncio.run(run())


async def test_sse_stream_opens_with_a_hello_frame():
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/events?limit=1")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert '"hello"' in response.text


async def test_agent_log_reaches_the_stream():
    async with app.router.lifespan_context(app):

        async def publish_soon():
            await asyncio.sleep(0.05)
            app.state.agent.log("perceive", "測試用紀錄")

        publisher = asyncio.create_task(publish_soon())
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            async with asyncio.timeout(5):
                response = await client.get("/events?limit=2")
        await publisher

    assert "agent_log" in response.text
    assert "測試用紀錄" in response.text


async def test_subscriber_is_released_when_the_stream_ends():
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await client.get("/events?limit=1")
        assert app.state.agent.bus.subscriber_count == 0
