from django.contrib import admin

from .models import Tarea


@admin.register(Tarea)
class TareaAdmin(admin.ModelAdmin):
    list_display = ("creada", "descripcion", "cola", "estado", "intentos")
    list_filter = ("estado", "cola", "nombre")
    search_fields = ("descripcion", "task_id", "resultado")
    readonly_fields = [f.name for f in Tarea._meta.fields]
