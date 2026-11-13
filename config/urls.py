from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.http import JsonResponse
from django.urls import path
from django.views.generic import RedirectView


def health_check(request):
    return JsonResponse({"status": "ok"})


urlpatterns = [
    path("", RedirectView.as_view(url="/admin/", permanent=False)),
    path("health/", health_check, name="health_check"),
    path("admin/", admin.site.urls),
]

if settings.DEBUG and not settings.USE_S3_STORAGE:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)