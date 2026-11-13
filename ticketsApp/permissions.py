from __future__ import annotations

from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType

from .models import AgentProfile, Customer, Tag, Ticket, TicketComment

SUPPORT_ADMIN_GROUP = "Support Admin"
SUPPORT_AGENT_GROUP = "Support Agent"


def ensure_role_groups() -> tuple[Group, Group]:
    """
    Idempotently create or update the Support Admin and Support Agent Django Groups
    with explicit model-level permissions.
    """
    admin_group, _ = Group.objects.get_or_create(name=SUPPORT_ADMIN_GROUP)
    agent_group, _ = Group.objects.get_or_create(name=SUPPORT_AGENT_GROUP)

    ticket_ct = ContentType.objects.get_for_model(Ticket)
    comment_ct = ContentType.objects.get_for_model(TicketComment)
    customer_ct = ContentType.objects.get_for_model(Customer)
    tag_ct = ContentType.objects.get_for_model(Tag)
    agent_ct = ContentType.objects.get_for_model(AgentProfile)

    # Support Admin: full CRUD across all ticketsApp models
    admin_perms = Permission.objects.filter(
        content_type__in=[ticket_ct, comment_ct, customer_ct, tag_ct, agent_ct]
    )
    admin_group.permissions.set(admin_perms)

    # Support Agent: restricted operational permissions (no delete, no agent/tag management)
    agent_codenames = [
        (ticket_ct, "view_ticket"),
        (ticket_ct, "change_ticket"),
        (comment_ct, "view_ticketcomment"),
        (comment_ct, "add_ticketcomment"),
        (customer_ct, "view_customer"),
        (customer_ct, "add_customer"),
        (tag_ct, "view_tag"),
    ]
    agent_perms = []
    for ct, codename in agent_codenames:
        perm = Permission.objects.filter(content_type=ct, codename=codename).first()
        if perm:
            agent_perms.append(perm)
    agent_group.permissions.set(agent_perms)

    return admin_group, agent_group


def sync_user_role_permissions(profile: AgentProfile) -> None:
    """Sync Django staff flag and Group membership whenever an AgentProfile is saved."""
    admin_group, agent_group = ensure_role_groups()
    user = profile.user

    # Ensure staff access so the user can authenticate into /admin/
    if not user.is_staff:
        user.is_staff = True
        user.save(update_fields=["is_staff"])

    if profile.role == AgentProfile.Role.ADMIN:
        user.groups.remove(agent_group)
        user.groups.add(admin_group)
    else:
        user.groups.remove(admin_group)
        user.groups.add(agent_group)


def get_agent_profile(user) -> AgentProfile | None:
    if not user or not user.is_authenticated:
        return None
    return getattr(user, "agent_profile", None)


def is_support_admin(user) -> bool:
    if not user or not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    profile = get_agent_profile(user)
    return bool(profile and profile.role == AgentProfile.Role.ADMIN)


def is_support_agent(user) -> bool:
    if not user or not user.is_authenticated or user.is_superuser:
        return False
    profile = get_agent_profile(user)
    return bool(profile and profile.role == AgentProfile.Role.AGENT)