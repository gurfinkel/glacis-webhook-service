"""Sweeper-input queries — `list_stuck_received` and
`list_stuck_processing` are the data sources the sweep workflow fans
out from."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from webhooks import storage
from webhooks.models import WebhookEvent, WebhookStatus


def _create_with_age(idempotency_key: str, status: WebhookStatus, age_seconds: int) -> None:
    """Insert a row, then back-date `created_at` to simulate age. Direct
    .update() bypasses auto/db default and is the only way to do this in
    a single transaction without freezing the wall clock."""
    WebhookEvent.objects.create(
        idempotency_key=idempotency_key,
        raw_payload={"k": idempotency_key},
        status=status,
    )
    WebhookEvent.objects.filter(idempotency_key=idempotency_key).update(
        created_at=datetime.now(UTC) - timedelta(seconds=age_seconds),
    )


@pytest.mark.django_db
class TestListStuckReceived:
    def test_returns_only_rows_older_than_cutoff(self):
        _create_with_age("old", WebhookStatus.RECEIVED, age_seconds=600)
        _create_with_age("fresh", WebhookStatus.RECEIVED, age_seconds=60)

        rows = storage.list_stuck_received(older_than_seconds=300)

        keys = [r["idempotency_key"] for r in rows]
        assert keys == ["old"]

    def test_ignores_other_statuses(self):
        _create_with_age("recv", WebhookStatus.RECEIVED, age_seconds=600)
        _create_with_age("proc", WebhookStatus.PROCESSING, age_seconds=600)
        _create_with_age("done", WebhookStatus.COMPLETED, age_seconds=600)
        _create_with_age("fail", WebhookStatus.FAILED, age_seconds=600)

        rows = storage.list_stuck_received(older_than_seconds=300)

        assert [r["idempotency_key"] for r in rows] == ["recv"]


@pytest.mark.django_db
class TestListStuckProcessing:
    """PROCESSING rows older than the (longer) grace must be returned —
    this is what the sweeper consumes to recover workflows that died
    between `mark_processing` and `mark_failed`."""

    def test_returns_processing_rows_older_than_cutoff(self):
        _create_with_age("stuck", WebhookStatus.PROCESSING, age_seconds=1200)
        _create_with_age("live", WebhookStatus.PROCESSING, age_seconds=300)

        rows = storage.list_stuck_processing(older_than_seconds=900)

        assert [r["idempotency_key"] for r in rows] == ["stuck"]

    def test_does_not_return_received_rows(self):
        """The two queries are deliberately separate: RECEIVED has a short
        grace (no live workflow to race), PROCESSING has a long grace
        (must exceed run_timeout). Conflating them would re-issue PROCESSING
        rows too eagerly, racing live workflows."""
        _create_with_age("recv-old", WebhookStatus.RECEIVED, age_seconds=1200)
        _create_with_age("proc-old", WebhookStatus.PROCESSING, age_seconds=1200)

        rows = storage.list_stuck_processing(older_than_seconds=900)

        assert [r["idempotency_key"] for r in rows] == ["proc-old"]

    def test_does_not_return_terminal_rows(self):
        _create_with_age("done", WebhookStatus.COMPLETED, age_seconds=1200)
        _create_with_age("fail", WebhookStatus.FAILED, age_seconds=1200)

        rows = storage.list_stuck_processing(older_than_seconds=900)

        assert rows == []

    def test_respects_limit(self):
        for i in range(5):
            _create_with_age(f"p{i}", WebhookStatus.PROCESSING, age_seconds=1200)

        rows = storage.list_stuck_processing(older_than_seconds=900, limit=3)

        assert len(rows) == 3
