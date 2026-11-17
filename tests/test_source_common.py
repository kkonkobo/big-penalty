import asyncio
import threading

import pandas as pd
import pytest

from jobhunt.models import SearchQuery
from jobhunt.sources._common import jobspy_scrape


async def test_jobspy_timeout_does_not_overlap_worker_threads(monkeypatch):
    import jobspy
    from jobspy.util import RotatingProxySession

    first_started, release_first, second_started = threading.Event(), threading.Event(), threading.Event()
    calls = []
    event_loop = asyncio.get_running_loop()
    first_signal = event_loop.create_future()
    second_signal = event_loop.create_future()

    def scrape(**kwargs):
        calls.append(kwargs["search_term"])
        if kwargs["search_term"] == "first":
            first_started.set()
            event_loop.call_soon_threadsafe(first_signal.set_result, None)
            assert release_first.wait(timeout=3)
        else:
            second_started.set()
            event_loop.call_soon_threadsafe(second_signal.set_result, None)
        return pd.DataFrame()

    monkeypatch.setattr(jobspy, "scrape_jobs", scrape)
    first = asyncio.create_task(jobspy_scrape("indeed", SearchQuery(keywords="first")))
    second = None
    try:
        await asyncio.wait_for(first_signal, 2)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(first, 0.01)
        second = asyncio.create_task(jobspy_scrape("indeed", SearchQuery(keywords="second")))
        await asyncio.sleep(0.05)
        assert first_started.is_set() and not second_started.is_set()
        release_first.set()
        await asyncio.wait_for(second_signal, 2)
        assert await second == []
        assert calls == ["first", "second"] and 0 < RotatingProxySession.request_timeout <= 30
    finally:
        release_first.set()
        await asyncio.gather(first, *([second] if second else []), return_exceptions=True)
