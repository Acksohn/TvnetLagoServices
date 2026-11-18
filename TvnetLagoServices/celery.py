"""
Arranque de Celery para el proyecto TvnetLagoServices.
Celery lee la configuración de settings.py (las variables que empiezan con CELERY_)
y busca automáticamente un archivo tasks.py en cada app instalada.
"""
import os
 
from celery import Celery
 
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "TvnetLagoServices.settings")
 
app = Celery("TvnetLagoServices")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
 