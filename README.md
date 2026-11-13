# TicketFlow — Support Operations & Triage Console

A production-configured Django support ticket tracking application featuring a customized Django Admin console, strict role-based access control (`Support Admin` vs. `Support Agent`), managed PostgreSQL persistence, S3-compatible object storage for comment attachments, and a schema-grounded AI Form Assistant.

## Live Demo & Evaluator Credentials

* **Live URL:** `<replace-with-your-deployed-url>/admin/`
* **Support Admin (Full Access):** `admin_demo` / `AdminPass123!`
* **Support Agent (Restricted Queue & Read-Only Triage):** `agent_demo` / `AgentPass123!`

---

## Architecture & Stack Decisions

### Why Python 3.12+ / Django 5.2?
1. **Native Admin Customization:** The task explicitly evaluates customizing a framework's built-in admin tooling rather than hand-building an SPA from scratch. Django's `ModelAdmin` provides declarative list filters, full-text search across relations, `TabularInline` comment threads, bulk actions, and fine-grained queryset/field-level hooks (`get_queryset`, `get_readonly_fields`, `has_module_permission`).
2. **Defense-in-Depth Validation:** Domain invariants (valid status state transitions, mandatory agent assignment, per-agent active workload limits, and file upload allowlists) live inside `Ticket.clean()` / `Ticket.save()` and `validators.py` rather than templates or views, guaranteeing enforcement across Admin forms, bulk actions, management commands, and future APIs.
3. **12-Factor Infrastructure Fail-Fast:** `config/settings.py` refuses to boot in production (`DJANGO_DEBUG=False`) if `DJANGO_SECRET_KEY`, `DATABASE_URL`, or S3 credentials are missing—eliminating silent fallbacks to SQLite or ephemeral container disk.

### Data Model & Domain Logic
* **`Customer`:** Account profile with `FREE`, `PRO`, and `ENTERPRISE` tiers.
* **`AgentProfile`:** One-to-one extension of Django's `User` storing `role` (`ADMIN` or `AGENT`), `department`, `max_open_tickets` capacity cap, and availability. Post-save signals automatically synchronize Django `Group` permissions and `is_staff`.
* **`Ticket`:** Tracks `reference_code` (`TCK-XXXXXX`), `customer` (`PROTECT`), `assigned_agent` (`SET_NULL`), `tags` (`ManyToManyField`), `status`, `priority`, and `resolved_at`.
  * **State Machine:** Enforces directed transitions (`OPEN -> IN_PROGRESS / WAITING_CUSTOMER / RESOLVED -> CLOSED`). Direct `OPEN -> CLOSED` jumps are blocked.
  * **Workload Guardrail:** Blocks assigning new active tickets if an agent has reached `max_open_tickets`.
  * **Dynamic SLA Computation:** Computes priority-based resolution deadlines (`URGENT`: 4h, `HIGH`: 12h, `MEDIUM`: 24h, `LOW`: 72h) and surfaces live `On Track` / `Met` / `BREACHED` badges.
* **`TicketComment`:** Chronological replies and `is_internal_note` flags edited inline on the `Ticket` page, with optional `attachment` uploads stored in S3/R2 under UUID-prefixed keys (`ticket-attachments/<ref>/<uuid>.<ext>`), restricted to `.pdf, .png, .jpg, .jpeg, .txt, .log, .csv` up to 5 MB.

### Role-Based Access Control (`Support Admin` vs. `Support Agent`)
| Capability | Support Admin | Support Agent |
| :--- | :--- | :--- |
| **Ticket Visibility** | All tickets across all teams | Assigned tickets + unassigned `OPEN` tickets only |
| **Ticket Creation / Deletion** | Full CRUD | Edit assigned tickets only (no create/delete) |
| **Triage Fields (`customer`, `assigned_agent`, `priority`, `tags`)** | Editable | Read-only (`get_readonly_fields`) |
| **Bulk Actions** | Assign to me, Mark Resolved, Escalate to Urgent | Assign to me, Mark Resolved (`Escalate` stripped) |
| **Agent Profiles & Capacity** | Full management | Hidden (`has_module_permission = False`) |

### Bonus: Schema-Grounded AI Form Assistant
* Accessible via the floating **Form Field Assistant** drawer on every `/admin/` page.
* Introspects live Django model metadata (`_meta.get_fields()`, `verbose_name`, `required`, `choices`, `help_text`, plus `Ticket.ALLOWED_TRANSITIONS` and inline `TicketComment` fields) and injects that exact schema into the system prompt.
* Protected behind `@staff_member_required` + CSRF validation, with a 500-character input cap and control-character stripping.
* **Zero-Crash Fallback:** If `LLM_API_KEY` is omitted or the upstream provider times out/errors, `ask_form_assistant()` automatically falls back to a deterministic field-reference responder built from the live model metadata.

---

## Local Setup & Testing

```bash
# 1. Create virtual environment & install dependencies
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Configure environment variables
cp .env.example .env
# For quick local testing without Postgres/S3, set DJANGO_DEBUG=True and USE_S3_STORAGE=False in .env

# 3. Apply migrations & seed realistic demo data (160 tickets, 45 customers, 8 agents)
python manage.py migrate
python manage.py seed_data   # Pass --flush to wipe and regenerate

# 4. Run automated test suite
DJANGO_DEBUG=True USE_S3_STORAGE=False pytest

# 5. Start development server
python manage.py runserver