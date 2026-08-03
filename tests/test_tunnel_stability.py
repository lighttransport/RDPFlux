"""Live stability harness for a running rdpflux tunnel.

These tests drive real traffic through an already-running tunnel, so they are
opt-in: set RDPFLUX_STABILITY_URL to a forwarded HTTP endpoint (e.g. a local
forward pointing at an HTTP server on the RDP host) to enable them.

    RDPFLUX_STABILITY_URL=http://127.0.0.1:18000/ python -m pytest tests/test_tunnel_stability.py -v

They exercise the mux under load: rapid stream open/close churn, concurrency,
sustained traffic, and (optionally) large transfers verified for byte integrity.
Tunable via environment:

    RDPFLUX_STABILITY_URL         forwarded endpoint to hammer (enables the suite)
    RDPFLUX_STABILITY_LARGE_URL   optional larger resource for the transfer test
    RDPFLUX_STABILITY_CONCURRENCY concurrent workers            (default 16)
    RDPFLUX_STABILITY_REQUESTS    requests in the concurrency test (default 200)
    RDPFLUX_STABILITY_DURATION    sustained-load seconds        (default 10)
    RDPFLUX_STABILITY_TIMEOUT     per-request timeout seconds   (default 30)
"""
from __future__ import annotations

import concurrent.futures as cf
import hashlib
import os
import time
import urllib.request

import pytest

URL = os.environ.get("RDPFLUX_STABILITY_URL")
LARGE_URL = os.environ.get("RDPFLUX_STABILITY_LARGE_URL")
CONCURRENCY = int(os.environ.get("RDPFLUX_STABILITY_CONCURRENCY", "16"))
REQUESTS = int(os.environ.get("RDPFLUX_STABILITY_REQUESTS", "200"))
DURATION = float(os.environ.get("RDPFLUX_STABILITY_DURATION", "10"))
TIMEOUT = float(os.environ.get("RDPFLUX_STABILITY_TIMEOUT", "30"))

pytestmark = pytest.mark.skipif(
    not URL,
    reason="set RDPFLUX_STABILITY_URL to a forwarded HTTP endpoint to run the tunnel stability suite",
)


def _fetch(url: str) -> tuple[int, str, float]:
    """Return (body length, sha256, elapsed) or raise so the caller records a failure."""
    start = time.time()
    response = urllib.request.urlopen(url, timeout=TIMEOUT)
    body = response.read()
    if response.status != 200:
        raise AssertionError(f"unexpected HTTP status {response.status}")
    return len(body), hashlib.sha256(body).hexdigest(), time.time() - start


def _run(url: str, count: int, workers: int) -> list:
    results: list = []
    with cf.ThreadPoolExecutor(workers) as pool:
        futures = [pool.submit(_fetch, url) for _ in range(count)]
        for future in cf.as_completed(futures):
            try:
                results.append((True, future.result()))
            except Exception as exc:  # noqa: BLE001 - report every failure kind
                results.append((False, f"{type(exc).__name__}: {exc}"))
    return results


def _assert_clean(results: list) -> set[int]:
    failures = [detail for ok, detail in results if not ok]
    assert not failures, f"{len(failures)}/{len(results)} requests failed: {failures[:5]}"
    sizes = {detail[0] for ok, detail in results if ok}
    assert len(sizes) == 1, f"inconsistent body sizes (truncation/corruption?): {sizes}"
    return sizes


def test_endpoint_reachable():
    length, _digest, _elapsed = _fetch(URL)
    assert length > 0


def test_sequential_churn():
    """Back-to-back requests, one stream each: exercises open/close churn."""
    results = [(True, _fetch(URL)) for _ in range(20)]
    _assert_clean(results)


def test_concurrent_load():
    results = _run(URL, REQUESTS, CONCURRENCY)
    _assert_clean(results)


def test_sustained_load():
    """Keep every worker busy for DURATION seconds; no failures, no hangs, no drift."""
    deadline = time.time() + DURATION
    results: list = []

    def worker() -> list:
        local: list = []
        while time.time() < deadline:
            try:
                local.append((True, _fetch(URL)))
            except Exception as exc:  # noqa: BLE001
                local.append((False, f"{type(exc).__name__}: {exc}"))
        return local

    with cf.ThreadPoolExecutor(CONCURRENCY) as pool:
        for future in [pool.submit(worker) for _ in range(CONCURRENCY)]:
            results.extend(future.result())

    assert results, "sustained load produced no requests"
    _assert_clean(results)


@pytest.mark.skipif(
    not LARGE_URL,
    reason="set RDPFLUX_STABILITY_LARGE_URL to a multi-MB resource to test large transfers",
)
def test_large_transfer_integrity():
    """Concurrent large transfers must all return byte-identical data.

    A single interleaved/truncated stream would surface as a differing sha256
    or length, and a flow-control stall would surface as a timeout failure.
    """
    workers = min(4, CONCURRENCY)
    results = _run(LARGE_URL, workers, workers)
    _assert_clean(results)
    digests = {detail[1] for ok, detail in results if ok}
    assert len(digests) == 1, f"large transfers disagreed on content: {len(digests)} distinct hashes"
