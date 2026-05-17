from __future__ import annotations

import argparse
import asyncio
import time
from collections import Counter
from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class LoadTestConfig:
    url: str
    host: str
    requests: int
    concurrency: int
    timeout_seconds: float


async def _fetch(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    config: LoadTestConfig,
) -> str:
    async with semaphore:
        try:
            response = await client.get(
                config.url,
                headers={
                    "Host": config.host,
                    "Connection": "close",
                },
            )
            await response.aread()
            return f"status:{response.status_code}"
        except Exception as exc:
            return type(exc).__name__


async def run_load_test(config: LoadTestConfig) -> Counter[str]:
    semaphore = asyncio.Semaphore(config.concurrency)
    limits = httpx.Limits(
        max_connections=config.concurrency,
        max_keepalive_connections=0,
    )
    timeout = httpx.Timeout(config.timeout_seconds)

    async with httpx.AsyncClient(limits=limits, timeout=timeout) as client:
        results = await asyncio.gather(
            *(_fetch(client, semaphore, config) for _ in range(config.requests))
        )
    return Counter(results)


def _parse_args() -> LoadTestConfig:
    parser = argparse.ArgumentParser(description="Load test an AWS-Light proxied service.")
    parser.add_argument("--url", default="http://localhost:8080/", help="Proxy URL to call.")
    parser.add_argument(
        "--host",
        default="hello-service.localhost",
        help="Host header used by the proxy to select the service.",
    )
    parser.add_argument(
        "-n",
        "--requests",
        type=int,
        default=1000,
        help="Total number of requests to send.",
    )
    parser.add_argument(
        "-c",
        "--concurrency",
        type=int,
        default=100,
        help="Maximum number of concurrent requests.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="Per-request timeout in seconds.",
    )
    args = parser.parse_args()

    if args.requests < 1:
        parser.error("--requests must be at least 1")
    if args.concurrency < 1:
        parser.error("--concurrency must be at least 1")

    return LoadTestConfig(
        url=args.url,
        host=args.host,
        requests=args.requests,
        concurrency=args.concurrency,
        timeout_seconds=args.timeout,
    )


def main() -> None:
    config = _parse_args()
    start = time.perf_counter()
    results = asyncio.run(run_load_test(config))
    duration = time.perf_counter() - start

    success_count = sum(
        count for result, count in results.items() if result.startswith("status:2")
    )
    print(f"URL: {config.url}")
    print(f"Host: {config.host}")
    print(f"Requests: {config.requests}")
    print(f"Concurrency: {config.concurrency}")
    print(f"Success: {success_count}")
    print(f"Time: {duration:.2f}s")
    print(f"RPS: {config.requests / duration:.2f}")
    print("Results:")
    for result, count in results.most_common():
        print(f"  {result}: {count}")


if __name__ == "__main__":
    main()
