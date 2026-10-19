import asyncio

import pytest

from yuxi_im_gateway.runner import SessionQueue, approval_request_id


def test_approval_request_id_is_deterministic():
    assert approval_request_id("approve", "run-1") == approval_request_id("approve", "run-1")
    assert approval_request_id("approve", "run-1") != approval_request_id("reject", "run-1")


@pytest.mark.asyncio
async def test_session_queue_serializes_same_key():
    queue = SessionQueue()
    order: list[str] = []

    async def job(tag: str, delay: float) -> None:
        order.append(f"start-{tag}")
        await asyncio.sleep(delay)
        order.append(f"end-{tag}")

    await asyncio.gather(
        queue.run("k", lambda: job("a", 0.05)),
        queue.run("k", lambda: job("b", 0.0)),
    )
    assert order == ["start-a", "end-a", "start-b", "end-b"]


@pytest.mark.asyncio
async def test_session_queue_runs_different_keys_concurrently():
    queue = SessionQueue()
    order: list[str] = []

    async def job(tag: str, delay: float) -> None:
        order.append(f"start-{tag}")
        await asyncio.sleep(delay)
        order.append(f"end-{tag}")

    await asyncio.gather(
        queue.run("k1", lambda: job("a", 0.05)),
        queue.run("k2", lambda: job("b", 0.0)),
    )
    assert order.index("start-b") < order.index("end-a")
