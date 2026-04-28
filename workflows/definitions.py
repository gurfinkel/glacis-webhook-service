"""Temporal workflows.

Workflows are deterministic: no I/O, no clock reads, no random. Side
effects go through activities, referenced by name string so the
workflow module stays free of httpx, Django ORM, etc.

The workflow_id is the webhook's `idempotency_key`, so Temporal's
`WorkflowAlreadyStarted` error gives us a third dedup layer (after
Redis and the DB unique constraint).
"""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from django.conf import settings


# Bounds the worst case for end-to-end processing of one payload. The
# workflow fails beyond this — preventing pathological retry storms or
# stuck activities from holding the workflow open forever.
WORKFLOW_RUN_TIMEOUT = timedelta(minutes=10)

# Temporal identifies activity failures by the raised exception's class
# name. Listing both the bare class name and the fully qualified path
# covers the wire formats Temporal uses.
NON_RETRYABLE_LLM_ERRORS = ("LLMPermanentError", "workflows.llm_client.LLMPermanentError")

_QUICK_RETRY = RetryPolicy(maximum_attempts=3)
_LLM_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    maximum_interval=timedelta(seconds=30),
    backoff_coefficient=2.0,
    maximum_attempts=settings.MAX_RETRIES,
    non_retryable_error_types=list(NON_RETRYABLE_LLM_ERRORS),
)


def _should_verify(classification: dict) -> bool:
    """Skip verification for schema-rejected extractions (already routed
    to review) and UNCLASSIFIED (no extracted fields to verify)."""
    return (
        classification.get("kind") == "ok"
        and classification.get("result", {}).get("event_type") != "unclassified"
    )


@workflow.defn(name="ClassifyWebhookWorkflow")
class ClassifyWebhookWorkflow:
    """Classify and persist one webhook payload."""

    @workflow.run
    async def run(self, idempotency_key: str, event_id: str, payload: dict) -> None:
        try:
            await workflow.execute_activity(
                "mark_processing",
                args=[idempotency_key],
                start_to_close_timeout=timedelta(seconds=10),
                retry_policy=_QUICK_RETRY,
            )

            classification = await workflow.execute_activity(
                "classify_payload",
                args=[payload],
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=_LLM_RETRY,
            )

            verification: dict | None = None
            if _should_verify(classification):
                verification = await workflow.execute_activity(
                    "verify_extraction",
                    args=[payload, classification],
                    start_to_close_timeout=timedelta(seconds=60),
                    retry_policy=_LLM_RETRY,
                )

            await workflow.execute_activity(
                "persist_classification",
                args=[idempotency_key, event_id, classification, verification],
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=_QUICK_RETRY,
            )
        except ActivityError as e:
            cause = e.cause
            error_message = str(cause) if isinstance(cause, ApplicationError) else str(e)
            await workflow.execute_activity(
                "mark_failed",
                args=[idempotency_key, error_message],
                start_to_close_timeout=timedelta(seconds=10),
                retry_policy=_QUICK_RETRY,
            )
            raise


@workflow.defn(name="SweepStuckEventsWorkflow")
class SweepStuckEventsWorkflow:
    """Recovers durably-persisted events whose workflow run never reached
    a terminal state. Two stuck states, two graces:

    - **RECEIVED**: row inserted but `start_workflow` failed.
      `received_grace_seconds` is short — no live workflow to race.
    - **PROCESSING**: workflow started, then died before reaching a
      terminal state (typically a run-timeout during a long LLM retry
      loop; workflow code does NOT regain control on run-timeout, so
      the `except ActivityError → mark_failed` branch never executes).
      `processing_grace_seconds` MUST exceed WORKFLOW_RUN_TIMEOUT plus
      a buffer or the sweeper races live workflows. Invariant enforced
      at worker startup.
    """

    @workflow.run
    async def run(
        self,
        received_grace_seconds: int = 300,
        processing_grace_seconds: int = 900,
        limit: int = 100,
    ) -> int:
        stuck: list[dict] = []
        stuck.extend(await workflow.execute_activity(
            "list_stuck_received",
            args=[received_grace_seconds, limit],
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_QUICK_RETRY,
        ))
        stuck.extend(await workflow.execute_activity(
            "list_stuck_processing",
            args=[processing_grace_seconds, limit],
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_QUICK_RETRY,
        ))
        for row in stuck:
            await workflow.execute_activity(
                "reissue_classify_workflow",
                args=[row["idempotency_key"], row["id"], row["raw_payload"]],
                start_to_close_timeout=timedelta(seconds=10),
                retry_policy=_QUICK_RETRY,
            )
        return len(stuck)
