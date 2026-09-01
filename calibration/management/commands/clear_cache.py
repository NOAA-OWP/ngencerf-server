import logging

from django.core.cache import cache
from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Flush the Django (Redis) cache. Safe to run on every startup since sessions are DB-backed."

    def handle(self, *args, **options):
        cache.clear()
        logger.info("Redis cache cleared.")
