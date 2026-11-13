from django.apps import AppConfig


class TicketsappConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "ticketsApp"
    verbose_name = "Support Operations"

    def ready(self) -> None:
        import ticketsApp.signals  # noqa: F401