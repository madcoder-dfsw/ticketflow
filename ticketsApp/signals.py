from django.conf import settings
from django.db.models.signals import post_migrate, post_save
from django.dispatch import receiver

from .models import AgentProfile
from .permissions import ensure_role_groups, sync_user_role_permissions


@receiver(post_migrate)
def create_default_role_groups(sender, **kwargs):
    if sender.name == "ticketsApp":
        ensure_role_groups()


@receiver(post_save, sender=AgentProfile)
def on_agent_profile_saved(sender, instance: AgentProfile, **kwargs):
    sync_user_role_permissions(instance)


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def ensure_superuser_has_admin_profile(sender, instance, created: bool, **kwargs):
    """Automatically provision an ADMIN AgentProfile when a superuser is created."""
    if created and instance.is_superuser:
        AgentProfile.objects.get_or_create(
            user=instance,
            defaults={
                "role": AgentProfile.Role.ADMIN,
                "department": "Operations Leadership",
                "max_open_tickets": 50,
            },
        )