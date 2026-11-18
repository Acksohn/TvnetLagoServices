"""
Tareas en cola para el MikroTik. Cada router tiene su propia fila: "mk_<id>".
Igual que con las OLTs: un worker con --pool=solo atiende la fila y hace
UNA orden a la vez, así el router nunca recibe una avalancha de peticiones.
"""
from celery import shared_task

from . import servicios
from .models import Cliente


def cola_de(router):
    """Nombre de la fila de un router. Ej: router con id 1 -> 'mk_1'."""
    return f"mk_{router.pk}"


@shared_task
def accion_cliente_task(cliente_id, accion):
    cliente = Cliente.objects.select_related("router").get(pk=cliente_id)
    try:
        servicios.ACCIONES[accion](cliente)
    except Exception as e:
        # Guardamos el error para verlo en la web, y liberamos al cliente
        Cliente.objects.filter(pk=cliente_id).update(ultimo_error=f"{accion}: {e}",
                                                     accion_pendiente="")
        raise
    Cliente.objects.filter(pk=cliente_id).update(ultimo_error="", accion_pendiente="")
    return f"{cliente}: {accion} OK"


def encolar(cliente, accion):
    """
    Pone la orden en la fila del router del cliente.
    Marca 'accion_pendiente' para que nadie mande otra orden al mismo cliente
    mientras esta no termine (evita doble clic / dos técnicos a la vez).
    """
    if accion not in servicios.ACCIONES:
        raise ValueError(f"Acción no válida: {accion}")
    marcado = (Cliente.objects.filter(pk=cliente.pk, accion_pendiente="")
               .update(accion_pendiente=accion))
    if not marcado:
        raise ValueError(f"{cliente} ya tiene una orden en proceso; espera unos segundos.")
    try:
        return accion_cliente_task.apply_async(args=[cliente.pk, accion],
                                               queue=cola_de(cliente.router))
    except Exception:
        # Redis apagado: deshacemos la marca para que se pueda reintentar
        Cliente.objects.filter(pk=cliente.pk).update(accion_pendiente="")
        raise
