"""
Tareas en cola. Cada OLT tiene su propia fila: "olt_<id>".
Un worker con --pool=solo atiende esa fila y ejecuta UNA orden a la vez,
así la OLT nunca recibe dos sesiones al mismo tiempo desde el sistema.

Importante: a las tareas se les pasa el ID (un número), no el objeto,
porque la tarea corre en otro proceso y vuelve a leer la ONU de la base de datos.
"""
from celery import shared_task
from netmiko.exceptions import NetmikoAuthenticationException, NetmikoTimeoutException

from . import servicios
from .models import OLT, ONU

# Si la OLT no responde o rechaza el login (por ejemplo, alguien tiene la
# sesión ocupada), se reintenta 2 veces, esperando 60 s. No más, para no
# provocar el bloqueo de Huawei por intentos fallidos.
REINTENTOS = dict(
    autoretry_for=(NetmikoTimeoutException, NetmikoAuthenticationException),
    retry_backoff=60,
    retry_jitter=False,
    max_retries=2,
)


def cola_de(olt):
    """Nombre de la fila de una OLT. Ej: OLT con id 1 -> 'olt_1'."""
    return f"olt_{olt.pk}"


@shared_task(**REINTENTOS)
def buscar_onus_task(olt_id):
    olt = OLT.objects.get(pk=olt_id)
    total, nuevas, quitadas, registradas = servicios.buscar_onus_nuevas(olt)
    texto = f"{olt}: {total} en autofind, {nuevas} nuevas"
    if quitadas:
        texto += f", {quitadas} ya no aparecen (se quitan de la lista)"
    if registradas:
        texto += f", {registradas} ya estaban autorizadas en la OLT (pasan a Configuradas)"
    return texto


@shared_task  # Paso 14: SIN reintentos automáticos. Es una orden que escribe en la OLT:
             # si falla, la persona ve el error y decide (y al reintentar no se duplica).
def autorizar_onu_task(onu_id):
    onu = ONU.objects.select_related("olt", "plan", "cliente").get(pk=onu_id)
    aviso = servicios.autorizar(onu)
    onu.refresh_from_db()
    texto = f"{onu.sn}: autorizada como ONT {onu.fsp}:{onu.ont_id}"
    return texto + (f" ({aviso})" if aviso else "")


@shared_task(**REINTENTOS)
def baja_onu_task(onu_id):
    onu = ONU.objects.select_related("olt", "plan").get(pk=onu_id)
    servicios.dar_de_baja(onu)
    return f"{onu.sn}: eliminada de la OLT"


@shared_task(bind=True, **REINTENTOS)
def accion_onu_task(self, onu_id, accion):
    """Paso 9: leer señal, reiniciar, desactivar, activar o dar de baja desde la ficha."""
    onu = ONU.objects.select_related("olt", "plan", "cliente").get(pk=onu_id)
    funcion = servicios.ACCIONES_ONU[accion][0]
    try:
        resultado = funcion(onu)
    except (NetmikoTimeoutException, NetmikoAuthenticationException) as e:
        # Se va a reintentar: solo liberamos la ONU si ya fue el último intento
        if self.request.retries >= REINTENTOS["max_retries"]:
            ONU.objects.filter(pk=onu_id).update(accion_pendiente="", ultimo_error=f"{accion}: {e}")
        raise
    except Exception as e:
        ONU.objects.filter(pk=onu_id).update(accion_pendiente="", ultimo_error=f"{accion}: {e}")
        raise
    ONU.objects.filter(pk=onu_id).update(accion_pendiente="", ultimo_error="")
    if accion == "baja":
        # Tras borrarla, la OLT la vuelve a ver en autofind en unos segundos (si sigue conectada).
        # Buscamos solos para que regrese a "Sin configurar" sin que nadie pulse nada.
        try:
            buscar_onus_task.apply_async(args=[onu.olt_id], queue=cola_de(onu.olt), countdown=40)
        except Exception:
            pass  # no es grave: se puede pulsar "Buscar" a mano
    return f"{onu.sn}: {resultado or accion}"


@shared_task  # sin reintentos: es una prueba de conexión, queremos el resultado ya
def info_olt_task(olt_id):
    """Paso 10: 'Actualizar info' (probar conexión + leer versión y tarjetas)."""
    olt = OLT.objects.get(pk=olt_id)
    try:
        resultado = servicios.leer_info_olt(olt)
    except Exception as e:
        OLT.objects.filter(pk=olt_id).update(accion_pendiente="", conectada=False,
                                             ultimo_error=f"{type(e).__name__}: {e}"[:2000])
        raise
    OLT.objects.filter(pk=olt_id).update(accion_pendiente="", ultimo_error="")
    return f"{olt}: {resultado}"


