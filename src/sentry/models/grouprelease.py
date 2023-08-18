from datetime import timedelta

from django.db import IntegrityError, models, router, transaction
from django.utils import timezone

from sentry.backup.scopes import RelocationScope
from sentry.db.models import (
    BoundedBigIntegerField,
    BoundedPositiveIntegerField,
    Model,
    region_silo_only_model,
    sane_repr,
)
from sentry.tasks.process_buffer import buffer_incr
from sentry.utils.cache import cache
from sentry.utils.hashlib import md5_text


@region_silo_only_model
class GroupRelease(Model):
    __include_in_export__ = False
    __relocation_scope__ = RelocationScope.Excluded

    project_id = BoundedBigIntegerField(db_index=True)
    group_id = BoundedBigIntegerField()
    # TODO: Should be BoundedBigIntegerField
    release_id = BoundedPositiveIntegerField(db_index=True)
    environment = models.CharField(max_length=64, default="")
    first_seen = models.DateTimeField(default=timezone.now)
    last_seen = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        app_label = "sentry"
        db_table = "sentry_grouprelease"
        unique_together = (("group_id", "release_id", "environment"),)
        index_together = (
            ("group_id", "first_seen"),
            ("group_id", "last_seen"),
        )

    __repr__ = sane_repr("group_id", "release_id")

    @classmethod
    def get_cache_key(cls, group_id, release_id, environment):
        return "grouprelease:1:{}:{}".format(
            group_id, md5_text(f"{release_id}:{environment}").hexdigest()
        )

    @classmethod
    def get_or_create(cls, group, release, environment, datetime, **kwargs):
        cache_key = cls.get_cache_key(group.id, release.id, environment.name)

        instance = cache.get(cache_key)
        if instance is None:
            try:
                with transaction.atomic(router.db_for_write(cls)):
                    instance, created = (
                        cls.objects.create(
                            release_id=release.id,
                            group_id=group.id,
                            environment=environment.name,
                            project_id=group.project_id,
                            first_seen=datetime,
                            last_seen=datetime,
                        ),
                        True,
                    )
            except IntegrityError:
                instance, created = (
                    cls.objects.get(
                        release_id=release.id, group_id=group.id, environment=environment.name
                    ),
                    False,
                )
        else:
            created = False

        if not created and instance.last_seen < datetime - timedelta(seconds=60):
            buffer_incr(
                model=cls,
                columns={},
                filters={"id": instance.id},
                extra={"last_seen": datetime},
            )
            instance.last_seen = datetime

        cache.set(cache_key, instance, 3600)
        return instance
