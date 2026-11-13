import json

from django.contrib.admin.views.decorators import staff_member_required
from django.http import JsonResponse
from django.views.decorators.http import require_POST

from .ai_assistant import ask_form_assistant


@staff_member_required
@require_POST
def admin_ai_assist_view(request):
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({"error": "Invalid JSON payload."}, status=400)

    question = payload.get("question", "")
    model_name = payload.get("model", "ticket")

    result = ask_form_assistant(question=question, model_name=model_name)
    return JsonResponse(result)