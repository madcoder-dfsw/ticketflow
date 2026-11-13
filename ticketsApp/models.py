import uuid
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from .validators import ticket_attachment_upload_path, validate_attachment_file


class Customer(models.Model):
    class Tier(models.TextChoices):
        FREE = "FREE", "Free"
        PRO = "PRO", "Pro"
        ENTERPRISE = "ENTERPRISE", "Enterprise"

    full_name = models.CharField(
        max_length=150,
        help_text="Primary contact's full name.",
    )
    email = models.EmailField(
        unique=True,
        help_text="Unique customer email address used for ticket correspondence.",
    )
    company = models.CharField(
        max_length=150,
        blank=True,
        help_text="Organization or account name (optional for individual users).",
    )
    tier = models.CharField(
        max_length=20,
        choices=Tier.choices,
        default=Tier.FREE,
        db_index=True,
        help_text="Subscription tier determining support priority and SLA expectations.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["full_name"]

    def __str__(self) -> str:
        return f"{self.full_name} ({self.company or self.email})"


class AgentProfile(models.Model):
    class Role(models.TextChoices):
        ADMIN = "ADMIN", "Support Admin"
        AGENT = "AGENT", "Support Agent"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="agent_profile",
        help_text="Linked Django authentication user account.",
    )
    role = models.CharField(
        max_length=20,
        choices=Role.choices,
        default=Role.AGENT,
        db_index=True,
        help_text="Determines admin panel permissions: Support Admins manage all tickets and config; Support Agents handle assigned queues.",
    )
    department = models.CharField(
        max_length=100,
        default="General Support",
        help_text="Operational queue or specialty team (e.g., Billing, Technical, Trust & Safety).",
    )
    max_open_tickets = models.PositiveSmallIntegerField(
        default=15,
        help_text="Maximum concurrent active tickets (Open, In Progress, Waiting) allowed for this agent.",
    )
    is_available = models.BooleanField(
        default=True,
        help_text="Uncheck when the agent is on leave or unavailable for new ticket assignments.",
    )

    class Meta:
        ordering = ["user__username"]

    def __str__(self) -> str:
        return f"{self.user.get_full_name() or self.user.username} [{self.get_role_display()}]"

    @property
    def active_ticket_count(self) -> int:
        return self.assigned_tickets.exclude(
            status__in=[Ticket.Status.RESOLVED, Ticket.Status.CLOSED]
        ).count()