@shared_task  # sin reintentos: tocar puertos PON dos veces no tiene sentido
def accion_olt_task(olt_id, accion, kwargs=None):
    """Paso 19: órdenes de la pestaña Puertos PON (leer, habilitar/autofind, reiniciar ONUs)."""
    olt = OLT.objects.get(pk=olt_id)
    try:
        resultado = servicios.ACCIONES_OLT[accion][0](olt, **(kwargs or {}))
    except Exception as e:
        OLT.objects.filter(pk=olt_id).update(accion_pendiente="", ultimo_error=f"{accion}: {type(e).__name__}: {e}"[:2000])
        raise
    OLT.objects.filter(pk=olt_id).update(accion_pendiente="", ultimo_error="", conectada=True)
    return f"{olt}: {resultado}"


# ---- Funciones para usar desde el admin / vistas: ponen la orden en la fila correcta
def encolar_busqueda(olt):
    return buscar_onus_task.apply_async(args=[olt.pk], queue=cola_de(olt))


def encolar_autorizacion(onu):
    """
    Paso 14: marca la ONU 'autorizando' ANTES de ponerla en la fila. Así un segundo clic
    (o dos personas a la vez) no mete otra orden para la misma ONU.
    Si quedó trabada en 'autorizando' más de ONU.MINUTOS_TRABADA minutos, se puede volver a enviar.
    """
    from datetime import timedelta
    from django.db.models import Q
    from django.utils import timezone
    ahora = timezone.now()
    limite = ahora - timedelta(minutes=ONU.MINUTOS_TRABADA)
    marcado = (ONU.objects.filter(pk=onu.pk)
               .filter(Q(estado="sin_autorizar") | Q(estado="autorizando", actualizado__lt=limite))
               .update(estado="autorizando", actualizado=ahora))
    if not marcado:
        raise ValueError(f"{onu.sn} ya tiene una autorización en la fila; espera a que termine.")
    try:
        return autorizar_onu_task.apply_async(args=[onu.pk], queue=cola_de(onu.olt))
    except Exception:
        ONU.objects.filter(pk=onu.pk).update(estado="sin_autorizar")
        raise


def encolar_baja(onu):
    return baja_onu_task.apply_async(args=[onu.pk], queue=cola_de(onu.olt))


def encolar_accion_onu(onu, accion):
    """Marca la ONU como ocupada (evita doble clic) y pone la orden en la fila de su OLT."""
    if accion not in servicios.ACCIONES_ONU:
        raise ValueError(f"Acción no válida: {accion}")
    marcado = ONU.objects.filter(pk=onu.pk, accion_pendiente="").update(accion_pendiente=accion)
    if not marcado:
        raise ValueError(f"{onu.sn} ya tiene una orden en proceso; espera unos segundos.")
    try:
        return accion_onu_task.apply_async(args=[onu.pk, accion], queue=cola_de(onu.olt))
    except Exception:
        ONU.objects.filter(pk=onu.pk).update(accion_pendiente="")
        raise


def encolar_info_olt(olt):
    """Marca la OLT como ocupada (evita doble clic) y pone 'Actualizar info' en su fila."""
    from django.utils import timezone
    from datetime import timedelta
    from django.db.models import Q
    libre = Q(accion_pendiente="") | Q(accion_desde__lt=timezone.now() - timedelta(minutes=OLT.MINUTOS_TRABADA))
    marcado = OLT.objects.filter(libre, pk=olt.pk).update(
        accion_pendiente="info", accion_desde=timezone.now())
    if not marcado:
        raise ValueError(f"{olt} ya tiene una lectura en proceso; espera unos segundos.")
    try:
        return info_olt_task.apply_async(args=[olt.pk], queue=cola_de(olt))
    except Exception:
        OLT.objects.filter(pk=olt.pk).update(accion_pendiente="")
        raise


def encolar_accion_olt(olt, accion, **kwargs):
    """Paso 19: marca la OLT ocupada (evita doble clic) y pone la orden en su fila."""
    from django.utils import timezone
    if accion not in servicios.ACCIONES_OLT:
        raise ValueError(f"Acción no válida: {accion}")
    if servicios.ACCIONES_OLT[accion][1] and olt.solo_lectura:
        raise ValueError(f"{olt} está en modo solo lectura: esto se hace desde SmartOLT.")
    from datetime import timedelta
    from django.db.models import Q
    libre = Q(accion_pendiente="") | Q(accion_desde__lt=timezone.now() - timedelta(minutes=OLT.MINUTOS_TRABADA))
    marcado = OLT.objects.filter(libre, pk=olt.pk).update(
        accion_pendiente=accion, accion_desde=timezone.now())
    if not marcado:
        raise ValueError(f"{olt} ya tiene una orden en proceso; espera unos segundos.")
    try:
        return accion_olt_task.apply_async(args=[olt.pk, accion, kwargs], queue=cola_de(olt))
    except Exception:
        OLT.objects.filter(pk=olt.pk).update(accion_pendiente="")
        raise
