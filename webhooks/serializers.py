"""DRF serializers for HTTP wire shapes."""

from rest_framework import serializers


class WebhookResponseSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=["accepted", "already_received"])
    idempotency_key = serializers.CharField()
