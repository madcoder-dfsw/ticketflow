import json
from datetime import timedelta
from unittest.mock import MagicMock, patch

import httpx
import pytest
from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory
from django.urls import reverse
from django.utils import timezone

from ticketsApp.admin import AgentProfileAdmin, TicketAdmin
from ticketsApp.ai_assistant import ask_form_assistant, extract_model_schema_context
from ticketsApp.models import AgentProfile, Customer, Tag, Ticket, TicketComment
from ticketsApp.validators import MAX_ATTACHMENT_SIZE_BYTES

User = get_user_model()


@pytest.fixture
def customer(db):
    return Customer.objects.create(
        full_name="Aino Kask",
        email="aino.kask@nordicpay.ee",
        company="NordicPay",
        tier=Customer.Tier.ENTERPRISE,
    )


@pytest.fixture
def support_admin_user(db):
    user = User.objects.create_user(
        username="admin_tester",
        password="StrongPassword123!",
        is_staff=True,
    )
    AgentProfile.objects.create(
        user=user,
        role=AgentProfile.Role.ADMIN,
        department="Operations",
        max_open_tickets=10,
    )
    return user


@pytest.fixture
def support_agent_user(db):
    user = User.objects.create_user(
        username="agent_tester",
        password="StrongPassword123!",
        is_staff=True,
    )
    AgentProfile.objects.create(
        user=user,
        role=AgentProfile.Role.AGENT,
        department="Tier 1 Support",
        max_open_tickets=2,
    )
    return user


@pytest.fixture
def other_agent_user(db):
    user = User.objects.create_user(
        username="other_agent",
        password="StrongPassword123!",
        is_staff=True,
    )
    AgentProfile.objects.create(
        user=user,
        role=AgentProfile.Role.AGENT,
        department="Billing",
        max_open_tickets=5,
    )
    return user


@pytest.mark.django_db
class TestTicketBusinessLogic:
    def test_requires_agent_before_leaving_open_status(self, customer):
        ticket = Ticket(
            title="API auth failure",
            description="401 returned with valid token",
            customer=customer,
            status=Ticket.Status.IN_PROGRESS,
        )
        with pytest.raises(ValidationError) as exc:
            ticket.save()
        assert "assigned_agent" in exc.value.message_dict

    def test_valid_lifecycle_transitions_and_resolved_timestamp(self, customer, support_agent_user):
        agent = support_agent_user.agent_profile
        ticket = Ticket.objects.create(
            title="Webhook timeout",
            description="Endpoints timing out after 10s",
            customer=customer,
        )
        assert ticket.reference_code.startswith("TCK-")
        assert ticket.resolved_at is None

        # Cannot jump directly from OPEN to CLOSED
        with pytest.raises(ValidationError) as exc:
            ticket.transition_to(Ticket.Status.CLOSED)
        assert "status" in exc.value.message_dict

        # Assign agent -> IN_PROGRESS -> RESOLVED -> CLOSED
        ticket.assigned_agent = agent
        ticket.transition_to(Ticket.Status.IN_PROGRESS)
        assert ticket.status == Ticket.Status.IN_PROGRESS

        ticket.transition_to(Ticket.Status.RESOLVED)
        assert ticket.status == Ticket.Status.RESOLVED
        assert ticket.resolved_at is not None

        ticket.transition_to(Ticket.Status.CLOSED)
        assert ticket.status == Ticket.Status.CLOSED

    def test_agent_active_ticket_capacity_cap(self, customer, support_agent_user):
        agent = support_agent_user.agent_profile  # max_open_tickets = 2
        for idx in range(2):
            Ticket.objects.create(
                title=f"Active issue {idx}",
                description="Details",
                customer=customer,
                assigned_agent=agent,
                status=Ticket.Status.IN_PROGRESS,
            )

        overflow_ticket = Ticket(
            title="Third active issue",
            description="Should exceed agent cap",
            customer=customer,
            assigned_agent=agent,
            status=Ticket.Status.IN_PROGRESS,
        )
        with pytest.raises(ValidationError) as exc:
            overflow_ticket.save()
        assert "assigned_agent" in exc.value.message_dict

    def test_sla_breach_computation(self, customer):
        ticket = Ticket.objects.create(
            title="Urgent outage",
            description="Production down",
            customer=customer,
            priority=Ticket.Priority.URGENT,  # 4-hour SLA
        )
        assert ticket.is_sla_breached is False

        # Backdate created_at by 5 hours
        Ticket.objects.filter(pk=ticket.pk).update(
            created_at=timezone.now() - timedelta(hours=5)
        )
        ticket.refresh_from_db()
        assert ticket.is_sla_breached is True


