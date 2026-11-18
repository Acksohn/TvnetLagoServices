from django.contrib import admin, messages

from . import tasks

AVISO_REDIS = ("No se pudo poner la orden en la fila: ¿está encendido Redis? "
               "(en Ubuntu/WSL: sudo service redis-server start). Detalle: ")
from .models import OLT, ONU, VLAN, PerfilVelocidad, PlanOLT


@admin.register(OLT)
class OLTAdmin(admin.ModelAdmin):
    list_display = ("id", "nombre", "host", "protocolo", "puerto_ssh", "solo_lectura", "cola",
                    "modelo_equipo", "conectada", "activa")
    readonly_fields = ("modelo_equipo", "version", "encendida_desde", "tarjetas", "conectada",
                       "info_leida", "accion_pendiente", "accion_desde", "ultimo_error")
    list_filter = ("protocolo", "solo_lectura", "activa")
    actions = ["buscar_onus"]

    @admin.display(description="Fila Celery")
    def cola(self, obj):
        return tasks.cola_de(obj)

    @admin.action(description="🔍 Buscar ONUs nuevas (autofind)")
    def buscar_onus(self, request, queryset):
        for olt in queryset:
            try:
                tasks.encolar_busqueda(olt)
            except Exception as e:
                self.message_user(request, AVISO_REDIS + str(e), messages.ERROR)
                return
            self.message_user(request, f"{olt}: búsqueda puesta en la fila "
                                       f"'{tasks.cola_de(olt)}'. Recarga ONUs en unos segundos.")


@admin.register(PlanOLT)
class PlanOLTAdmin(admin.ModelAdmin):
    list_display = ("nombre", "olt", "lineprofile_id", "srvprofile_id", "vlan", "gemport")
    list_filter = ("olt",)


@admin.register(VLAN)
class VLANAdmin(admin.ModelAdmin):
    list_display = ("vlan_id", "olt", "descripcion", "gestion", "iptv", "en_olt", "service_ports")
    list_filter = ("olt",)


@admin.register(PerfilVelocidad)
class PerfilVelocidadAdmin(admin.ModelAdmin):
    list_display = ("nombre", "direccion", "kbps", "nombre_olt", "indice_olt", "predeterminado", "creado_en_olt")
    list_filter = ("direccion",)


@admin.register(ONU)
class ONUAdmin(admin.ModelAdmin):
    list_display = ("sn", "olt", "fsp", "ont_id", "modelo", "cliente", "plan", "estado", "rx_power")
    list_filter = ("olt", "estado")
    search_fields = ("sn", "descripcion")
    readonly_fields = ("ont_id", "service_port", "estado", "ultimo_error", "actualizado")
    actions = ["autorizar", "dar_de_baja"]

    @admin.action(description="✅ Autorizar en OLT + crear PPPoE en MikroTik")
    def autorizar(self, request, queryset):
        for onu in queryset:
            if onu.olt.solo_lectura:
                self.message_user(request, f"{onu.sn}: {onu.olt} es SOLO LECTURA.", messages.WARNING)
                continue
            if (onu.estado != "sin_autorizar" and not onu.autorizando_trabada) or not onu.plan_efectivo or not onu.cliente_id:
                self.message_user(request, f"{onu.sn}: debe estar 'Sin autorizar' y tener perfiles, VLAN y Cliente.",
                                  messages.WARNING)
                continue
            try:
                tasks.encolar_autorizacion(onu)
            except ValueError as e:  # paso 14: ya está en la fila
                self.message_user(request, str(e), messages.WARNING)
                continue
            except Exception as e:
                self.message_user(request, AVISO_REDIS + str(e), messages.ERROR)
                return
            self.message_user(request, f"{onu.sn}: autorización en cola. Recarga en unos segundos.")

    @admin.action(description="🗑️ Dar de baja en la OLT")
    def dar_de_baja(self, request, queryset):
        for onu in queryset:
            if onu.olt.solo_lectura:
                self.message_user(request, f"{onu.sn}: {onu.olt} es SOLO LECTURA.", messages.WARNING)
                continue
            if onu.ont_id is None:
                self.message_user(request, f"{onu.sn}: no está autorizada.", messages.WARNING)
                continue
            try:
                tasks.encolar_baja(onu)
            except Exception as e:
                self.message_user(request, AVISO_REDIS + str(e), messages.ERROR)
                return
            self.message_user(request, f"{onu.sn}: baja en cola. Recarga en unos segundos.")
