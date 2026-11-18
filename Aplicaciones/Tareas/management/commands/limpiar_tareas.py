"""
Paso 14. Limpia órdenes que quedaron trabadas (por ejemplo después de vaciar una fila con
"celery purge" o de cerrar un worker a la fuerza):

  - En "Tareas en cola": las que siguen En cola / Ejecutando / Reintentando desde hace más de
    --minutos pasan a Error ("Cancelada").
  - Si esa fila ya no tiene nada pendiente, se liberan los equipos y ONUs/clientes de esa fila
    (ONUs en "Autorizando", botones en "procesando...").

Uso (desde la carpeta del proyecto):
    py manage.py limpiar_tareas --cola olt_2           <- solo esa fila
    py manage.py limpiar_tareas                        <- todas las filas
    py manage.py limpiar_tareas --cola olt_2 --minutos 0   <- todo lo de esa fila, ya
    py manage.py limpiar_tareas --ver                  <- solo muestra, no cambia nada

Úsalo con el worker de esa fila APAGADO o después de "purge": si el worker está trabajando,
una orden que de verdad está corriendo no debe darse por cancelada.
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from Aplicaciones.ServerMikro.models import Cliente, Router
from Aplicaciones.ServerOLT.models import OLT, ONU
from Aplicaciones.Tareas.models import Tarea

ABIERTAS = ("en_cola", "ejecutando", "reintento")


class Command(BaseCommand):
    help = "Marca como canceladas las tareas trabadas y libera ONUs/equipos de esa fila."

    def add_arguments(self, parser):
        parser.add_argument("--cola", default="", help="Ej: olt_2 o mk_4. Vacío = todas.")
        parser.add_argument("--minutos", type=int, default=10,
                            help="Solo tareas abiertas desde hace más de estos minutos (def. 10).")
        parser.add_argument("--ver", action="store_true", help="Solo mostrar, sin cambiar nada.")

    def handle(self, cola, minutos, ver, **_):
        ahora = timezone.now()
        trabadas = Tarea.objects.filter(estado__in=ABIERTAS, creada__lte=ahora - timedelta(minutes=minutos))
        if cola:
            trabadas = trabadas.filter(cola=cola)

        for t in trabadas:
            self.stdout.write(f"  {t.creada:%d/%m %H:%M}  {t.cola:8} {t.get_estado_display():13} {t.descripcion}")
        if not trabadas.exists():
            self.stdout.write("No hay tareas trabadas.")
        if ver:
            return

        n = trabadas.update(estado="error", terminada=ahora,
                            resultado="Cancelada: se limpió con 'limpiar_tareas' "
                                      "(fila vaciada o worker reiniciado).")
        if n:
            self.stdout.write(self.style.SUCCESS(f"{n} tarea(s) marcadas como canceladas."))

        # Liberar lo que esas órdenes dejaron "ocupado", solo en filas que ya no tienen nada abierto
        filas = [cola] if cola else sorted({f"olt_{pk}" for pk in OLT.objects.values_list("pk", flat=True)} |
                                           {f"mk_{pk}" for pk in Router.objects.values_list("pk", flat=True)})
        for fila in filas:
            if Tarea.objects.filter(cola=fila, estado__in=ABIERTAS).exists():
                self.stdout.write(f"  {fila}: todavía tiene tareas recientes abiertas; no se libera nada.")
                continue
            tipo, _, num = fila.partition("_")
            if not num.isdigit():
                continue
            pk = int(num)
            if tipo == "olt":
                a = ONU.objects.filter(olt_id=pk, estado="autorizando").update(estado="sin_autorizar")
                b = ONU.objects.filter(olt_id=pk).exclude(accion_pendiente="").update(accion_pendiente="")
                c = OLT.objects.filter(pk=pk).exclude(accion_pendiente="").update(accion_pendiente="")
                total = a + b + c
            elif tipo == "mk":
                b = Cliente.objects.filter(router_id=pk).exclude(accion_pendiente="").update(accion_pendiente="")
                c = Router.objects.filter(pk=pk).exclude(accion_pendiente="").update(accion_pendiente="")
                total = b + c
            else:
                continue
            if total:
                self.stdout.write(self.style.SUCCESS(f"  {fila}: {total} registro(s) liberados."))
