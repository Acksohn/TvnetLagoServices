from django.apps import AppConfig


class TareasConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "Aplicaciones.Tareas"
    verbose_name = "Tareas en cola"

    def ready(self):
        # Conecta las señales de Celery (en Django y también dentro del worker)
        from . import senales  # noqa: F401
