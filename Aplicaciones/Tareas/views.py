from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.shortcuts import render
from django.utils import timezone

from .models import Tarea

PENDIENTES = ("en_cola", "ejecutando", "reintento")
ESPERA_SOSPECHOSA = timedelta(minutes=2)  # si lleva más que esto en cola, ¿el worker está apagado?


@login_required
def lista(request):
    ahora = timezone.now()
    hoy = timezone.localtime(ahora).replace(hour=0, minute=0, second=0, microsecond=0)

    estado = request.GET.get("estado", "")
    cola = request.GET.get("cola", "")
    busqueda = request.GET.get("q", "").strip()

    resumen = Tarea.objects.aggregate(
        pendientes=Count("id", filter=Q(estado__in=PENDIENTES)),
        ok_hoy=Count("id", filter=Q(estado="ok", creada__gte=hoy)),
        error_hoy=Count("id", filter=Q(estado="error", creada__gte=hoy)),
    )

    # Filas (colas) con lo que tienen pendiente: una tarjeta por equipo
    colas = (Tarea.objects.filter(creada__gte=ahora - timedelta(days=7)).exclude(cola="")
             .values("cola")
             .annotate(pendientes=Count("id", filter=Q(estado__in=PENDIENTES)),
                       errores=Count("id", filter=Q(estado="error", creada__gte=hoy)))
             .order_by("cola"))

    tareas = Tarea.objects.all()
    if estado == "pendientes":
        tareas = tareas.filter(estado__in=PENDIENTES)
    elif estado in ("ok", "error"):
        tareas = tareas.filter(estado=estado)
    if cola:
        tareas = tareas.filter(cola=cola)
    if busqueda:
        tareas = tareas.filter(Q(descripcion__icontains=busqueda) | Q(resultado__icontains=busqueda))

    pagina = Paginator(tareas, 50).get_page(request.GET.get("page"))
    for t in pagina:  # marca las que esperan demasiado (worker apagado)
        t.atascada = t.estado == "en_cola" and ahora - t.creada > ESPERA_SOSPECHOSA

    return render(request, "tareas/lista.html", {
        "pagina": pagina, "resumen": resumen, "colas": colas,
        "estado": estado, "cola": cola, "busqueda": busqueda,
    })
