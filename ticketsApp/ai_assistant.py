from __future__ import annotations

import logging
import re
from typing import Any

import httpx
from django.apps import apps
from django.conf import settings
from django.db import models

from .models import Ticket, TicketComment

logger = logging.getLogger(__name__)

ALLOWED_MODEL_NAMES = {"ticket", "customer", "agentprofile", "tag"}
MAX_QUESTION_LENGTH = 500


def sanitize_question(raw_question: str) -> str:
    """Normalize whitespace, strip control characters, and enforce length cap."""
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", raw_question or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:MAX_QUESTION_LENGTH]


def extract_model_schema_context(model_name: str) -> dict[str, Any]:
    """
    Introspect live Django model metadata (field names, types, required flags,
    choices, and help_text) so the LLM is strictly grounded in real form definitions.
    """
    normalized = (model_name or "ticket").strip().lower()
    if normalized not in ALLOWED_MODEL_NAMES:
        normalized = "ticket"

    model_cls = apps.get_model("ticketsApp", normalized)
    fields_info = []

    for field in model_cls._meta.get_fields():
        if field.auto_created and not field.concrete:
            continue
        if isinstance(field, models.AutoField):
            continue

        field_entry: dict[str, Any] = {
            "name": field.name,
            "label": str(getattr(field, "verbose_name", field.name)),
            "type": field.get_internal_type(),
            "required": not getattr(field, "blank", True) and not getattr(field, "null", True),
            "editable": getattr(field, "editable", True),
            "help_text": str(getattr(field, "help_text", "") or ""),
        }
        choices = getattr(field, "choices", None)
        if choices:
            field_entry["choices"] = [f"{val} ({label})" for val, label in choices]

        fields_info.append(field_entry)

    # For Ticket forms, include inline TicketComment field metadata & state-machine rules
    extra_rules: list[str] = []
    if normalized == "ticket":
        extra_rules.append(
            "Status workflow rules: "
            + "; ".join(
                f"{src} -> {', '.join(sorted(dsts))}"
                for src, dsts in Ticket.ALLOWED_TRANSITIONS.items()
            )
        )
        extra_rules.append(
            "An assigned_agent is required before moving a ticket out of OPEN status, "
            "and each agent cannot exceed their max_open_tickets limit."
        )
        for c_field in TicketComment._meta.get_fields():
            if c_field.name in {"body", "is_internal_note", "attachment"}:
                fields_info.append(
                    {
                        "name": f"comment.{c_field.name} (inline)",
                        "label": str(c_field.verbose_name),
                        "type": c_field.get_internal_type(),
                        "required": c_field.name == "body",
                        "editable": True,
                        "help_text": str(c_field.help_text or ""),
                    }
                )

    return {
        "model": model_cls.__name__,
        "fields": fields_info,
        "business_rules": extra_rules,
    }


def _build_schema_fallback(question: str, schema_ctx: dict[str, Any]) -> str:
    """
    Deterministic fallback when no LLM API key is configured or the upstream provider fails.
    Matches field names mentioned in the question against live help_text metadata.
    """
    q_lower = question.lower()
    matched = [
        f
        for f in schema_ctx["fields"]
        if f["name"].lower() in q_lower or f["label"].lower() in q_lower
    ]

    targets = matched if matched else schema_ctx["fields"][:5]
    lines = [f"Field reference for **{schema_ctx['model']}** (from live schema metadata):"]
    for f in targets:
        req_tag = "required" if f["required"] else "optional"
        choice_str = f" | Options: {', '.join(f['choices'])}" if f.get("choices") else ""
        help_str = f["help_text"] or "No additional description."
        lines.append(f"• {f['label']} (`{f['name']}`, {req_tag}): {help_str}{choice_str}")

    if schema_ctx["business_rules"] and any(
        kw in q_lower for kw in ("status", "agent", "rule", "transition", "close", "resolve")
    ):
        lines.append("Workflow rules: " + " ".join(schema_ctx["business_rules"]))

    return "\n".join(lines)


def ask_form_assistant(question: str, model_name: str = "ticket") -> dict[str, Any]:
    """
    Query the configured OpenAI-compatible LLM endpoint with grounded Django model schema.
    Always degrades gracefully on missing credentials, timeouts, or upstream HTTP errors.
    """
    clean_q = sanitize_question(question)
    if not clean_q:
        return {"answer": "Please enter a question about a form field or workflow rule.", "source": "validation"}

    schema_ctx = extract_model_schema_context(model_name)

    if not settings.LLM_API_KEY:
        return {
            "answer": _build_schema_fallback(clean_q, schema_ctx),
            "source": "schema-fallback",
            "model": schema_ctx["model"],
        }

    schema_lines = []
    for f in schema_ctx["fields"]:
        line = f"- {f['name']} ({f['type']}, {'required' if f['required'] else 'optional'}): {f['help_text']}"
        if f.get("choices"):
            line += f" | Choices: {', '.join(f['choices'])}"
        schema_lines.append(line)

    system_prompt = (
        "You are an internal Django Admin assistant for TicketFlow, a customer support tracker.\n"
        "Answer ONLY using the live form schema and business rules provided below. "
        "Be concise (3-5 sentences max), practical, and never invent fields or policies that are not listed.\n\n"
        f"Active Admin Model: {schema_ctx['model']}\n"
        "Fields:\n" + "\n".join(schema_lines) + "\n"
    )
    if schema_ctx["business_rules"]:
        system_prompt += "Business Rules:\n- " + "\n- ".join(schema_ctx["business_rules"]) + "\n"

    endpoint = f"{settings.LLM_BASE_URL.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.LLM_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": settings.LLM_MODEL,
        "temperature": 0.2,
        "max_tokens": 250,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": clean_q},
        ],
    }

    try:
        with httpx.Client(timeout=8.0) as client:
            response = client.post(endpoint, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
            answer = data["choices"][0]["message"]["content"].strip()
            return {
                "answer": answer,
                "source": "llm",
                "model": schema_ctx["model"],
            }
    except Exception as exc:
        logger.warning("AI assistant upstream call failed (%s); using schema fallback.", exc)
        return {
            "answer": _build_schema_fallback(clean_q, schema_ctx),
            "source": "schema-fallback",
            "model": schema_ctx["model"],
        }