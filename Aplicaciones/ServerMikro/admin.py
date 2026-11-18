from django.contrib import admin, messages

from . import tasks
from .models import CajaNAP, Cliente, Plan, RedIPv4, Router
from .servicios import ESTADO_REQUERIDO


@admin.register(Router)
class RouterAdmin(admin.ModelAdmin):
    list_display = ("nombre", "ip", "metodo", "puerto", "usuario", "cola", "modelo", "conectado")
    readonly_fields = ("huella_vista", "modelo", "version", "encendido_desde", "cpu", "memoria_libre", "ppp_activos",
                       "conectado", "info_leida", "accion_pendiente", "accion_desde", "ultimo_error")

    @admin.display(description="Fila Celery")
    def cola(self, obj):
        return tasks.cola_de(obj)


@admin.register(Plan)
class PlanAdmin(admin.ModelAdmin):
    list_display = ("nombre", "subida", "bajada", "precio", "activo")
    list_filter = ("activo",)


@admin.register(Cliente)
class ClienteAdmin(admin.ModelAdmin):
    list_display = ("nombre", "usuario_pppoe", "ip", "plan", "velocidad", "router", "estado", "accion_pendiente")
    list_filter = ("estado", "router", "plan")
    search_fields = ("nombre", "usuario_pppoe", "ip", "cedula")
    readonly_fields = ("ultimo_error", "actualizado")
    actions = ["crear_en_mikrotik", "suspender", "activar"]

    def _encolar(self, request, queryset, accion):
        for c in queryset.select_related("router"):
            if c.estado not in ESTADO_REQUERIDO[accion]:
                self.message_user(request, f"{c}: está {c.get_estado_display()}, se omite.",
                                  messages.WARNING)
                continue
            try:
                tasks.encolar(c, accion)
                self.message_user(request, f"{c}: {accion} en cola ({tasks.cola_de(c.router)}).")
            except Exception as e:
                self.message_user(request, f"{c}: ERROR {e}", messages.ERROR)

    @admin.action(description="Crear en MikroTik")
    def crear_en_mikrotik(self, request, queryset):
        self._encolar(request, queryset, "crear")

    @admin.action(description="Suspender")
    def suspender(self, request, queryset):
        self._encolar(request, queryset, "suspender")

    @admin.action(description="Activar")
    def activar(self, request, queryset):
        self._encolar(request, queryset, "activar")


@admin.register(RedIPv4)
class RedIPv4Admin(admin.ModelAdmin):
    """Paso 15. Mejor créalas desde el panel (Redes IPv4): ahí se valida que no se crucen."""
    list_display = ("cidr_texto", "router", "gateway", "activa")
    list_filter = ("router", "activa")



@admin.register(CajaNAP)
class CajaNAPAdmin(admin.ModelAdmin):
    """Paso 16. Mejor desde el panel (Cajas NAP), donde se ven los puertos libres."""
    list_display = ("nombre", "ubicacion", "coordenadas", "puertos")
    search_fields = ("nombre", "ubicacion")
