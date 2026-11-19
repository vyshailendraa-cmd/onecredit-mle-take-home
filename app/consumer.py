"""Consume `company-events` and keep Postgres current.

Delivery guarantees, in order of importance:
- A message is acked only after its transaction commits, so a crash means redelivery, never a lost company.
- Redelivery is harmless: message_id is the primary key of company_events.
- Bad data is quarantined in rejected_messages and acked, so one bad message can't block the queue.
- If Postgres is unreachable the message is requeued and the consumer backs off, instead of quarantining good data.
"""

import asyncio
import contextlib
import logging
import signal

import aio_pika
import psycopg
from aio_pika.abc import AbstractIncomingMessage

from app import config, embedding
from app.store import Outcome, apply_message, connect, message_key, quarantine

log = logging.getLogger("consumer")


class Consumer:
    def __init__(self, settings: config.Settings):
        self.settings = settings
        self.embedder = embedding.get(settings.embedding_model)
        self.conn = connect(settings.database_url)
        self.counts = {outcome: 0 for outcome in Outcome}

    def _apply(self, message: AbstractIncomingMessage) -> Outcome:
        try:
            return apply_message(self.conn, self.embedder, message.message_id, message.body)
        except psycopg.OperationalError:
            # Connection lost: reconnect once so the next attempt has a chance, then let the caller requeue.
            self.conn.close()
            self.conn = connect(self.settings.database_url)
            raise

    async def handle(self, message: AbstractIncomingMessage) -> None:
        try:
            outcome = await asyncio.to_thread(self._apply, message)
        except psycopg.OperationalError:
            log.exception("database unavailable; requeueing %s", message.message_id)
            await asyncio.sleep(2)
            await message.nack(requeue=True)
            return
        except Exception as exc:
            # An unexpected bug, not bad data. Requeue once (it may be transient). If it fails again on
            # redelivery, quarantine it in Postgres and ack, so the queue keeps moving and nothing is dropped.
            log.exception("failed to apply %s (redelivered=%s)", message.message_id, message.redelivered)
            if not message.redelivered:
                await message.nack(requeue=True)
                return
            key = message_key(message.message_id, message.body)
            await asyncio.to_thread(quarantine, self.conn, key, message.body, f"processing error: {exc!r}")
            outcome = Outcome.REJECTED
        await message.ack()
        self.counts[outcome] += 1
        if sum(self.counts.values()) % 250 == 0:
            log.info("progress %s", dict(self.counts))

    async def run(self) -> None:
        connection = await aio_pika.connect_robust(self.settings.rabbitmq_url)
        async with connection:
            # One message at a time per consumer: the Postgres connection isn't shared between threads.
            channel = await connection.channel()
            await channel.set_qos(prefetch_count=1)
            # Same declaration as the producer: durable, no extra arguments.
            queue = await channel.declare_queue(self.settings.queue, durable=True)
            await queue.consume(self.handle)
            log.info("consuming %s with embedder %s", self.settings.queue, self.embedder.name)
            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                # Not supported on Windows; there Ctrl+C raises KeyboardInterrupt instead (handled in main).
                with contextlib.suppress(NotImplementedError):
                    loop.add_signal_handler(sig, stop.set)
            await stop.wait()
        log.info("stopped %s", dict(self.counts))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(Consumer(config.load()).run())


if __name__ == "__main__":
    main()
