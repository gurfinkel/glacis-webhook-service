"""`python manage.py run_worker` — entrypoint for the Temporal worker process."""

import asyncio

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Start the Temporal worker (workflows + activities)."

    def handle(self, *args, **options):
        from workflows.worker import run_worker

        asyncio.run(run_worker())
