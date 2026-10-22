import pytest
from gremlin_town.review_card.latency import start_tap, finish_tap, phase, provider_elapsed
import logging


def test_provider_timing_is_scoped_and_contains_no_provider_identity(caplog):
    token = start_tap()
    try:
        with caplog.at_level(logging.INFO, logger="gremlin_town.review_card.latency"):
            with phase("read_items"):
                provider_elapsed("read_ref", .125)
                provider_elapsed("secret-record-and-url", .250)
        messages = [entry.getMessage() for entry in caplog.records]
        assert any("phase=github_read_ref parent=read_items seconds=0.125" in text for text in messages)
        assert any("phase=github_unknown parent=read_items" in text for text in messages)
        assert not any("secret-record-and-url" in text for text in messages)
    finally:
        finish_tap(token)
    caplog.clear()
    with caplog.at_level(logging.INFO):
        provider_elapsed("read_ref", .1)
    assert not caplog.records


@pytest.mark.anyio
async def test_host_correlates_real_storage_request_and_cleans_up_observer(caplog):
    from types import SimpleNamespace
    import asyncio
    from brain_core.github_storage import GitHubBrainHomeStorage, HTTPResponse
    from gremlin_town.review_card.host import InteractionHost
    from tests.review_card.test_host import Engine, Client, settings, event
    class Transport:
        def request(self, *args, **kwargs):
            return HTTPResponse(200, {}, b'{}')
    storage = GitHubBrainHomeStorage(repository="fixture/home", branch="accepted",
        token="fixture-only", transport=Transport())
    def read():
        with phase("read_items"):
            storage._request("GET", "/repos/fixture/home/git/ref/heads/accepted", expected=(200,))
    class ReadingEngine(Engine):
        async def handle(self, *args, **kwargs):
            await asyncio.to_thread(read)
            return SimpleNamespace(message={"components": []})
    host = InteractionHost(ReadingEngine([]), Client([]), settings())
    try:
        with caplog.at_level(logging.INFO, logger="gremlin_town.review_card.latency"):
            assert await host.interaction(event())
        provider = [entry.getMessage() for entry in caplog.records if "phase=github_read_ref " in entry.getMessage()]
        assert len(provider) == 1 and "parent=read_items" in provider[0] and "tap=" in provider[0]
        assert "fixture/home" not in provider[0] and "accepted" not in provider[0]
        caplog.clear()
        with caplog.at_level(logging.INFO):
            read()
        assert not caplog.records
    finally:
        host.close()
