"""
Registro automático de tareas usando las señales de Celery.
No hay que cambiar ninguna tarea: cada vez que algo se pone en la fila,
empieza, termina o falla, aquí se anota en la tabla Tarea.

Regla: si el registro falla, NUNCA debe romper la tarea real. Por eso todo va en try/except.
"""
import logging

from celery.signals import (after_task_publish, task_failure, task_prerun,
                            task_retry, task_success)
from django.db.models import F
from django.urls import reverse
from django.utils import timezone

from .models import Tarea

log = logging.getLogger(__name__)

VERBOS = {"crear": "Crear en MikroTik", "suspender": "Suspender", "activar": "Activar",
          "plan": "Cambiar plan"}
VERBOS_ONU = {"senal": "Leer señal", "reiniciar": "Reiniciar", "desactivar": "Suspender",
              "activar": "Reactivar", "baja": "Eliminar",
              "velocidad": "Cambiar velocidad"}  # paso 18


def _corto(nombre):
    """'Aplicaciones.ServerOLT.tasks.buscar_onus_task' -> 'buscar_onus_task'"""
    return (nombre or "").rsplit(".", 1)[-1]


def describir(nombre, args):
    """Convierte (tarea, argumentos) en un texto que entiende una persona, y un enlace."""
    from Aplicaciones.ServerMikro.models import Cliente
    from Aplicaciones.ServerOLT.models import OLT, ONU
    try:
        if nombre == "buscar_onus_task":
            olt = OLT.objects.get(pk=args[0])
            return f"Buscar ONUs nuevas en {olt}", reverse("olt:sin_configurar") + f"?olt={olt.pk}"
        if nombre in ("autorizar_onu_task", "baja_onu_task"):
            onu = ONU.objects.select_related("cliente", "olt").get(pk=args[0])
            verbo = "Autorizar ONU" if nombre == "autorizar_onu_task" else "Dar de baja ONU"
            quien = f" · {onu.cliente.nombre}" if onu.cliente else ""
            enlace = reverse("clientes:detalle", args=[onu.cliente_id]) if onu.cliente_id else ""
            return f"{verbo} {onu.sn} en {onu.olt}{quien}", enlace
        if nombre == "info_router_task":
            from Aplicaciones.ServerMikro.models import Router
            r = Router.objects.get(pk=args[0])
            return f"Probar conexión del MikroTik {r.nombre}", reverse("clientes:router_detalle", args=[r.pk])
        if nombre == "info_olt_task":
            olt = OLT.objects.get(pk=args[0])
            return f"Actualizar info de {olt} (conexión, versión y tarjetas)", reverse("olt:detalle_olt", args=[olt.pk])
        if nombre == "accion_olt_task":  # paso 19
            olt = OLT.objects.get(pk=args[0])
            kw = args[2] if len(args) > 2 and isinstance(args[2], dict) else {}
            if args[1] == "puertos":
                verbo = "Leer puertos PON"
            elif args[1] == "perfiles_ont":
                return f"Leer line/service profiles de {olt}", reverse("olt:perfiles_ont", args=[olt.pk])
            elif args[1] == "vlans":
                return f"Leer VLANs de {olt}", reverse("olt:vlans", args=[olt.pk])
            elif args[1] == "crear_vlan":
                return f"Crear VLANs en {olt}", reverse("olt:vlans", args=[olt.pk])
            elif args[1] == "reset_pon":
                verbo = f"Reiniciar ONUs del puerto {kw.get('frame', 0)}/{kw.get('slot', '?')}/{kw.get('puerto', '?')}"
            else:
                verbo = "Cambiar puertos PON"
            return f"{verbo} en {olt}", reverse("olt:puertos_pon", args=[olt.pk])
        if nombre == "accion_onu_task":
            onu = ONU.objects.select_related("cliente", "olt").get(pk=args[0])
            verbo = VERBOS_ONU.get(args[1], args[1])
            quien = f" · {onu.cliente.nombre}" if onu.cliente else ""
            return f"{verbo} ONU {onu.sn} en {onu.olt}{quien}", reverse("olt:detalle_onu", args=[onu.pk])
        if nombre == "accion_cliente_task":
            c = Cliente.objects.select_related("plan").get(pk=args[0])
            verbo = VERBOS.get(args[1], args[1])
            if args[1] == "plan" and c.plan:
                verbo = f"Cambiar a {c.plan.nombre} ({c.plan.velocidad})"
            return f"{verbo}: {c.nombre} ({c.usuario_pppoe})", reverse("clientes:detalle", args=[c.pk])
    except Exception:
        pass
    return f"{nombre} {list(args)}", ""


def _args_de(body):
    # Celery (protocolo 2) manda el cuerpo como (args, kwargs, extras)
    if isinstance(body, (list, tuple)) and body and isinstance(body[0], (list, tuple)):
        return list(body[0])
    return []


@after_task_publish.connect
def al_poner_en_fila(sender=None, headers=None, body=None, routing_key=None, **_):
    """Corre en Django, justo cuando la orden entra a Redis."""
    try:
        nombre = _corto(sender)
        descripcion, enlace = describir(nombre, _args_de(body))
        Tarea.objects.update_or_create(
            task_id=headers["id"],
            defaults={"nombre": nombre, "descripcion": descripcion[:200], "enlace": enlace,
                      "cola": routing_key or "", "estado": "en_cola"})
    except Exception:
        log.exception("No se pudo registrar la tarea en cola")


@task_prerun.connect
def al_empezar(task_id=None, task=None, args=None, **_):
    """Corre en el worker, cuando toma la orden de la fila."""
    try:
        ahora = timezone.now()
        filas = Tarea.objects.filter(task_id=task_id).update(
            estado="ejecutando", iniciada=ahora, intentos=F("intentos") + 1)
        if not filas:  # se puso en la fila antes de instalar este registro
            nombre = _corto(task.name)
            descripcion, enlace = describir(nombre, args or [])
            cola = (task.request.delivery_info or {}).get("routing_key", "")
            Tarea.objects.create(task_id=task_id, nombre=nombre, descripcion=descripcion[:200],
                                 enlace=enlace, cola=cola, estado="ejecutando",
                                 iniciada=ahora, intentos=1)
    except Exception:
        log.exception("No se pudo registrar el inicio de la tarea")


@task_success.connect
def al_terminar_bien(sender=None, result=None, **_):
    try:
        Tarea.objects.filter(task_id=sender.request.id).update(
            estado="ok", resultado=str(result or "")[:2000], terminada=timezone.now())
    except Exception:
        log.exception("No se pudo registrar el fin de la tarea")


@task_failure.connect
def al_fallar(task_id=None, exception=None, **_):
    try:
        Tarea.objects.filter(task_id=task_id).update(
            estado="error", resultado=str(exception)[:2000], terminada=timezone.now())
    except Exception:
        log.exception("No se pudo registrar el error de la tarea")


@task_retry.connect
def al_reintentar(request=None, reason=None, **_):
    try:
        Tarea.objects.filter(task_id=request.id).update(
            estado="reintento", resultado=f"Reintentando: {reason}"[:2000])
    except Exception:
        log.exception("No se pudo registrar el reintento")