@pytest.mark.django_db
class TestAttachmentValidation:
    def test_rejects_disallowed_file_extension(self, customer, support_agent_user):
        ticket = Ticket.objects.create(
            title="Upload test",
            description="Testing attachment security",
            customer=customer,
        )
        malicious_file = SimpleUploadedFile(
            "payload.exe", b"MZ\x90\x00", content_type="application/octet-stream"
        )
        comment = TicketComment(
            ticket=ticket,
            author=support_agent_user,
            body="Attaching executable",
            attachment=malicious_file,
        )
        with pytest.raises(ValidationError) as exc:
            comment.full_clean()
        assert "attachment" in exc.value.message_dict

    def test_rejects_oversized_attachment(self, customer, support_agent_user):
        ticket = Ticket.objects.create(
            title="Large log test",
            description="Testing max file size",
            customer=customer,
        )
        oversized = SimpleUploadedFile(
            "huge.log",
            b"x" * (MAX_ATTACHMENT_SIZE_BYTES + 1024),
            content_type="text/plain",
        )
        comment = TicketComment(
            ticket=ticket,
            author=support_agent_user,
            body="Attaching huge log",
            attachment=oversized,
        )
        with pytest.raises(ValidationError) as exc:
            comment.full_clean()
        assert "attachment" in exc.value.message_dict

    def test_accepts_valid_pdf_attachment(self, customer, support_agent_user):
        ticket = Ticket.objects.create(
            title="Invoice PDF",
            description="Valid attachment",
            customer=customer,
        )
        valid_pdf = SimpleUploadedFile(
            "invoice.pdf", b"%PDF-1.4 valid content", content_type="application/pdf"
        )
        comment = TicketComment(
            ticket=ticket,
            author=support_agent_user,
            body="See attached invoice",
            attachment=valid_pdf,
        )
        comment.full_clean()  # Should not raise


@pytest.mark.django_db
class TestAdminRBAC:
    def setup_method(self):
        self.site = AdminSite()
        self.factory = RequestFactory()
        self.ticket_admin = TicketAdmin(Ticket, self.site)
        self.agent_admin = AgentProfileAdmin(AgentProfile, self.site)

    def test_agent_queryset_scoped_to_own_and_unassigned_open_tickets(
        self, customer, support_agent_user, other_agent_user
    ):
        own_ticket = Ticket.objects.create(
            title="Mine",
            description="Assigned to me",
            customer=customer,
            assigned_agent=support_agent_user.agent_profile,
            status=Ticket.Status.IN_PROGRESS,
        )
        unassigned_open = Ticket.objects.create(
            title="Unassigned",
            description="Open queue",
            customer=customer,
            status=Ticket.Status.OPEN,
        )
        other_ticket = Ticket.objects.create(
            title="Other agent's ticket",
            description="Should be hidden",
            customer=customer,
            assigned_agent=other_agent_user.agent_profile,
            status=Ticket.Status.IN_PROGRESS,
        )

        request = self.factory.get("/admin/ticketsApp/ticket/")
        request.user = support_agent_user
        visible_ids = set(self.ticket_admin.get_queryset(request).values_list("id", flat=True))

        assert own_ticket.id in visible_ids
        assert unassigned_open.id in visible_ids
        assert other_ticket.id not in visible_ids

    def test_agent_readonly_fields_and_hidden_agent_module(
        self, support_agent_user, support_admin_user
    ):
        agent_req = self.factory.get("/admin/ticketsApp/ticket/")
        agent_req.user = support_agent_user

        readonly = self.ticket_admin.get_readonly_fields(agent_req)
        assert "assigned_agent" in readonly
        assert "priority" in readonly
        assert "customer" in readonly
        assert self.agent_admin.has_module_permission(agent_req) is False
        assert "escalate_to_urgent" not in self.ticket_admin.get_actions(agent_req)

        admin_req = self.factory.get("/admin/ticketsApp/ticket/")
        admin_req.user = support_admin_user
        admin_readonly = self.ticket_admin.get_readonly_fields(admin_req)
        assert "assigned_agent" not in admin_readonly
        assert self.agent_admin.has_module_permission(admin_req) is True
        assert "escalate_to_urgent" in self.ticket_admin.get_actions(admin_req)


@pytest.mark.django_db
class TestAIFormAssistant:
    def test_extracts_real_model_schema_and_rules(self):
        ctx = extract_model_schema_context("ticket")
        field_names = {f["name"] for f in ctx["fields"]}
        assert "status" in field_names
        assert "priority" in field_names
        assert "comment.attachment (inline)" in field_names
        assert len(ctx["business_rules"]) >= 2

    def test_graceful_fallback_when_api_key_missing(self, settings):
        settings.LLM_API_KEY = ""
        result = ask_form_assistant("What does status mean?", model_name="ticket")
        assert result["source"] == "schema-fallback"
        assert "status" in result["answer"].lower()

    @patch("ticketsApp.ai_assistant.httpx.Client")
    def test_graceful_fallback_when_upstream_llm_errors(self, mock_client_cls, settings):
        settings.LLM_API_KEY = "test-key"
        mock_client = MagicMock()
        mock_client.__enter__.return_value = mock_client
        mock_client.post.side_effect = httpx.TimeoutException("Upstream timeout")
        mock_client_cls.return_value = mock_client

        result = ask_form_assistant("How does priority work?", model_name="ticket")
        assert result["source"] == "schema-fallback"
        assert "priority" in result["answer"].lower()

    def test_ai_endpoint_requires_staff_and_returns_answer(self, client, support_agent_user, settings):
        settings.LLM_API_KEY = ""
        url = reverse("admin_ai_assist")

        # Unauthenticated request redirects to admin login
        anon_resp = client.post(url, data=json.dumps({"question": "help"}), content_type="application/json")
        assert anon_resp.status_code == 302

        # Authenticated staff request succeeds
        client.force_login(support_agent_user)
        resp = client.post(
            url,
            data=json.dumps({"question": "What does priority do?", "model": "ticket"}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "answer" in body
        assert body["model"] == "Ticket"