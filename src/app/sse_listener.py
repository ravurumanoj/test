"""SSE-to-webhook listener.

Holds a long-lived Server-Sent Events connection to the upstream backend and
forwards every received event to a local webhook endpoint with bounded
concurrency. Reconnects automatically on errors and proactively before the
upstream idle timeout.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator

import httpx

from app.settings import Settings

logger = logging.getLogger(__name__)
settings = Settings.from_env()

# Break and reconnect after this many seconds without any chunk, to stay
# below the upstream/proxy 60s idle timeout.
IDLE_RECONNECT_SECONDS = 50
RETRY_DELAY_SECONDS = 5
STREAM_TIMEOUT_SECONDS = 300.0
# Raised so the webhook POST doesn't time out before a large, multi-iteration
# orchestrator run (LLM calls can now take up to UNIQUE_LLM_TIMEOUT_SECONDS
# each) finishes and replies.
WEBHOOK_TIMEOUT_SECONDS = 1900.0


def _parse_sse_event(event_data: str) -> dict | None:
    """Parse a single raw SSE event block into a dict."""
    event: dict[str, str] = {}
    for line in event_data.split("\n"):
        if ": " in line:
            key, value = line.split(": ", 1)
            event[key] = value

    if "data" not in event:
        return None

    try:
        return json.loads(event["data"])
    except json.JSONDecodeError as exc:
        logger.error("JSON parsing error: %s", exc)
        return None


def _process_buffer(buffer: str) -> tuple[list[dict], str]:
    """Extract complete SSE events from the buffer.

    Returns the parsed events and the remaining (incomplete) buffer.
    """
    events: list[dict] = []
    while "\n\n" in buffer:
        event_data, buffer = buffer.split("\n\n", 1)
        if event_data.strip():
            parsed = _parse_sse_event(event_data)
            if parsed is not None:
                events.append(parsed)
    return events, buffer


def _build_stream_url() -> str:
    """Build the upstream SSE stream URL from settings."""
    subscriptions = ",".join(settings.subscriptions)
    return (
        f"{settings.sse_api_base}/public/event-socket/events/stream"
        f"?subscriptions={subscriptions}"
    )


def _build_headers() -> dict[str, str]:
    """Build the auth and streaming headers for the upstream connection."""
    return {
        "Authorization": f"Bearer {settings.sse_api_key}",
        "x-app-id": settings.sse_app_id,
        "x-company-id": settings.sse_company_id,
        "Connection": "keep-alive",
        "Accept": "text/event-stream",
    }


async def get_sse_stream() -> AsyncIterator[dict]:
    """Yield parsed SSE events, reconnecting automatically."""
    url = _build_stream_url()
    headers = _build_headers()

    while True:  # Infinite loop for automatic reconnection
        try:
            async with httpx.AsyncClient(
                timeout=STREAM_TIMEOUT_SECONDS,
                trust_env=False,
                verify=settings.sse_ca_bundle or True,
            ) as client:  # nosec B501
                last_activity = time.time()

                async with client.stream("GET", url, headers=headers) as response:
                    response.raise_for_status()
                    logger.info("SSE stream connected: %s", url)
                    buffer = ""

                    async for chunk in response.aiter_bytes():
                        last_activity = time.time()  # Reset timer on each chunk
                        buffer += chunk.decode("utf-8")

                        events, buffer = _process_buffer(buffer)
                        for event in events:
                            yield event

                        # Reconnect proactively to avoid the upstream timeout
                        if time.time() - last_activity > IDLE_RECONNECT_SECONDS:
                            logger.info("Proactive reconnection to avoid timeout")
                            break

        except asyncio.CancelledError:
            logger.info("SSE stream cancelled")
            raise
        except Exception as exc:  # noqa: BLE001 - listener must never die
            logger.warning(
                "Connection error: %s. Retrying in %ss...", exc, RETRY_DELAY_SECONDS
            )
            await asyncio.sleep(RETRY_DELAY_SECONDS)


async def process_event(
    event_data: dict,
    client: httpx.AsyncClient,
    webhook_url: str,
    semaphore: asyncio.Semaphore,
) -> None:
    """Forward a single event to the local webhook."""
    async with semaphore:
        try:
            logger.debug("New event received: %s", event_data)
            response = await client.post(
                webhook_url,
                json=event_data,
                headers={"Content-Type": "application/json"},
                timeout=WEBHOOK_TIMEOUT_SECONDS,
            )
            if response.status_code != 200:
                logger.error(
                    "Webhook error: %s - %s", response.status_code, response.text
                )
            else:
                logger.debug("Webhook response: %s", response.text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one bad event must not stop the loop
            logger.error("Error sending to webhook: %s", exc)


async def start_sse_listener(webhook_url: str, max_concurrent_tasks: int = 10) -> None:
    """Listen to SSE events and forward them to the webhook."""
    logger.info(
        "Starting SSE listener for %s -> %s",
        settings.subscriptions,
        webhook_url,
    )

    background_tasks: set[asyncio.Task] = set()
    semaphore = asyncio.Semaphore(max_concurrent_tasks)

    # Single shared client for the whole listener lifetime. Creating it per
    # event would close it while the spawned task is still using it.
    async with httpx.AsyncClient(
        timeout=WEBHOOK_TIMEOUT_SECONDS, trust_env=False
    ) as webhook_client:
        try:
            async for event_data in get_sse_stream():
                task = asyncio.create_task(
                    process_event(event_data, webhook_client, webhook_url, semaphore)
                )
                background_tasks.add(task)
                task.add_done_callback(background_tasks.discard)
        except asyncio.CancelledError:
            logger.info("SSE listener cancelled, draining in-flight tasks")
            for task in list(background_tasks):
                task.cancel()
            await asyncio.gather(*background_tasks, return_exceptions=True)
            raise
