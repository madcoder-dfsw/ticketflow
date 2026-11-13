import random
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from ticketsApp.models import AgentProfile, Customer, Tag, Ticket, TicketComment
from ticketsApp.permissions import ensure_role_groups

User = get_user_model()

TAGS_DATA = [
    ("billing", "#0284C7"),
    ("bug", "#DC2626"),
    ("outage", "#991B1B"),
    ("vip", "#7C3AED"),
    ("security", "#B45309"),
    ("api", "#0D9488"),
    ("onboarding", "#2563EB"),
    ("feature-request", "#4F46E5"),
]

AGENTS_DATA = [
    ("admin_demo", "Elena", "Rostova", "elena.rostova@ticketflow.io", AgentProfile.Role.ADMIN, "Operations Leadership", 40),
    ("agent_demo", "Marcus", "Vance", "marcus.vance@ticketflow.io", AgentProfile.Role.AGENT, "Technical Support", 35),
    ("sofia.lindqvist", "Sofia", "Lindqvist", "sofia.l@ticketflow.io", AgentProfile.Role.ADMIN, "Escalations & Risk", 40),
    ("daniel.kovacs", "Daniel", "Kovacs", "daniel.k@ticketflow.io", AgentProfile.Role.AGENT, "Billing & Accounts", 35),
    ("priya.nair", "Priya", "Nair", "priya.n@ticketflow.io", AgentProfile.Role.AGENT, "API & Integrations", 35),
    ("lukas.meyer", "Lukas", "Meyer", "lukas.m@ticketflow.io", AgentProfile.Role.AGENT, "Technical Support", 35),
    ("hana.sato", "Hana", "Sato", "hana.s@ticketflow.io", AgentProfile.Role.AGENT, "Enterprise Success", 35),
    ("tomas.novak", "Tomas", "Novak", "tomas.n@ticketflow.io", AgentProfile.Role.AGENT, "Billing & Accounts", 35),
]

FIRST_NAMES = [
    "Liam", "Olivia", "Noah", "Emma", "Oliver", "Ava", "Elijah", "Sophia",
    "Mateo", "Isabella", "Lucas", "Mia", "Levi", "Charlotte", "Ezra", "Amelia",
    "Asher", "Harper", "Leo", "Evelyn", "Klara", "Henrik", "Nadia", "Viktor",
    "Amina", "Tariq", "Clara", "Julian", "Maren", "Soren",
]

LAST_NAMES = [
    "Bergstrom", "Kowalski", "Patel", "Chen", "Okafor", "Lindholm", "Silva",
    "Novak", "Morales", "Tanaka", "Muller", "Dubois", "Richter", "Alvarez",
    "Vaher", "Kask", "Johansson", "Bennett", "Walsh", "Costa",
]

COMPANIES = [
    "NordicPay Systems", "Kestrel Logistics", "Veloce Commerce", "Aether Cloud",
    "Boreal Analytics", "Stratos Health", "FinBridge Europe", "Lumina Retail",
    "Octave AI", "Harborline Freight", "Cinder Security", "Meridian Media",
    "PulseGrid Energy", "Atlas BioLabs", "", "",
]

TICKET_TEMPLATES = [
    ("Webhook signature verification failing on /v2/events", "HMAC-SHA256 digest mismatch started appearing after rotating our secondary signing secret in staging.", Ticket.Priority.HIGH, ["api", "bug"]),
    ("Duplicate invoice generated for annual Pro renewal", "Customer reports two charge attempts on card ending in 4419 for the same billing cycle.", Ticket.Priority.URGENT, ["billing", "vip"]),
    ("SSO SAML assertion rejected with InvalidAudience", "Okta integration returns 403 when users authenticate via IdP-initiated login.", Ticket.Priority.HIGH, ["security", "onboarding"]),
    ("CSV export times out for date ranges > 90 days", "Export job spins for 60s and returns gateway timeout when downloading quarterly ledger.", Ticket.Priority.MEDIUM, ["bug", "api"]),
    ("Requesting higher rate limits ahead of Black Friday launch", "Enterprise merchant expects 5x traffic spike next week and needs burst ceiling raised to 2,000 req/min.", Ticket.Priority.HIGH, ["vip", "api"]),
    ("Unable to invite new team members to workspace", "Invite emails are queued but never delivered to corporate @finbridge domains.", Ticket.Priority.MEDIUM, ["onboarding", "bug"]),
    ("Intermittent 502 Bad Gateway on EU-Central cluster", "Elevated error rate observed between 08:15 and 08:40 UTC across eu-central-1 ingress.", Ticket.Priority.URGENT, ["outage", "bug"]),
    ("Need VAT ID added to historical Q3 receipts", "Finance team requires updated tax registration number reflected on past three invoices.", Ticket.Priority.LOW, ["billing"]),
    ("Audit log retention policy clarification", "Compliance officer asking whether Enterprise tier retains immutable admin audit trails for 7 years.", Ticket.Priority.LOW, ["security", "vip"]),
    ("Support custom webhook retry backoff intervals", "Feature request to configure exponential retry windows per endpoint.", Ticket.Priority.LOW, ["feature-request", "api"]),
]

