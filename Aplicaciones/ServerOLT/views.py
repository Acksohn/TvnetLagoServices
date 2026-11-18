"""
Vistas web del panel (fuera del admin), estilo SmartOLT.

Regla de oro: estas vistas NUNCA hablan con la OLT. Solo leen la base de datos
y ponen órdenes en la fila de Celery (tasks.encolar_*). El worker de cada OLT
es el único que entra por SSH, una orden a la vez.
"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.paginator import Paginator
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError
from django.db.models import Avg, Count, Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from . import servicios, tasks
from .forms import (AutorizarONUForm, OLTForm, PerfilVelocidadForm, PlanOLTForm, VelocidadONUForm, VLANForm,
                    VLANMultipleForm)
from .models import OLT, ONU, VLAN, PerfilVelocidad, PlanOLT, PuertoPON

AVISO_REDIS = ("No se pudo poner la orden en la fila. ¿Está encendido Redis? "
               "(en Ubuntu/WSL: sudo service redis-server start). Detalle: ")

# Estados que se muestran en "Sin configurar"
PENDIENTES = ("sin_autorizar", "autorizando")


def _volver_a_lista(olt_id=None):
    url = reverse("olt:sin_configurar")
    return redirect(f"{url}?olt={olt_id}" if olt_id else url)


# ------------------------------------------------------------ Sin configurar

@login_required
def sin_configurar(request):
    """Lista de ONUs detectadas por autofind que aún no están autorizadas."""
    olts = (OLT.objects.filter(activa=True)
            .annotate(pendientes=Count("onus", filter=Q(onus__estado__in=PENDIENTES)))
            .order_by("nombre"))

    onus = (ONU.objects.filter(estado__in=PENDIENTES)
            .select_related("olt")
            .order_by("olt__nombre", "frame", "slot", "puerto", "sn"))

    olt_id = request.GET.get("olt", "")
    busqueda = request.GET.get("q", "").strip()
    if olt_id.isdigit():
        onus = onus.filter(olt_id=int(olt_id))
    if busqueda:
        onus = onus.filter(Q(sn__icontains=busqueda) | Q(modelo__icontains=busqueda))

    contexto = {
        "olts": olts,
        "onus": onus,
        "olt_id": olt_id,
        "busqueda": busqueda,
        "total_pendientes": sum(o.pendientes for o in olts),
        "hay_planes": PlanOLT.objects.exists(),
    }
    return render(request, "olt/sin_configurar.html", contexto)


@login_required
@permission_required("ServerOLT.change_onu", raise_exception=True)
@require_POST
def buscar(request, olt_id=None):
    """Pone 'buscar ONUs nuevas' en la fila de una OLT (o de todas)."""
    if olt_id:
        olts = [get_object_or_404(OLT, pk=olt_id, activa=True)]
    else:
        olts = list(OLT.objects.filter(activa=True))
    for olt in olts:
        try:
            tasks.encolar_busqueda(olt)
        except Exception as e:
            messages.error(request, AVISO_REDIS + str(e))
            return _volver_a_lista(olt_id)
    nombres = ", ".join(o.nombre for o in olts) or "ninguna OLT activa"
    messages.info(request, f"Buscando ONUs nuevas en {nombres}. "
                           f"La lista se actualiza sola en unos segundos.")
    return _volver_a_lista(olt_id)


# ------------------------------------------------------------ ONUs configuradas

# Umbrales de señal RX en la ONU (dBm), típicos de GPON clase B+/C+
SENAL_DEBIL = -25    # de -25 a -27: aviso
SENAL_CRITICA = -27  # menor a -27: crítica (cortes, lentitud)


@login_required
def configuradas(request):
    """ONUs ya autorizadas (tienen ONT-ID), con filtros como en SmartOLT 'Configured'."""
    autorizadas = ONU.objects.filter(ont_id__isnull=False).exclude(estado__in=PENDIENTES)

    olts = (OLT.objects.filter(activa=True)
            .annotate(total=Count("onus", filter=Q(onus__ont_id__isnull=False)
                                  & ~Q(onus__estado__in=PENDIENTES)))
            .order_by("nombre"))

    olt_id = request.GET.get("olt", "")
    estado = request.GET.get("estado", "")
    pon = request.GET.get("pon", "")
    senal = request.GET.get("senal", "")
    busqueda = request.GET.get("q", "").strip()

    # 1) Filtro por OLT: afecta también a los contadores de arriba
    if olt_id.isdigit():
        autorizadas = autorizadas.filter(olt_id=int(olt_id))

    resumen = {
        "total": autorizadas.count(),
        "activas": autorizadas.filter(estado="activa").count(),
        "suspendidas": autorizadas.filter(estado="suspendida").count(),
        "error": autorizadas.filter(estado="error").count(),
        "senal_baja": autorizadas.filter(rx_power__lt=SENAL_CRITICA).count(),
    }

    # Puertos PON con ONUs (solo si hay una OLT elegida, para no mezclar OLTs)
    puertos = []
    if olt_id.isdigit():
        puertos = (autorizadas.values_list("frame", "slot", "puerto")
                   .distinct().order_by("frame", "slot", "puerto"))
        puertos = [f"{f}/{s}/{p}" for f, s, p in puertos]

    # 2) Los demás filtros solo afectan a la tabla
    onus = autorizadas.select_related("olt", "plan", "cliente")
    if estado in ("activa", "suspendida", "error"):
        onus = onus.filter(estado=estado)
    if pon in puertos:
        f, s, p = (int(x) for x in pon.split("/"))
        onus = onus.filter(frame=f, slot=s, puerto=p)
    if senal == "baja":
        onus = onus.filter(rx_power__lt=SENAL_CRITICA)
    if busqueda:
        onus = onus.filter(
            Q(sn__icontains=busqueda) | Q(descripcion__icontains=busqueda)
            | Q(cliente__nombre__icontains=busqueda)
            | Q(cliente__usuario_pppoe__icontains=busqueda))
    onus = onus.order_by("olt__nombre", "frame", "slot", "puerto", "ont_id")

    pagina = Paginator(onus, 50).get_page(request.GET.get("page"))

    return render(request, "olt/configuradas.html", {
        "olts": olts, "olt_id": olt_id, "estado": estado, "pon": pon, "senal": senal,
        "busqueda": busqueda, "puertos": puertos, "resumen": resumen, "pagina": pagina,
        "senal_debil": SENAL_DEBIL, "senal_critica": SENAL_CRITICA,
    })


# ------------------------------------------------------------ Autorizar

@login_required
@permission_required("ServerOLT.change_onu", raise_exception=True)
def autorizar(request, pk):
    """Formulario 'Autorizar ONU': elegir plan, cliente y descripción, y encolar."""
    onu = get_object_or_404(ONU.objects.select_related("olt"), pk=pk)
    if onu.olt.solo_lectura:
        messages.warning(request, f"{onu.olt} está en modo solo lectura: autorízala desde SmartOLT.")
        return _volver_a_lista(onu.olt_id)
    if onu.estado != "sin_autorizar" and not onu.autorizando_trabada:
        messages.warning(request, f"{onu.sn} ya no está sin autorizar "
                                  f"(estado: {onu.get_estado_display()}).")
        return _volver_a_lista(onu.olt_id)

    form = AutorizarONUForm(request.POST or None, instance=onu)
    if request.method == "POST" and form.is_valid():
        if onu.autorizando_trabada:  # paso 14: la orden anterior se perdió (fila vaciada, worker caído)
            ONU.objects.filter(pk=onu.pk, estado="autorizando").update(estado="sin_autorizar")
        onu = form.save(commit=False)
        onu.ultimo_error = ""
        # Guardamos SOLO estos campos: el estado lo maneja el worker
        onu.save(update_fields=["plan", "vlan", "lineprofile_id", "srvprofile_id", "gemport", "modo", "vel_bajada", "vel_subida", "cliente", "descripcion",
                                "ultimo_error", "actualizado"])
        try:
            form.guardar_nap(onu.cliente)  # paso 17: caja/puerto NAP del cliente
        except IntegrityError:
            form.add_error("puerto_nap", "Ese puerto NAP lo acaban de ocupar. Elige otro.")
            return render(request, "olt/autorizar.html", {"onu": onu, "form": form})
        try:
            tasks.encolar_autorizacion(onu)
        except ValueError as e:  # paso 14: ya hay una autorización en la fila (doble clic)
            messages.warning(request, str(e))
            return _volver_a_lista(onu.olt_id)
        except Exception as e:
            messages.error(request, AVISO_REDIS + str(e))
            return redirect("olt:autorizar", pk=onu.pk)
        messages.success(request, f"{onu.sn}: autorización en cola en {onu.olt}. "
                                  f"En unos segundos pasará a Activa.")
        return _volver_a_lista(onu.olt_id)

    return render(request, "olt/autorizar.html", {"onu": onu, "form": form})


# ------------------------------------------------------------ Ficha de la ONU (paso 9)

TEXTO_ACCION = {
    "senal": "lectura de señal", "reiniciar": "reinicio", "desactivar": "suspensión de la ONU",
    "activar": "reactivación de la ONU", "baja": "eliminación de la ONU en la OLT",
    "velocidad": "cambio de velocidad",  # paso 18
}


@login_required
def detalle_onu(request, pk):
    onu = get_object_or_404(ONU.objects.select_related("olt", "plan", "cliente", "cliente__router", "cliente__plan"), pk=pk)
    if onu.ont_id is None and onu.estado in PENDIENTES:
        return redirect("olt:autorizar", pk=onu.pk)  # todavía no está autorizada
    if onu.ont_id is None and onu.estado == "no_detectada":
        messages.info(request, f"{onu.sn} no está autorizada y ya no aparece en el autofind de {onu.olt}. "
                               f"Si la conectas de nuevo, pulsa Buscar y saldrá aquí.")
        return redirect("olt:sin_configurar")
    return render(request, "olt/detalle_onu.html", {
        "onu": onu, "senal_debil": SENAL_DEBIL, "senal_critica": SENAL_CRITICA,
        "form_vel": VelocidadONUForm(instance=onu),
    })


@login_required
@permission_required("ServerOLT.change_onu", raise_exception=True)
@require_POST
def onu_velocidad(request, pk):
    """Paso 18: guarda la velocidad elegida y pone 'cambiar velocidad' en la fila de la OLT."""
    onu = get_object_or_404(ONU.objects.select_related("olt"), pk=pk)
    if onu.olt.solo_lectura:
        messages.warning(request, f"{onu.olt} está en modo solo lectura: cambia la velocidad desde SmartOLT.")
        return redirect("olt:detalle_onu", pk=onu.pk)
    if onu.estado not in servicios.ACCIONES_ONU["velocidad"][1]:
        messages.warning(request, f"{onu.sn} está {onu.get_estado_display()}: no se puede cambiar la velocidad.")
        return redirect("olt:detalle_onu", pk=onu.pk)
    if onu.accion_pendiente:
        messages.warning(request, f"{onu.sn} ya tiene una orden en proceso; espera unos segundos.")
        return redirect("olt:detalle_onu", pk=onu.pk)
    form = VelocidadONUForm(request.POST, instance=onu)
    if not form.is_valid():
        messages.error(request, "Velocidad no válida: " + "; ".join(e for l in form.errors.values() for e in l))
        return redirect("olt:detalle_onu", pk=onu.pk)
    if not form.has_changed():
        messages.info(request, "No cambiaste nada.")
        return redirect("olt:detalle_onu", pk=onu.pk)
    form.save()
    try:
        tasks.encolar_accion_onu(onu, "velocidad")
        messages.success(request, f"{onu.sn}: cambio de velocidad en la fila de {onu.olt}. "
                                  f"El cliente puede perder la conexión 1 o 2 segundos.")
    except ValueError as e:
        messages.warning(request, str(e))
    except Exception as e:
        messages.error(request, AVISO_REDIS + str(e))
    return redirect("olt:detalle_onu", pk=onu.pk)


@login_required
@permission_required("ServerOLT.change_onu", raise_exception=True)
@require_POST
def accion_onu(request, pk, accion):
    if accion not in servicios.ACCIONES_ONU:
        raise Http404("Acción no válida")
    onu = get_object_or_404(ONU.objects.select_related("olt"), pk=pk)
    estados_validos = servicios.ACCIONES_ONU[accion][1]

    if accion != "senal" and onu.olt.solo_lectura:
        messages.warning(request, f"{onu.olt} está en modo solo lectura: hazlo desde SmartOLT.")
    elif onu.estado not in estados_validos:
        messages.warning(request, f"{onu.sn} está {onu.get_estado_display()}: "
                                  f"no se puede hacer la {TEXTO_ACCION[accion]}.")
    else:
        try:
            tasks.encolar_accion_onu(onu, accion)
            messages.success(request, f"{onu.sn}: {TEXTO_ACCION[accion]} en la fila de {onu.olt}.")
        except ValueError as e:
            messages.warning(request, str(e))
        except Exception as e:
            messages.error(request, AVISO_REDIS + str(e))

    if accion == "baja":
        messages.info(request, f"Cuando termine, {onu.sn} volverá sola a «Sin configurar» "
                               f"(se busca de nuevo en unos 40 segundos), lista para autorizarla otra vez.")
        return redirect("olt:configuradas")
    return redirect("olt:detalle_onu", pk=onu.pk)


# ------------------------------------------------------------ OLTs (paso 10)

ESPERA_WORKER = 120  # segundos: si una lectura lleva más que esto en la fila, probablemente no hay worker


def _con_conteos(qs):
    configurada = Q(onus__ont_id__isnull=False) & ~Q(onus__estado__in=PENDIENTES)
    return qs.annotate(
        total=Count("onus", filter=configurada),
        activas=Count("onus", filter=configurada & Q(onus__estado="activa")),
        suspendidas=Count("onus", filter=configurada & Q(onus__estado="suspendida")),
        fuera=Count("onus", filter=configurada & Q(onus__en_linea=False)),
        criticas=Count("onus", filter=configurada & Q(onus__rx_power__lt=SENAL_CRITICA)),
        pendientes=Count("onus", filter=Q(onus__estado="sin_autorizar")),
    )


def _sin_worker(olt):
    from django.utils import timezone
    return bool(olt.accion_pendiente and olt.accion_desde
                and (timezone.now() - olt.accion_desde).total_seconds() > ESPERA_WORKER)


@login_required
def olts(request):
    lista = list(_con_conteos(OLT.objects.all()).order_by("-activa", "nombre"))
    for o in lista:
        o.liberar_si_trabada()
        o.cola = tasks.cola_de(o)
        o.sin_worker = _sin_worker(o)
    return render(request, "olt/olts.html", {
        "olts": lista,
        "resumen": {
            "olts": len(lista),
            "conectadas": sum(1 for o in lista if o.conectada),
            "sin_conexion": sum(1 for o in lista if o.conectada is False),
            "onus": sum(o.total for o in lista),
        },
    })


@login_required
def detalle_olt(request, pk):
    olt = get_object_or_404(_con_conteos(OLT.objects.all()), pk=pk)
    olt.liberar_si_trabada()  # paso 20b: nada se queda girando para siempre
    configuradas_qs = olt.onus.filter(ont_id__isnull=False).exclude(estado__in=PENDIENTES)

    # Estadísticas por puerto PON, desde la base de datos (no se le pregunta a la OLT)
    filas = (configuradas_qs.values("frame", "slot", "puerto").annotate(
        total=Count("id"),
        activas=Count("id", filter=Q(estado="activa")),
        fuera=Count("id", filter=Q(en_linea=False)),
        criticas=Count("id", filter=Q(rx_power__lt=SENAL_CRITICA)),
        rx=Avg("rx_power")))
    por_puerto = {(f["frame"], f["slot"], f["puerto"]): f for f in filas}
    pendientes = dict(((f["slot"], f["puerto"]), f["n"]) for f in
                      olt.onus.filter(estado="sin_autorizar").values("slot", "puerto").annotate(n=Count("id")))

    # Puertos: todos los de las tarjetas GPON leídas + los que tengan ONUs aunque no se haya leído la tarjeta
    claves = set(por_puerto)
    for t in olt.tarjetas_pon:
        for p in range(t.get("puertos", 0)):
            claves.add((0, t["slot"], p))
    vacio = {"total": 0, "activas": 0, "fuera": 0, "criticas": 0, "rx": None}
    puertos = []
    for f, s, p in sorted(claves):
        d = dict(vacio, **por_puerto.get((f, s, p), {}))
        d.update(fsp=f"{f}/{s}/{p}", pendientes=pendientes.get((s, p), 0))
        puertos.append(d)

    ver_vacios = request.GET.get("vacios") == "1"
    con_onus = [p for p in puertos if p["total"] or p["pendientes"]]

    onus_por_slot = dict(configuradas_qs.values_list("slot").annotate(n=Count("id")))
    pon = {t["slot"]: t for t in olt.tarjetas_pon}  # paso 20b: reconoce también CGHF, XGHD…
    tarjetas = [dict(pon.get(t["slot"], t), onus=onus_por_slot.get(t["slot"], 0)) for t in (olt.tarjetas or [])]

    return render(request, "olt/detalle_olt.html", {
        "olt": olt, "tarjetas": tarjetas,
        "puertos": puertos if ver_vacios else con_onus,
        "total_puertos": len(puertos), "puertos_ocultos": len(puertos) - len(con_onus),
        "ver_vacios": ver_vacios,
        "cola": tasks.cola_de(olt), "sin_worker": _sin_worker(olt),
        "senal_debil": SENAL_DEBIL, "senal_critica": SENAL_CRITICA,
    })


@login_required
@require_POST
def info_olt(request, pk):
    """Pone 'Actualizar info' (probar conexión + versión + tarjetas) en la fila de la OLT."""
    if not request.user.has_perm("ServerOLT.view_olt") and not request.user.has_perm("ServerOLT.change_olt"):
        raise PermissionDenied
    olt = get_object_or_404(OLT, pk=pk)
    try:
        tasks.encolar_info_olt(olt)
        messages.info(request, f"{olt}: leyendo la información en la fila '{tasks.cola_de(olt)}'…")
    except ValueError as e:
        messages.warning(request, str(e))
    except Exception as e:
        messages.error(request, AVISO_REDIS + str(e))
    if request.POST.get("volver") == "lista":
        return redirect("olt:olts")
    return redirect("olt:detalle_olt", pk=olt.pk)


@login_required
def olt_form(request, pk=None):
    if not request.user.has_perm("ServerOLT.change_olt" if pk else "ServerOLT.add_olt"):
        raise PermissionDenied
    olt = get_object_or_404(OLT, pk=pk) if pk else None
    form = OLTForm(request.POST or None, instance=olt)
    if request.method == "POST" and form.is_valid():
        nueva = olt is None
        olt = form.save()
        if nueva:
            messages.success(request, f"{olt} creada. Abre su worker con el comando de abajo y pulsa "
                                      f"«Actualizar info» para probar la conexión.")
        else:
            messages.success(request, f"{olt} guardada.")
        return redirect("olt:detalle_olt", pk=olt.pk)
    return render(request, "olt/form_olt.html", {"form": form, "olt": olt})



# ------------------------------------------------------------ Planes OLT (paso 11)

@login_required
def planes_olt(request):
    planes = (PlanOLT.objects.select_related("olt").annotate(
        total=Count("onu", filter=Q(onu__ont_id__isnull=False)),
        activas=Count("onu", filter=Q(onu__estado="activa")),
    ).order_by("olt__nombre", "nombre"))
    olt_id = request.GET.get("olt", "")
    if olt_id.isdigit():
        planes = planes.filter(olt_id=int(olt_id))
    return render(request, "olt/planes_olt.html", {
        "planes": planes, "olt_id": olt_id, "olts": OLT.objects.order_by("nombre"),
        "sin_olt": PlanOLT.objects.filter(olt__isnull=True).count()})


@login_required
def plan_olt_form(request, pk=None):
    if not request.user.has_perm("ServerOLT.change_planolt" if pk else "ServerOLT.add_planolt"):
        raise PermissionDenied
    plan = get_object_or_404(PlanOLT, pk=pk) if pk else None
    inicial = {"olt": int(request.GET["olt"])} if str(request.GET.get("olt", "")).isdigit() else None
    form = PlanOLTForm(request.POST or None, instance=plan, initial=inicial)
    if request.method == "POST" and form.is_valid():
        plan = form.save()
        messages.success(request, f"Plan OLT «{plan.nombre}» guardado. Se usará en las próximas "
                                  f"autorizaciones; las ONUs ya autorizadas no cambian.")
        return redirect(f"{reverse('olt:planes_olt')}?olt={plan.olt_id or ''}")
    usadas = plan.onu_set.filter(ont_id__isnull=False).count() if plan else 0
    return render(request, "olt/plan_olt_form.html", {"form": form, "plan": plan, "usadas": usadas})


@login_required
@permission_required("ServerOLT.delete_planolt", raise_exception=True)
@require_POST
def plan_olt_eliminar(request, pk):
    plan = get_object_or_404(PlanOLT, pk=pk)
    n = plan.onu_set.count()
    if n:
        messages.warning(request, f"No se puede eliminar «{plan.nombre}»: lo usan {n} ONU(s).")
    else:
        nombre = plan.nombre
        plan.delete()
        messages.success(request, f"Plan OLT «{nombre}» eliminado.")
    return redirect("olt:planes_olt")


# ------------------------------------------------------------ Perfiles de velocidad (paso 18)

@login_required
def velocidades(request):
    direccion = request.GET.get("dir", "bajada")
    if direccion not in ("bajada", "subida"):
        direccion = "bajada"
    perfiles = (PerfilVelocidad.objects.filter(direccion=direccion)
                .annotate(usos=Count("onus_bajada", distinct=True) + Count("onus_subida", distinct=True))
                .order_by("kbps"))
    return render(request, "olt/velocidades.html", {
        "perfiles": perfiles, "direccion": direccion,
        "n_bajada": PerfilVelocidad.objects.filter(direccion="bajada").count(),
        "n_subida": PerfilVelocidad.objects.filter(direccion="subida").count(),
    })


@login_required
def velocidad_form(request, pk=None):
    if not request.user.has_perm("ServerOLT.change_perfilvelocidad" if pk else "ServerOLT.add_perfilvelocidad"):
        raise PermissionDenied
    perfil = get_object_or_404(PerfilVelocidad, pk=pk) if pk else None
    inicial = {"direccion": request.GET.get("dir")} if not perfil and request.GET.get("dir") in ("bajada", "subida") else None
    form = PerfilVelocidadForm(request.POST or None, instance=perfil, initial=inicial)
    if request.method == "POST" and form.is_valid():
        try:
            perfil = form.save()
        except IntegrityError:
            form.add_error("indice_olt", "Ese índice de traffic table ya lo usa otro perfil.")
        else:
            messages.success(request, f"Perfil «{perfil.nombre}» ({perfil.velocidad_texto}) guardado. "
                                      f"Se crea en la OLT la primera vez que una ONU lo use.")
            return redirect(f"{reverse('olt:velocidades')}?dir={perfil.direccion}")
    usos = (perfil.onus_bajada.count() + perfil.onus_subida.count()) if perfil else 0
    return render(request, "olt/velocidad_form.html", {"form": form, "perfil": perfil, "usos": usos})


@login_required
@permission_required("ServerOLT.delete_perfilvelocidad", raise_exception=True)
@require_POST
def velocidad_eliminar(request, pk):
    perfil = get_object_or_404(PerfilVelocidad, pk=pk)
    n = perfil.onus_bajada.count() + perfil.onus_subida.count()
    if n:
        messages.warning(request, f"No se puede eliminar «{perfil.nombre}»: lo usan {n} ONU(s). "
                                  f"Cámbiales la velocidad primero.")
    else:
        nombre, d = perfil.nombre, perfil.direccion
        perfil.delete()
        aviso = (" La traffic table sigue en la OLT (no molesta); bórrala a mano si quieres."
                 if perfil.creado_en_olt else "")
        messages.success(request, f"Perfil «{nombre}» eliminado.{aviso}")
        return redirect(f"{reverse('olt:velocidades')}?dir={d}")
    return redirect(f"{reverse('olt:velocidades')}?dir={perfil.direccion}")


# ------------------------------------------------------------ Puertos PON (paso 19, como SmartOLT)

def _puertos_de(olt):
    """
    Une 3 fuentes por puerto: tarjetas GPON leídas (display board), lo leído del puerto
    (display port state all, en PuertoPON) y las ONUs de nuestra base (online/total/señal).
    Devuelve [(slot, nombre_tarjeta, [puertos...])].
    """
    configuradas = olt.onus.filter(ont_id__isnull=False).exclude(estado__in=PENDIENTES)
    stats = {(f["frame"], f["slot"], f["puerto"]): f for f in configuradas.values("frame", "slot", "puerto").annotate(
        total=Count("id"), online=Count("id", filter=Q(en_linea=True)), rx=Avg("rx_power"))}
    pendientes = {(f["frame"], f["slot"], f["puerto"]): f["n"] for f in
                  olt.onus.filter(estado="sin_autorizar").values("frame", "slot", "puerto").annotate(n=Count("id"))}
    leidos = {(p.frame, p.slot, p.puerto): p for p in olt.puertos_pon.all()}

    tarjetas = {t["slot"]: t for t in olt.tarjetas_pon}
    claves = set(stats) | set(leidos)
    for slot, t in tarjetas.items():
        claves |= {(0, slot, p) for p in range(t.get("puertos", 0))}

    por_slot = {}
    for f, s, p in sorted(claves):
        pp = leidos.get((f, s, p)) or PuertoPON(olt=olt, frame=f, slot=s, puerto=p)
        st = stats.get((f, s, p), {})
        pp.total, pp.online = st.get("total", 0), st.get("online", 0)
        pp.rx, pp.pendientes = st.get("rx"), pendientes.get((f, s, p), 0)
        pp.carga = round(100 * pp.total / pp.max_onus) if pp.max_onus else 0
        por_slot.setdefault(s, []).append(pp)
    return [(s, tarjetas.get(s, {}).get("nombre", ""), lista) for s, lista in sorted(por_slot.items())]


@login_required
def puertos_pon(request, pk):
    olt = get_object_or_404(OLT, pk=pk)
    olt.liberar_si_trabada()
    grupos = _puertos_de(olt)
    slot = request.GET.get("slot", "")
    if slot.isdigit():
        grupos = [g for g in grupos if g[0] == int(slot)]
    return render(request, "olt/puertos_pon.html", {
        "olt": olt, "grupos": grupos, "slot": slot,
        "slots": [(t["slot"], t["nombre"]) for t in olt.tarjetas_pon],
        "leido": olt.puertos_pon.exclude(leido=None).order_by("-leido").values_list("leido", flat=True).first(),
        "cola": tasks.cola_de(olt), "sin_worker": _sin_worker(olt),
        "senal_debil": SENAL_DEBIL, "senal_critica": SENAL_CRITICA,
    })


def _fsp(texto):
    """'0/1/3' -> (0, 1, 3) o None."""
    partes = (texto or "").split("/")
    if len(partes) == 3 and all(x.isdigit() for x in partes):
        return tuple(int(x) for x in partes)
    return None


@login_required
@require_POST
def accion_puertos(request, pk):
    """Botones de la pestaña Puertos PON. Todo va a la fila de la OLT (nunca se habla con ella aquí)."""
    olt = get_object_or_404(OLT, pk=pk)
    accion = request.POST.get("accion", "")
    volver = redirect(f"{reverse('olt:puertos_pon', args=[olt.pk])}?slot={request.POST.get('slot', '')}")
    if accion != "leer" and not request.user.has_perm("ServerOLT.change_olt"):
        raise PermissionDenied
    todos = [[p.frame, p.slot, p.puerto] for _s, _n, lista in _puertos_de(olt) for p in lista]
    fsp = _fsp(request.POST.get("fsp"))

    try:
        if accion == "leer":
            tasks.encolar_accion_olt(olt, "puertos")
            messages.info(request, f"Leyendo los puertos PON de {olt}… la tabla se actualiza sola.")
        elif accion == "autofind_todos":
            tasks.encolar_accion_olt(olt, "puerto", puertos=todos, autofind=True)
            messages.info(request, f"Activando autofind en {len(todos)} puertos de {olt}…")
        elif accion == "habilitar_todos":
            tasks.encolar_accion_olt(olt, "puerto", puertos=todos, habilitar=True)
            messages.info(request, f"Habilitando {len(todos)} puertos PON de {olt}…")
        elif accion == "configurar" and fsp:
            pp, _ = PuertoPON.objects.get_or_create(olt=olt, frame=fsp[0], slot=fsp[1], puerto=fsp[2])
            pp.descripcion = request.POST.get("descripcion", "").strip()[:60]
            mx = request.POST.get("max_onus", "")
            if mx.isdigit() and 1 <= int(mx) <= 256:
                pp.max_onus = int(mx)
            pp.save(update_fields=["descripcion", "max_onus"])
            cambios = {}
            for campo in ("habilitado", "autofind"):
                v = request.POST.get(campo, "")
                nuevo = {"1": True, "0": False}.get(v)
                if nuevo is not None and nuevo != getattr(pp, campo):
                    cambios["habilitar" if campo == "habilitado" else "autofind"] = nuevo
            if cambios:
                tasks.encolar_accion_olt(olt, "puerto", puertos=[list(fsp)], **cambios)
                messages.info(request, f"Puerto {pp.fsp}: datos guardados y cambio en la fila de {olt}.")
            else:
                messages.success(request, f"Puerto {pp.fsp}: datos guardados.")
        elif accion == "reset_pon" and fsp:
            tasks.encolar_accion_olt(olt, "reset_pon", frame=fsp[0], slot=fsp[1], puerto=fsp[2])
            messages.info(request, f"Reiniciando las ONUs del puerto {'/'.join(map(str, fsp))}…")
        else:
            messages.warning(request, "Acción no válida.")
    except ValueError as e:
        messages.warning(request, str(e))
    except Exception as e:
        messages.error(request, AVISO_REDIS + str(e))
    return volver


# ------------------------------------------------------------ VLANs por OLT (paso 20, como SmartOLT)

def _vlans_con_uso(olt):
    from django.db import models
    from django.db.models.functions import Coalesce
    uso = dict(olt.onus.filter(ont_id__isnull=False).annotate(v=Coalesce("vlan", "plan__vlan", output_field=models.IntegerField()))
               .values_list("v").annotate(n=Count("id")))
    planes = {}
    for p in olt.planes.all():
        planes.setdefault(p.vlan, []).append(p.nombre)
    lista = list(olt.vlans.all())
    for v in lista:
        v.onus, v.planes = uso.get(v.vlan_id, 0), planes.get(v.vlan_id, [])
    return lista


@login_required
def vlans(request, pk):
    olt = get_object_or_404(OLT, pk=pk)
    olt.liberar_si_trabada()
    puede = request.user.has_perm("ServerOLT.change_olt")
    que = request.GET.get("form", "")
    form = form_multi = None
    if puede and que == "una":
        form = VLANForm(request.POST or None, olt=olt)
        if request.method == "POST" and form.is_valid():
            v = form.save()
            _crear_en_olt(request, olt, [[v.vlan_id, v.descripcion]], form.cleaned_data.get("crear_en_olt"))
            messages.success(request, f"VLAN {v} agregada a {olt}.")
            return redirect("olt:vlans", pk=olt.pk)
    elif puede and que == "varias":
        form_multi = VLANMultipleForm(request.POST or None, olt=olt)
        if request.method == "POST" and form_multi.is_valid():
            pref = form_multi.cleaned_data["descripcion"].strip()
            nuevas = []
            for n in form_multi.cleaned_data["vlans"]:
                v, creada = VLAN.objects.get_or_create(olt=olt, vlan_id=n, defaults={"descripcion": f"{pref}{n}" if pref else ""})
                if creada:
                    nuevas.append([v.vlan_id, v.descripcion])
            _crear_en_olt(request, olt, nuevas, form_multi.cleaned_data.get("crear_en_olt"))
            messages.success(request, f"{len(nuevas)} VLAN(s) nuevas en {olt}"
                                      + (f" ({len(form_multi.cleaned_data['vlans']) - len(nuevas)} ya estaban)."
                                         if len(nuevas) < len(form_multi.cleaned_data["vlans"]) else "."))
            return redirect("olt:vlans", pk=olt.pk)
    return render(request, "olt/vlans.html", {
        "olt": olt, "vlans": _vlans_con_uso(olt), "form": form, "form_multi": form_multi,
        "cola": tasks.cola_de(olt), "sin_worker": _sin_worker(olt),
    })


def _crear_en_olt(request, olt, lista, crear):
    if not (crear and lista) or olt.solo_lectura:
        return
    try:
        tasks.encolar_accion_olt(olt, "crear_vlan", vlans=lista)
        messages.info(request, f"Creando {len(lista)} VLAN(s) en {olt} (fila {tasks.cola_de(olt)})… "
                               f"Recuerda agregarlas al puerto de subida (uplink) de la OLT.")
    except ValueError as e:
        messages.warning(request, f"Se guardaron en el sistema, pero no se pudieron crear en la OLT: {e}")
    except Exception as e:
        messages.error(request, AVISO_REDIS + str(e))


@login_required
@require_POST
def vlan_accion(request, pk):
    """Leer VLANs de la OLT, editar casillas de una VLAN o borrarla (del sistema)."""
    olt = get_object_or_404(OLT, pk=pk)
    accion = request.POST.get("accion", "")
    if accion != "leer" and not request.user.has_perm("ServerOLT.change_olt"):
        raise PermissionDenied
    if accion == "leer":
        try:
            tasks.encolar_accion_olt(olt, "vlans")
            messages.info(request, f"Leyendo las VLANs de {olt}… la tabla se actualiza sola.")
        except ValueError as e:
            messages.warning(request, str(e))
        except Exception as e:
            messages.error(request, AVISO_REDIS + str(e))
    elif accion in ("casillas", "borrar"):
        v = get_object_or_404(VLAN, pk=request.POST.get("vlan"), olt=olt)
        if accion == "casillas":
            for campo in ("gestion", "iptv", "dhcp_snooping", "lan_to_lan"):
                setattr(v, campo, request.POST.get(campo) == "1")
            v.descripcion = request.POST.get("descripcion", v.descripcion).strip()[:60]
            v.save()
            messages.success(request, f"VLAN {v.vlan_id} guardada.")
        else:
            usos = [o for o in _vlans_con_uso(olt) if o.pk == v.pk][0]
            if usos.onus or usos.planes:
                messages.warning(request, f"No se puede borrar la VLAN {v.vlan_id}: la usan "
                                          f"{usos.onus} ONU(s) y los planes {', '.join(usos.planes) or '—'}.")
            else:
                v.delete()
                messages.success(request, f"VLAN {v.vlan_id} quitada del sistema. En la OLT sigue existiendo "
                                          f"(si quieres borrarla allá: undo vlan {v.vlan_id}).")
    return redirect("olt:vlans", pk=olt.pk)


@login_required
@require_POST
def olt_liberar(request, pk):
    """Paso 20b: botón 'Cancelar orden' cuando una orden de OLT no termina."""
    if not request.user.has_perm("ServerOLT.change_olt"):
        raise PermissionDenied
    olt = get_object_or_404(OLT, pk=pk)
    if olt.accion_pendiente:
        texto = olt.get_accion_pendiente_display()
        OLT.objects.filter(pk=olt.pk).update(accion_pendiente="", accion_desde=None, ultimo_error="")
        messages.info(request, f"Orden «{texto}» cancelada. Si el worker la hace igual más tarde, no pasa nada.")
    from django.utils.http import url_has_allowed_host_and_scheme
    volver = request.POST.get("volver", "")
    if not url_has_allowed_host_and_scheme(volver, allowed_hosts={request.get_host()}):
        volver = reverse("olt:detalle_olt", args=[olt.pk])
    return redirect(volver)


# ------------------------------------------------------------ Perfiles ONT (paso 21, pestaña "Profiles" de SmartOLT)

@login_required
def perfiles_ont(request, pk):
    olt = get_object_or_404(OLT, pk=pk)
    olt.liberar_si_trabada()
    if request.method == "POST":
        try:
            tasks.encolar_accion_olt(olt, "perfiles_ont")
            messages.info(request, f"Leyendo los perfiles de {olt}… la tabla se actualiza sola (puede tardar 1 minuto).")
        except ValueError as e:
            messages.warning(request, str(e))
        except Exception as e:
            messages.error(request, AVISO_REDIS + str(e))
        return redirect("olt:perfiles_ont", pk=olt.pk)
    uso_line = dict(olt.onus.filter(ont_id__isnull=False, lineprofile_id__isnull=False)
                    .values_list("lineprofile_id").annotate(n=Count("id")))
    uso_srv = dict(olt.onus.filter(ont_id__isnull=False, srvprofile_id__isnull=False)
                   .values_list("srvprofile_id").annotate(n=Count("id")))
    lineas, servicios_ = [], []
    for p in olt.perfiles_ont.all():
        p.nuestras = (uso_line if p.tipo == "line" else uso_srv).get(p.profile_id, 0)
        (lineas if p.tipo == "line" else servicios_).append(p)
    return render(request, "olt/perfiles_ont.html", {
        "olt": olt, "lineas": lineas, "servicios": servicios_,
        "cola": tasks.cola_de(olt), "sin_worker": _sin_worker(olt),
    })
