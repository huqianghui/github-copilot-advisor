from aiohttp.test_utils import TestClient, TestServer

from teams_adapter.app import create_app


class StubAdapter:
    def __init__(self):
        self.processed = []

    async def process(self, request, bot):
        self.processed.append(request)
        from aiohttp import web
        return web.Response(status=201)


class StubBot:
    pass


async def test_healthz_ok():
    app = create_app(StubBot(), StubAdapter())
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/healthz")
        assert resp.status == 200
        assert await resp.text() == "ok"


async def test_messages_delegates_to_adapter():
    adapter = StubAdapter()
    app = create_app(StubBot(), adapter)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/messages", json={"type": "message"})
        assert resp.status == 201
        assert len(adapter.processed) == 1
