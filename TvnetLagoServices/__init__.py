# Carga Celery junto con Django, para que los @shared_task usen esta configuración.
from .celery import app as celery_app
 
__all__ = ("celery_app",)
 