PUBLIC_REPLIES = [
    "Thank you for reaching out. We have reproduced the behavior on our end and are investigating the root cause.",
    "Could you confirm the approximate UTC timestamp and request ID from your latest attempt?",
    "We have applied a configuration update to your workspace. Please test again and let us know if the issue persists.",
    "Our billing specialist has voided the duplicate pending authorization; it will drop off your statement within 2–3 business days.",
    "This fix has now been deployed to production. Marking this ticket as resolved—feel free to reply if anything else comes up.",
]

INTERNAL_NOTES = [
    "Checked Datadog traces—correlated with upstream connection pool saturation on worker-03.",
    "Verified customer SLA tier and escalated to on-call platform engineer.",
    "Reviewed account ledger; no duplicate settlement occurred, only an auth hold.",
    "Linked incident to internal tracking issue #4821.",
]


class Command(BaseCommand):
    help = "Seed realistic support ticket data (customers, agents, tags, 160 tickets, 450+ comments)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--flush",
            action="store_true",
            help="Delete existing tickets, customers, tags, and demo agents before seeding.",
        )
        parser.add_argument(
            "--tickets",
            type=int,
            default=160,
            help="Number of tickets to ensure exist in the database (default: 160).",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        rng = random.Random(42)
        ensure_role_groups()

        if options["flush"]:
            self.stdout.write(self.style.WARNING("Flushing existing support operations data..."))
            TicketComment.objects.all().delete()
            Ticket.objects.all().delete()
            Tag.objects.all().delete()
            Customer.objects.all().delete()
            demo_usernames = [row[0] for row in AGENTS_DATA]
            AgentProfile.objects.filter(user__username__in=demo_usernames).delete()
            User.objects.filter(username__in=demo_usernames, is_superuser=False).delete()

        # 1. Tags
        tags_by_name = {}
        for name, color in TAGS_DATA:
            tag, _ = Tag.objects.get_or_create(name=name, defaults={"color": color})
            tags_by_name[name] = tag

        # 2. Staff Users & AgentProfiles
        agents = []
        for username, first, last, email, role, dept, max_tickets in AGENTS_DATA:
            user, created = User.objects.get_or_create(
                username=username,
                defaults={
                    "first_name": first,
                    "last_name": last,
                    "email": email,
                    "is_staff": True,
                },
            )
            if created:
                password = "AdminPass123!" if role == AgentProfile.Role.ADMIN else "AgentPass123!"
                user.set_password(password)
                user.save()

            profile, _ = AgentProfile.objects.get_or_create(
                user=user,
                defaults={
                    "role": role,
                    "department": dept,
                    "max_open_tickets": max_tickets,
                    "is_available": True,
                },
            )
            agents.append(profile)

        # 3. Customers (45)
        existing_customers = list(Customer.objects.all())
        existing_emails = {c.email for c in existing_customers}
        tiers = [Customer.Tier.FREE, Customer.Tier.PRO, Customer.Tier.ENTERPRISE]
        tier_weights = [0.45, 0.35, 0.20]

        new_customers = []
        idx = len(existing_customers)
        while len(existing_customers) + len(new_customers) < 45:
            idx += 1
            first = rng.choice(FIRST_NAMES)
            last = rng.choice(LAST_NAMES)
            company = rng.choice(COMPANIES)
            domain = company.lower().replace(" ", "") + ".com" if company else "example.org"
            email = f"{first.lower()}.{last.lower()}.{idx}@{domain}"
            if email in existing_emails:
                continue
            existing_emails.add(email)
            new_customers.append(
                Customer(
                    full_name=f"{first} {last}",
                    email=email,
                    company=company,
                    tier=rng.choices(tiers, weights=tier_weights, k=1)[0],
                )
            )

        if new_customers:
            Customer.objects.bulk_create(new_customers)
        customers = list(Customer.objects.all())

        # 4. Tickets & Comments (validated via clean(), then bulk-inserted)
        existing_tickets = Ticket.objects.count()
        target_tickets = options["tickets"]
        to_create = max(0, target_tickets - existing_tickets)

        if to_create == 0:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Database already has {existing_tickets} tickets. Pass --flush to regenerate."
                )
            )
            self._print_credentials()
            return

        now = timezone.now()
        active_counts = {agent.id: agent.active_ticket_count for agent in agents}
        statuses = [
            Ticket.Status.OPEN,
            Ticket.Status.IN_PROGRESS,
            Ticket.Status.WAITING_CUSTOMER,
            Ticket.Status.RESOLVED,
            Ticket.Status.CLOSED,
        ]
        status_weights = [0.20, 0.25, 0.15, 0.25, 0.15]

        tickets_to_insert = []
        ticket_tag_names = []

        for i in range(to_create):
            title_base, desc_base, default_priority, tag_names = rng.choice(TICKET_TEMPLATES)
            customer = rng.choice(customers)
            desired_status = rng.choices(statuses, weights=status_weights, k=1)[0]
            priority = rng.choices(
                [default_priority, Ticket.Priority.LOW, Ticket.Priority.MEDIUM, Ticket.Priority.HIGH, Ticket.Priority.URGENT],
                weights=[0.50, 0.12, 0.18, 0.12, 0.08],
                k=1,
            )[0]

            eligible_agents = [a for a in agents if active_counts[a.id] < a.max_open_tickets]
            if desired_status == Ticket.Status.OPEN and rng.random() < 0.45:
                assigned_agent = None
            elif eligible_agents:
                assigned_agent = rng.choice(eligible_agents)
            else:
                assigned_agent = rng.choice(agents)
                desired_status = Ticket.Status.RESOLVED

            age_hours = rng.randint(1, 14 * 24)
            created_at = now - timedelta(hours=age_hours)
            resolved_at = None
            if desired_status in {Ticket.Status.RESOLVED, Ticket.Status.CLOSED}:
                resolution_duration = rng.randint(1, min(age_hours, 48))
                resolved_at = created_at + timedelta(hours=resolution_duration)

            ticket = Ticket(
                reference_code=f"TCK-{(existing_tickets + i + 1000):06X}",
                title=f"{title_base} (#{existing_tickets + i + 101})",
                description=f"{desc_base}\n\nEnvironment: Production | Account Tier: {customer.get_tier_display()}",
                customer=customer,
                assigned_agent=assigned_agent,
                status=desired_status,
                priority=priority,
                resolved_at=resolved_at,
            )
            # Validate business rules (agent required for non-OPEN, etc.)
            if desired_status != Ticket.Status.CLOSED:
                ticket.clean()

            if assigned_agent and desired_status not in {Ticket.Status.RESOLVED, Ticket.Status.CLOSED}:
                active_counts[assigned_agent.id] += 1

            tickets_to_insert.append((ticket, created_at))
            ticket_tag_names.append(tag_names)

        created_ticket_objs = Ticket.objects.bulk_create([t for t, _ in tickets_to_insert])

        # Backdate created_at and build M2M through rows + comments in memory
        ThroughModel = Ticket.tags.through
        m2m_rows = []
        comments_to_insert = []

        for (ticket, created_at), tag_names in zip(
            [(obj, ts) for obj, (_, ts) in zip(created_ticket_objs, tickets_to_insert)],
            ticket_tag_names,
        ):
            ticket.created_at = created_at
            for t_name in tag_names:
                tag_obj = tags_by_name.get(t_name)
                if tag_obj:
                    m2m_rows.append(ThroughModel(ticket_id=ticket.id, tag_id=tag_obj.id))

            num_comments = rng.randint(1, 5)
            for c_idx in range(num_comments):
                is_internal = rng.random() < 0.35
                body = rng.choice(INTERNAL_NOTES if is_internal else PUBLIC_REPLIES)
                author = ticket.assigned_agent.user if ticket.assigned_agent else rng.choice(agents).user
                comment_time = min(now, created_at + timedelta(minutes=25 * (c_idx + 1)))
                comments_to_insert.append(
                    TicketComment(
                        ticket=ticket,
                        author=author,
                        body=body,
                        is_internal_note=is_internal,
                        created_at=comment_time,
                    )
                )

        Ticket.objects.bulk_update(created_ticket_objs, ["created_at"])
        if m2m_rows:
            ThroughModel.objects.bulk_create(m2m_rows, ignore_conflicts=True)
        created_comment_objs = TicketComment.objects.bulk_create(comments_to_insert)

        # Preserve backdated comment timestamps
        for comment, spec in zip(created_comment_objs, comments_to_insert):
            comment.created_at = spec.created_at
        TicketComment.objects.bulk_update(created_comment_objs, ["created_at"])

        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded {len(created_ticket_objs)} tickets and {len(created_comment_objs)} comments "
                f"(Total tickets: {Ticket.objects.count()}, Customers: {Customer.objects.count()})."
            )
        )
        self._print_credentials()

    def _print_credentials(self):
        self.stdout.write("\nDemo Admin Credentials:")
        self.stdout.write("  Support Admin -> username: admin_demo | password: AdminPass123!")
        self.stdout.write("  Support Agent -> username: agent_demo | password: AgentPass123!\n")