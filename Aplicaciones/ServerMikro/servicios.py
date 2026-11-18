# Aquí vive la lógica de "qué hacer en el MikroTik".
# Estas funciones las ejecuta el WORKER de Celery (tasks.py), nunca la página web.
# (La autorización de ONUs también llama a crear_en_mikrotik, dentro del worker de la OLT.)


def crear_en_mikrotik(cliente):
    from .models import red_de
    red = red_de(cliente.router_id, cliente.ip)  # paso 15: si su IP es de una Red IPv4 con gateway
    with cliente.router.conectar() as mk:
        mk.crear_cliente(cliente.usuario_pppoe, cliente.clave_pppoe,
                         cliente.ip, cliente.velocidad, cliente.nombre,
                         local=red.gateway if red and red.gateway else None)
    cliente.estado = "activo"
    cliente.save(update_fields=["estado", "actualizado"])


def suspender(cliente):
    with cliente.router.conectar() as mk:
        mk.suspender_cliente(cliente.usuario_pppoe)
    cliente.estado = "suspendido"
    cliente.save(update_fields=["estado", "actualizado"])


def activar(cliente):
    with cliente.router.conectar() as mk:
        mk.activar_cliente(cliente.usuario_pppoe)
    cliente.estado = "activo"
    cliente.save(update_fields=["estado", "actualizado"])


def cambiar_plan(cliente):
    """Aplica en el MikroTik la velocidad del plan que el cliente tiene en el sistema."""
    if not cliente.plan:
        raise ValueError(f"{cliente} no tiene plan asignado")
    with cliente.router.conectar() as mk:
        mk.cambiar_velocidad(cliente.usuario_pppoe, cliente.plan.velocidad)
    cliente.velocidad = cliente.plan.velocidad
    cliente.save(update_fields=["velocidad", "actualizado"])


# Las únicas acciones permitidas, y desde qué estado se puede hacer cada una
ACCIONES = {
    "crear": crear_en_mikrotik,
    "suspender": suspender,
    "activar": activar,
    "plan": cambiar_plan,
}
ESTADO_REQUERIDO = {
    "crear": ("pendiente",),
    "suspender": ("activo",),
    "activar": ("suspendido",),
    "plan": ("activo", "suspendido"),
}


# ---------------------------------------------------------------- info del router (paso 11)

def leer_info_router(router):
    """Solo lectura: /system/resource + cuántos PPPoE hay conectados."""
    from datetime import timedelta

    from django.utils import timezone

    from .mikrotik import segundos_uptime
    with router.conectar() as mk:
        r = mk.info()
        activos = mk.ppp_conectados()
        huella = getattr(mk, "huella_vista", "")

    def pct(libre, total):
        try:
            return round(int(libre) * 100 / int(total))
        except (TypeError, ValueError, ZeroDivisionError):
            return None

    ahora = timezone.now()
    seg = segundos_uptime(r.get("uptime"))
    router.modelo = str(r.get("board-name") or "")[:60]
    router.encendido_desde = ahora - timedelta(seconds=seg) if seg else None
    router.cpu = int(r["cpu-load"]) if str(r.get("cpu-load", "")).isdigit() else None
    router.version = str(r.get("version") or "")[:40]
    router.memoria_libre = pct(r.get("free-memory"), r.get("total-memory"))
    router.ppp_activos = activos
    router.conectado, router.info_leida = True, ahora
    router.huella_vista = huella or ""
    router.save(update_fields=["huella_vista", "modelo", "version", "encendido_desde", "cpu", "memoria_libre",
                               "ppp_activos", "conectado", "info_leida"])
    return f"{router.modelo} RouterOS {router.version}, {activos} PPPoE conectados"
