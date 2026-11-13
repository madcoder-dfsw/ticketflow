from django.contrib import admin, messages
from django.core.exceptions import ValidationError
from django.db.models import Count, Q
from django.utils.html import format_html

from .models import AgentProfile, Customer, Tag, Ticket, TicketComment
from .permissions import get_agent_profile, is_support_admin, is_support_agent


class TicketCommentInline(admin.TabularInline):
    model = TicketComment
    extra = 1
    fields = ("author", "body", "is_internal_note", "attachment", "created_at")
    readonly_fields = ("author", "created_at")
    ordering = ("created_at",)

    def has_change_permission(self, request, obj=None) -> bool:
        # Comments are append-only audit trail entries for non-admins
        return is_support_admin(request.user)

    def has_delete_permission(self, request, obj=None) -> bool:
        return is_support_admin(request.user)


@admin.register(Ticket)
class TicketAdmin(admin.ModelAdmin):
    list_display = (
        "reference_code",
        "title",
        "customer_link",
        "status_badge",
        "priority_badge",
        "sla_status",
        "assigned_agent",
        "tag_list",
        "comment_total",
        "created_at",
    )
    list_filter = ("status", "priority", "customer__tier", "tags", "assigned_agent")
    search_fields = (
        "reference_code",
        "title",
        "description",
        "customer__full_name",
        "customer__email",
        "customer__company",
        "comments__body",
    )
    autocomplete_fields = ("customer", "assigned_agent")
    filter_horizontal = ("tags",)
    date_hierarchy = "created_at"
    list_per_page = 30
    inlines = [TicketCommentInline]
    actions = ["assign_to_me", "mark_resolved", "escalate_to_urgent"]

    fieldsets = (
        (
            "Ticket Overview",
            {
                "fields": ("reference_code", "title", "customer", "description"),
            },
        ),
        (
            "Triage & Workflow",
            {
                "fields": ("status", "priority", "assigned_agent", "tags"),
            },
        ),
        (
            "Timestamps & SLA",
            {
                "fields": ("created_at", "updated_at", "resolved_at"),
                "classes": ("collapse",),
            },
        ),
    )

    def get_queryset(self, request):
        qs = (
            super()
            .get_queryset(request)
            .select_related("customer", "assigned_agent", "assigned_agent__user")
            .prefetch_related("tags")
            .annotate(_comment_count=Count("comments", distinct=True))
        )
        if is_support_admin(request.user):
            return qs

        profile = get_agent_profile(request.user)
        if not profile:
            return qs.none()

        # Support Agents only see tickets assigned to them or unassigned OPEN tickets
        return qs.filter(
            Q(assigned_agent=profile)
            | Q(assigned_agent__isnull=True, status=Ticket.Status.OPEN)
        )

    def get_readonly_fields(self, request, obj=None):
        base_readonly = ["reference_code", "created_at", "updated_at", "resolved_at"]
        if is_support_agent(request.user):
            # Agents can update status & description notes, but cannot reassign or alter triage fields
            return base_readonly + ["customer", "assigned_agent", "priority", "tags"]
        return base_readonly

    def get_actions(self, request):
        actions = super().get_actions(request)
        if not is_support_admin(request.user):
            actions.pop("escalate_to_urgent", None)
            actions.pop("delete_selected", None)
        return actions

    def save_formset(self, request, form, formset, change) -> None:
        instances = formset.save(commit=False)
        for instance in instances:
            if isinstance(instance, TicketComment) and not instance.author_id:
                instance.author = request.user
            instance.save()
        for deleted_obj in formset.deleted_objects:
            deleted_obj.delete()
        formset.save_m2m()

    # --- Custom Display Columns ---

    @admin.display(description="Customer", ordering="customer__full_name")
    def customer_link(self, obj: Ticket) -> str:
        return f"{obj.customer.full_name} [{obj.customer.get_tier_display()}]"

    @admin.display(description="Status", ordering="status")
    def status_badge(self, obj: Ticket) -> str:
        colors = {
            Ticket.Status.OPEN: ("#DBEAFE", "#1E40AF"),
            Ticket.Status.IN_PROGRESS: ("#FEF3C7", "#92400E"),
            Ticket.Status.WAITING_CUSTOMER: ("#F3E8FF", "#6B21A8"),
            Ticket.Status.RESOLVED: ("#DCFCE7", "#166534"),
            Ticket.Status.CLOSED: ("#F3F4F6", "#374151"),
        }
        bg, fg = colors.get(obj.status, ("#F3F4F6", "#111827"))
        return format_html(
            '<span style="background:{};color:{};padding:3px 8px;border-radius:999px;font-weight:600;font-size:11px;">{}</span>',
            bg,
            fg,
            obj.get_status_display(),
        )

    @admin.display(description="Priority", ordering="priority")
    def priority_badge(self, obj: Ticket) -> str:
        colors = {
            Ticket.Priority.URGENT: "#DC2626",
            Ticket.Priority.HIGH: "#D97706",
            Ticket.Priority.MEDIUM: "#2563EB",
            Ticket.Priority.LOW: "#6B7280",
        }
        color = colors.get(obj.priority, "#374151")
        return format_html(
            '<strong style="color:{};">{}</strong>',
            color,
            obj.priority,
        )

    @admin.display(description="SLA")
    def sla_status(self, obj: Ticket) -> str:
        if obj.is_sla_breached:
            return format_html(
                '<span style="color:#DC2626;font-weight:700;">BREACHED</span>'
            )
        if obj.status in {Ticket.Status.RESOLVED, Ticket.Status.CLOSED}:
            return format_html('<span style="color:#16A34A;">Met</span>')
        return format_html('<span style="color:#059669;">On Track</span>')

    @admin.display(description="Tags")
    def tag_list(self, obj: Ticket) -> str:
        tags = list(obj.tags.all())
        if not tags:
            return "—"
        pills = [
            format_html(
                '<span style="background:{};color:#fff;padding:2px 6px;border-radius:4px;font-size:11px;margin-right:3px;">{}</span>',
                t.color,
                t.name,
            )
            for t in tags
        ]
        return format_html("".join(str(p) for p in pills))

    @admin.display(description="Comments", ordering="_comment_count")
    def comment_total(self, obj: Ticket) -> int:
        return getattr(obj, "_comment_count", obj.comments.count())

    # --- Custom Bulk Actions ---

    @admin.action(description="Assign selected tickets to me")
    def assign_to_me(self, request, queryset) -> None:
        profile = get_agent_profile(request.user)
        if not profile:
            self.message_user(
                request,
                "Your user account does not have an AgentProfile configured.",
                level=messages.ERROR,
            )
            return

        updated = 0
        skipped = []
        for ticket in queryset:
            try:
                ticket.assigned_agent = profile
                if ticket.status == Ticket.Status.OPEN:
                    ticket.status = Ticket.Status.IN_PROGRESS
                ticket.save()
                updated += 1
            except ValidationError as exc:
                skipped.append(f"{ticket.reference_code}: {'; '.join(exc.messages)}")

        if updated:
            self.message_user(
                request,
                f"Assigned {updated} ticket(s) to {profile}.",
                level=messages.SUCCESS,
            )
        if skipped:
            self.message_user(
                request,
                f"Skipped {len(skipped)} ticket(s): {' | '.join(skipped[:3])}",
                level=messages.WARNING,
            )

    @admin.action(description="Mark selected tickets as Resolved")
    def mark_resolved(self, request, queryset) -> None:
        resolved_count = 0
        failed = []
        for ticket in queryset:
            try:
                ticket.transition_to(Ticket.Status.RESOLVED)
                resolved_count += 1
            except ValidationError as exc:
                failed.append(f"{ticket.reference_code}: {'; '.join(exc.messages)}")

        if resolved_count:
            self.message_user(
                request,
                f"Marked {resolved_count} ticket(s) as Resolved.",
                level=messages.SUCCESS,
            )
        if failed:
            self.message_user(
                request,
                f"Could not resolve {len(failed)} ticket(s): {' | '.join(failed[:3])}",
                level=messages.WARNING,
            )

    @admin.action(description="Escalate priority to Urgent (Admins only)")
    def escalate_to_urgent(self, request, queryset) -> None:
        if not is_support_admin(request.user):
            self.message_user(
                request,
                "Only Support Admins can bulk-escalate ticket priority.",
                level=messages.ERROR,
            )
            return
        count = queryset.exclude(status__in=[Ticket.Status.RESOLVED, Ticket.Status.CLOSED]).update(
            priority=Ticket.Priority.URGENT
        )
        self.message_user(
            request,
            f"Escalated {count} active ticket(s) to Urgent priority.",
            level=messages.SUCCESS,
        )


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ("full_name", "email", "company", "tier", "ticket_count", "created_at")
    list_filter = ("tier", "created_at")
    search_fields = ("full_name", "email", "company")
    readonly_fields = ("created_at",)

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(_ticket_count=Count("tickets"))

    @admin.display(description="Tickets", ordering="_ticket_count")
    def ticket_count(self, obj: Customer) -> int:
        return obj._ticket_count


@admin.register(AgentProfile)
class AgentProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "role", "department", "workload_display", "is_available")
    list_filter = ("role", "department", "is_available")
    search_fields = ("user__username", "user__first_name", "user__last_name", "user__email", "department")

    def has_module_permission(self, request) -> bool:
        # Completely hide Agent management from Support Agents
        return is_support_admin(request.user)

    @admin.display(description="Active Load / Cap")
    def workload_display(self, obj: AgentProfile) -> str:
        active = obj.active_ticket_count
        color = "#DC2626" if active >= obj.max_open_tickets else "#16A34A"
        return format_html(
            '<strong style="color:{};">{} / {}</strong>',
            color,
            active,
            obj.max_open_tickets,
        )


@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):
    list_display = ("name", "color_preview")
    search_fields = ("name",)

    @admin.display(description="Badge Preview")
    def color_preview(self, obj: Tag) -> str:
        return format_html(
            '<span style="background:{};color:#fff;padding:2px 10px;border-radius:4px;">{}</span>',
            obj.color,
            obj.name,
        )


admin.site.site_header = "TicketFlow Operations Admin"
admin.site.site_title = "TicketFlow Admin"
admin.site.index_title = "Customer Support & Triage Console"