class Tag(models.Model):
    name = models.CharField(
        max_length=50,
        unique=True,
        help_text="Short categorization label (e.g., billing, bug, outage, vip).",
    )
    color = models.CharField(
        max_length=7,
        default="#2563EB",
        help_text="Hex badge color displayed in the admin list view (e.g., #DC2626).",
    )

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class Ticket(models.Model):
    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        IN_PROGRESS = "IN_PROGRESS", "In Progress"
        WAITING_CUSTOMER = "WAITING_CUSTOMER", "Waiting on Customer"
        RESOLVED = "RESOLVED", "Resolved"
        CLOSED = "CLOSED", "Closed"

    class Priority(models.TextChoices):
        LOW = "LOW", "Low (72h SLA)"
        MEDIUM = "MEDIUM", "Medium (24h SLA)"
        HIGH = "HIGH", "High (12h SLA)"
        URGENT = "URGENT", "Urgent (4h SLA)"

    ALLOWED_TRANSITIONS: dict[str, set[str]] = {
        Status.OPEN: {Status.IN_PROGRESS, Status.WAITING_CUSTOMER, Status.RESOLVED},
        Status.IN_PROGRESS: {Status.WAITING_CUSTOMER, Status.RESOLVED, Status.OPEN},
        Status.WAITING_CUSTOMER: {Status.IN_PROGRESS, Status.RESOLVED},
        Status.RESOLVED: {Status.CLOSED, Status.IN_PROGRESS},
        Status.CLOSED: {Status.IN_PROGRESS},
    }

    SLA_HOURS: dict[str, int] = {
        Priority.URGENT: 4,
        Priority.HIGH: 12,
        Priority.MEDIUM: 24,
        Priority.LOW: 72,
    }

    reference_code = models.CharField(
        max_length=16,
        unique=True,
        editable=False,
        help_text="Auto-generated unique tracking identifier (e.g., TCK-8F3A21).",
    )
    title = models.CharField(
        max_length=200,
        help_text="Concise summary of the customer's issue or request.",
    )
    description = models.TextField(
        help_text="Detailed explanation, reproduction steps, or error logs provided by the customer.",
    )
    customer = models.ForeignKey(
        Customer,
        on_delete=models.PROTECT,
        related_name="tickets",
        help_text="Customer who submitted this support request.",
    )
    assigned_agent = models.ForeignKey(
        AgentProfile,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_tickets",
        help_text="Support agent responsible for resolving this ticket. Required before moving to In Progress or Resolved.",
    )
    tags = models.ManyToManyField(
        Tag,
        blank=True,
        related_name="tickets",
        help_text="Optional topic or triage labels for filtering and reporting.",
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.OPEN,
        db_index=True,
        help_text="Current lifecycle stage. Tickets must be Resolved before they can be Closed.",
    )
    priority = models.CharField(
        max_length=15,
        choices=Priority.choices,
        default=Priority.MEDIUM,
        db_index=True,
        help_text="Urgency level governing the target SLA resolution window.",
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    resolved_at = models.DateTimeField(
        null=True,
        blank=True,
        editable=False,
        help_text="Timestamp automatically recorded when the ticket transitions to Resolved.",
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "priority"]),
        ]

    def __str__(self) -> str:
        return f"[{self.reference_code}] {self.title}"

    @property
    def sla_deadline(self):
        base_time = self.created_at or timezone.now()
        hours = self.SLA_HOURS.get(self.priority, 24)
        return base_time + timedelta(hours=hours)

    @property
    def is_sla_breached(self) -> bool:
        if self.status in {self.Status.RESOLVED, self.Status.CLOSED}:
            if self.resolved_at:
                return self.resolved_at > self.sla_deadline
            return False
        return timezone.now() > self.sla_deadline

    def clean(self) -> None:
        errors = {}

        # Rule 1: Working statuses require an assigned agent
        if self.status != self.Status.OPEN and not self.assigned_agent_id:
            errors["assigned_agent"] = (
                f"An agent must be assigned before setting status to '{self.get_status_display()}'."
            )

        # Inspect persisted state for updates
        if self.pk:
            previous = (
                Ticket.objects.filter(pk=self.pk)
                .values("status", "assigned_agent_id")
                .first()
            )
            if previous:
                old_status = previous["status"]
                if old_status != self.status:
                    allowed = self.ALLOWED_TRANSITIONS.get(old_status, set())
                    if self.status not in allowed:
                        errors["status"] = (
                            f"Invalid status transition from '{old_status}' to '{self.status}'. "
                            f"Allowed next states: {', '.join(sorted(allowed)) or 'none'}."
                        )

                # Rule 2: Enforce agent active ticket capacity on new assignment
                if (
                    self.assigned_agent_id
                    and self.assigned_agent_id != previous["assigned_agent_id"]
                    and self.status not in {self.Status.RESOLVED, self.Status.CLOSED}
                ):
                    agent = self.assigned_agent
                    if agent.active_ticket_count >= agent.max_open_tickets:
                        errors["assigned_agent"] = (
                            f"{agent} has reached their active ticket limit ({agent.max_open_tickets})."
                        )
        else:
            # Rule 3: New tickets cannot start directly as Closed
            if self.status == self.Status.CLOSED:
                errors["status"] = "New tickets cannot be created directly in 'Closed' status."

            if self.assigned_agent_id and self.status not in {self.Status.RESOLVED, self.Status.CLOSED}:
                agent = self.assigned_agent
                if agent.active_ticket_count >= agent.max_open_tickets:
                    errors["assigned_agent"] = (
                        f"{agent} has reached their active ticket limit ({agent.max_open_tickets})."
                    )

        if errors:
            raise ValidationError(errors)

    def transition_to(self, new_status: str, save: bool = True) -> None:
        """Explicit domain method for validated status transitions."""
        self.status = new_status
        self.full_clean()
        if save:
            self.save()

    def save(self, *args, **kwargs) -> None:
        if not self.reference_code:
            self.reference_code = f"TCK-{uuid.uuid4().hex[:6].upper()}"

        if self.status == self.Status.RESOLVED and not self.resolved_at:
            self.resolved_at = timezone.now()
        elif self.status in {self.Status.OPEN, self.Status.IN_PROGRESS, self.Status.WAITING_CUSTOMER}:
            self.resolved_at = None

        self.full_clean()
        super().save(*args, **kwargs)


class TicketComment(models.Model):
    ticket = models.ForeignKey(
        Ticket,
        on_delete=models.CASCADE,
        related_name="comments",
        help_text="Parent support ticket this reply or internal note belongs to.",
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="ticket_comments",
        help_text="Staff user who wrote this comment.",
    )
    body = models.TextField(
        help_text="Message body sent to the customer or logged as an internal investigation note.",
    )
    is_internal_note = models.BooleanField(
        default=False,
        help_text="If checked, this note is visible only to internal support staff and hidden from customer exports.",
    )
    attachment = models.FileField(
        upload_to=ticket_attachment_upload_path,
        validators=[validate_attachment_file],
        null=True,
        blank=True,
        help_text="Optional screenshot, log file, or PDF (max 5 MB; allowed: .pdf, .png, .jpg, .jpeg, .txt, .log, .csv).",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self) -> str:
        kind = "Internal Note" if self.is_internal_note else "Reply"
        return f"{kind} by {self.author} on {self.ticket.reference_code}"