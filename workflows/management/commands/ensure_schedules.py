"""Manual schedule registration — `python manage.py ensure_schedules`.

The worker bootstrap calls `ensure_sweep_schedule` automatically. This
command exists for ops scenarios: fresh Temporal namespace, manual
re-registration after a wipe, etc.
"""

import asyncio

from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Register (or update) Temporal Schedules — idempotent."

    def handle(self, *args, **options):
        from workflows.worker import _validate_processing_grace
        _validate_processing_grace(settings.SWEEPER_PROCESSING_GRACE_SECONDS)

        async def _run():
            from temporalio.client import Client

            from workflows.schedules import ensure_sweep_schedule

            client = await Client.connect(
                settings.TEMPORAL_ADDRESS,
                namespace=settings.TEMPORAL_NAMESPACE,
            )
            await ensure_sweep_schedule(
                client,
                task_queue=settings.TEMPORAL_TASK_QUEUE,
                interval_seconds=settings.SWEEPER_INTERVAL_SECONDS,
                received_grace_seconds=settings.SWEEPER_GRACE_SECONDS,
                processing_grace_seconds=settings.SWEEPER_PROCESSING_GRACE_SECONDS,
                limit=settings.SWEEPER_BATCH_LIMIT,
            )

        asyncio.run(_run())
        self.stdout.write(self.style.SUCCESS("Sweep schedule registered"))
