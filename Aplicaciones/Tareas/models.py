from django.db import models


class Tarea(models.Model):
    """Una orden que pasó por la fila de Celery (se llena sola con señales)."""
    ESTADOS = [
        ("en_cola", "En cola"),
        ("ejecutando", "Ejecutando"),
        ("reintento", "Reintentando"),
        ("ok", "Completada"),
        ("error", "Error"),
    ]
    task_id = models.CharField(max_length=50, unique=True)
    nombre = models.CharField(max_length=100)          # ej. autorizar_onu_task
    descripcion = models.CharField(max_length=200)     # ej. "Suspender: Juan Pérez (jperez)"
    enlace = models.CharField(max_length=200, blank=True)  # a dónde lleva el clic (cliente, ONU…)
    cola = models.CharField(max_length=50, blank=True)     # ej. olt_2, mk_1
    estado = models.CharField(max_length=12, choices=ESTADOS, default="en_cola", db_index=True)
    resultado = models.TextField(blank=True)
    intentos = models.PositiveSmallIntegerField(default=0)
    creada = models.DateTimeField(auto_now_add=True, db_index=True)
    iniciada = models.DateTimeField(null=True, blank=True)
    terminada = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-creada"]

    def __str__(self):
        return f"{self.descripcion} [{self.get_estado_display()}]"

    @property
    def espera(self):
        """Segundos que estuvo esperando en la fila."""
        if self.iniciada:
            return (self.iniciada - self.creada).total_seconds()
        return None

    @property
    def duracion(self):
        """Segundos que tardó el equipo en hacerla."""
        if self.iniciada and self.terminada:
            return (self.terminada - self.iniciada).total_seconds()
        return None
