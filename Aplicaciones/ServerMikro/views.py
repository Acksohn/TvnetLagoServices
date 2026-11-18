"""
Pantallas de Clientes (estilo MikroWisp).
Igual que en las OLTs: la página NUNCA habla con el MikroTik.
Solo guarda en la base de datos y pone la orden en la fila (tasks.encolar).
"""
import ipaddress

from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.http import Http404, JsonResponse
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from . import tasks
from .forms import (CajaNAPForm, CambiarPlanForm, ClienteForm, NapClienteForm, PlanForm, RedIPv4Form,
                    RouterForm)
from .models import CajaNAP, Cliente, Plan, RedIPv4, Router, asignar_ip, ips_de_clientes
from .servicios import ESTADO_REQUERIDO

AVISO_REDIS = ("No se pudo poner la orden en la fila. ¿Está encendido Redis? "
               "(en Ubuntu/WSL: sudo service redis-server start). Detalle: ")

TEXTO_ACCION = {"crear": "creación en el MikroTik", "suspender": "suspensión",
                "activar": "activación", "plan": "aplicación del plan"}


@login_required
def lista(request):
    clientes = Cliente.objects.select_related("router", "plan")

    router_id = request.GET.get("router", "")
    estado = request.GET.get("estado", "")
    busqueda = request.GET.get("q", "").strip()

    if router_id.isdigit():
        clientes = clientes.filter(router_id=int(router_id))

    resumen = clientes.aggregate(
        total=Count("id"),
        activos=Count("id", filter=Q(estado="activo")),
        suspendidos=Count("id", filter=Q(estado="suspendido")),
        pendientes=Count("id", filter=Q(estado="pendiente")),
    )

    if estado in ("activo", "suspendido", "pendiente"):
        clientes = clientes.filter(estado=estado)
    if busqueda:
        clientes = clientes.filter(
            Q(nombre__icontains=busqueda) | Q(usuario_pppoe__icontains=busqueda)
            | Q(ip__icontains=busqueda) | Q(cedula__icontains=busqueda)
            | Q(telefono__icontains=busqueda) | Q(onus__sn__icontains=busqueda)).distinct()

    pagina = Paginator(clientes.order_by("nombre"), 50).get_page(request.GET.get("page"))
    return render(request, "clientes/lista.html", {
        "pagina": pagina, "resumen": resumen,
        "routers": Router.objects.annotate(total=Count("clientes")).order_by("nombre"),
        "router_id": router_id, "estado": estado, "busqueda": busqueda,
    })


@login_required
def detalle(request, pk):
    cliente = get_object_or_404(Cliente.objects.select_related("router", "plan", "caja_nap"), pk=pk)
    onus = cliente.onus.select_related("olt", "plan").order_by("sn")
    return render(request, "clientes/detalle.html", {
        "cliente": cliente, "onus": onus, "form_plan": CambiarPlanForm(cliente=cliente),
        "form_nap": NapClienteForm(instance=cliente, prefix="nap")})


@login_required
@permission_required("ServerMikro.change_cliente", raise_exception=True)
@require_POST
def cliente_nap(request, pk):
    """Paso 16: cambia la caja/puerto NAP del cliente. Solo base de datos (no toca equipos)."""
    cliente = get_object_or_404(Cliente, pk=pk)
    form = NapClienteForm(request.POST, instance=cliente, prefix="nap")
    if form.is_valid():
        try:
            with transaction.atomic():
                form.save()
        except IntegrityError:
            messages.error(request, "Ese puerto lo acaban de ocupar. Elige otro.")
        else:
            c = form.instance
            messages.success(request, f"{c}: {'caja ' + c.caja_nap.nombre + ', puerto ' + str(c.puerto_nap) if c.caja_nap else 'sin caja NAP'}.")
    else:
        for errores in form.errors.values():
            for e in errores:
                messages.error(request, e)
    return redirect("clientes:detalle", pk=pk)


@login_required
@permission_required("ServerMikro.change_cliente", raise_exception=True)
@require_POST
def cambiar_plan(request, pk):
    """Guarda el plan nuevo y, si el cliente ya está en el MikroTik, encola el cambio de velocidad."""
    cliente = get_object_or_404(Cliente.objects.select_related("router"), pk=pk)
    form = CambiarPlanForm(request.POST, cliente=cliente)
    if not form.is_valid():
        messages.error(request, "Elige un plan válido.")
        return redirect("clientes:detalle", pk=pk)
    if cliente.accion_pendiente:
        messages.warning(request, f"{cliente} ya tiene una orden en proceso; espera unos segundos.")
        return redirect("clientes:detalle", pk=pk)

    plan = form.cleaned_data["plan"]
    if plan.pk == cliente.plan_id and plan.velocidad == cliente.velocidad:
        messages.info(request, f"{cliente} ya tiene {plan.nombre} aplicado.")
        return redirect("clientes:detalle", pk=pk)
    cliente.plan = plan
    if cliente.estado == "pendiente":
        # Todavía no existe en el MikroTik: basta con guardarlo, se creará con esta velocidad
        cliente.velocidad = plan.velocidad
        cliente.save(update_fields=["plan", "velocidad", "actualizado"])
        messages.success(request, f"{cliente}: plan cambiado a {plan.nombre}.")
        return redirect("clientes:detalle", pk=pk)

    cliente.save(update_fields=["plan", "actualizado"])
    try:
        tasks.encolar(cliente, "plan")
        messages.success(request, f"{cliente}: cambio a {plan.nombre} en cola ({plan.velocidad}).")
    except Exception as e:
        messages.error(request, AVISO_REDIS + str(e))
    return redirect("clientes:detalle", pk=pk)


@login_required
@permission_required("ServerMikro.add_cliente", raise_exception=True)
def nuevo(request):
    form = ClienteForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        cliente = _guardar_con_ip(form)  # queda "pendiente"
        if cliente is None:
            return render(request, "clientes/form.html", _contexto_form(form))
        if form.cleaned_data["crear_ahora"]:
            try:
                tasks.encolar(cliente, "crear")
                messages.success(request, f"{cliente} guardado. Se está creando en el MikroTik…")
            except Exception as e:
                messages.error(request, f"{cliente} se guardó como Pendiente, pero no se pudo "
                                        f"poner en la fila. " + AVISO_REDIS + str(e))
        else:
            messages.info(request, f"{cliente} guardado como Pendiente. Se creará en el MikroTik "
                                   f"cuando autorices su ONU.")
        return redirect("clientes:detalle", pk=cliente.pk)
    return render(request, "clientes/form.html", _contexto_form(form))


def _contexto_form(form):
    return {"form": form, "url_siguiente_ip": reverse("clientes:siguiente_ip")}


def _guardar_con_ip(form):
    """
    Paso 15. Guarda el cliente. Si dos personas crean clientes a la vez y les tocó la misma IP
    automática, la base de datos rechaza la segunda (IP única): se le busca otra y se reintenta.
    """
    automatica = not form.data.get(form.add_prefix("ip"), "").strip()
    for _ in range(5):
        try:
            with transaction.atomic():
                return form.save()
        except IntegrityError:
            i = form.instance
            if i.caja_nap_id and Cliente.objects.filter(caja_nap_id=i.caja_nap_id, puerto_nap=i.puerto_nap).exists():
                form.add_error("puerto_nap", "Ese puerto NAP lo acaba de ocupar otro cliente.")
                return None
            if Cliente.objects.filter(usuario_pppoe=form.instance.usuario_pppoe).exists():
                form.add_error("usuario_pppoe", "Ese usuario PPPoE lo acaba de tomar otro cliente.")
                return None
            if not automatica:
                form.add_error("ip", "Esa IP la acaba de tomar otro cliente.")
                return None
            try:
                ip, _red = asignar_ip(form.cleaned_data["router"], form.cleaned_data.get("red"))
            except ValueError as e:
                form.add_error("ip", str(e))
                return None
            form.instance.ip = ip
    form.add_error("ip", "No se pudo asignar una IP libre; inténtalo otra vez.")
    return None


@login_required
@permission_required("ServerMikro.change_cliente", raise_exception=True)
@require_POST  # solo desde un botón, nunca abriendo un link
def accion(request, pk, accion):
    if accion not in ESTADO_REQUERIDO:
        raise Http404("Acción no válida")
    cliente = get_object_or_404(Cliente.objects.select_related("router"), pk=pk)
    volver = request.POST.get("volver", "")

    if cliente.estado not in ESTADO_REQUERIDO[accion]:
        messages.warning(request, f"{cliente} está {cliente.get_estado_display()}: "
                                  f"no se puede hacer la {TEXTO_ACCION[accion]}.")
    else:
        try:
            tasks.encolar(cliente, accion)
            messages.success(request, f"{cliente}: {TEXTO_ACCION[accion]} en cola.")
        except ValueError as e:
            messages.warning(request, str(e))
        except Exception as e:
            messages.error(request, AVISO_REDIS + str(e))

    if volver == "detalle":
        return redirect("clientes:detalle", pk=cliente.pk)
    # Volver a la lista con los mismos filtros (solo URLs de este sitio)
    siguiente = request.POST.get("siguiente", "")
    if url_has_allowed_host_and_scheme(siguiente, allowed_hosts={request.get_host()}):
        return redirect(siguiente)
    return redirect("clientes:lista")


# ------------------------------------------------------------ Planes (paso 9)

@login_required
def planes(request):
    lista = Plan.objects.annotate(
        total=Count("clientes"),
        activos=Count("clientes", filter=Q(clientes__estado="activo")),
    )
    # Clientes cuya velocidad aplicada no coincide con la de su plan
    for p in lista:
        p.desfasados = p.clientes.exclude(velocidad=p.velocidad).exclude(estado="pendiente").count()
    return render(request, "planes/lista.html", {
        "planes": lista,
        "sin_plan": Cliente.objects.filter(plan__isnull=True).count(),
    })


@login_required
def plan_form(request, pk=None):
    if not request.user.has_perm("ServerMikro.change_plan" if pk else "ServerMikro.add_plan"):
        raise PermissionDenied
    plan = get_object_or_404(Plan, pk=pk) if pk else None
    velocidad_antes = plan.velocidad if plan else None
    form = PlanForm(request.POST or None, instance=plan)
    if request.method == "POST" and form.is_valid():
        plan = form.save()
        texto = "creado" if velocidad_antes is None else "guardado"
        messages.success(request, f"{plan.nombre} {texto}.")

        # Si cambió la velocidad y pidieron aplicarla, encolamos uno por uno
        if velocidad_antes and velocidad_antes != plan.velocidad and form.cleaned_data.get("aplicar"):
            enviados, omitidos = 0, 0
            for c in plan.clientes.select_related("router").filter(estado__in=("activo", "suspendido")):
                try:
                    tasks.encolar(c, "plan")
                    enviados += 1
                except ValueError:
                    omitidos += 1  # ese cliente ya tenía otra orden en proceso
                except Exception as e:
                    messages.error(request, AVISO_REDIS + str(e))
                    break
            Cliente.objects.filter(plan=plan, estado="pendiente").update(velocidad=plan.velocidad)
            if enviados:
                messages.info(request, f"{enviados} cliente(s) en la fila para pasar a {plan.velocidad}. "
                                       f"Míralo en Tareas en cola.")
            if omitidos:
                messages.warning(request, f"{omitidos} cliente(s) tenían otra orden en proceso; "
                                          f"cámbialos luego desde su ficha.")
        return redirect("clientes:planes")
    return render(request, "planes/form.html", {"form": form, "plan": plan})



# ------------------------------------------------------------ Routers MikroTik (paso 11)

ESPERA_WORKER = 120  # segundos en la fila sin respuesta -> probablemente no hay worker


def _sin_worker(router):
    from django.utils import timezone
    return bool(router.accion_pendiente and router.accion_desde
                and (timezone.now() - router.accion_desde).total_seconds() > ESPERA_WORKER)


def _routers_con_conteos():
    return Router.objects.annotate(
        total=Count("clientes"),
        activos=Count("clientes", filter=Q(clientes__estado="activo")),
        suspendidos=Count("clientes", filter=Q(clientes__estado="suspendido")),
        pendientes=Count("clientes", filter=Q(clientes__estado="pendiente")),
        en_proceso=Count("clientes", filter=~Q(clientes__accion_pendiente="")),
    )


@login_required
def routers(request):
    lista = list(_routers_con_conteos().order_by("nombre"))
    for r in lista:
        r.cola = tasks.cola_de(r)
        r.sin_worker = _sin_worker(r)
    return render(request, "routers/lista.html", {
        "routers": lista,
        "resumen": {"routers": len(lista),
                    "conectados": sum(1 for r in lista if r.conectado),
                    "sin_conexion": sum(1 for r in lista if r.conectado is False),
                    "ppp": sum(r.ppp_activos or 0 for r in lista)},
    })


@login_required
def router_detalle(request, pk):
    router = get_object_or_404(_routers_con_conteos(), pk=pk)
    ultimos = router.clientes.select_related("plan").order_by("-actualizado")[:8]
    con_error = router.clientes.exclude(ultimo_error="").order_by("nombre")[:20]
    return render(request, "routers/detalle.html", {
        "router": router, "ultimos": ultimos, "con_error": con_error,
        "cola": tasks.cola_de(router), "sin_worker": _sin_worker(router),
    })


@login_required
@require_POST
def router_info(request, pk):
    router = get_object_or_404(Router, pk=pk)
    try:
        tasks.encolar_info_router(router)
        messages.info(request, f"{router.nombre}: probando conexión en la fila '{tasks.cola_de(router)}'…")
    except ValueError as e:
        messages.warning(request, str(e))
    except Exception as e:
        messages.error(request, AVISO_REDIS + str(e))
    if request.POST.get("volver") == "lista":
        return redirect("clientes:routers")
    return redirect("clientes:router_detalle", pk=router.pk)


@login_required
def router_form(request, pk=None):
    if not request.user.has_perm("ServerMikro.change_router" if pk else "ServerMikro.add_router"):
        raise PermissionDenied
    router = get_object_or_404(Router, pk=pk) if pk else None
    form = RouterForm(request.POST or None, instance=router)
    if request.method == "POST" and form.is_valid():
        nuevo = router is None
        router = form.save()
        if nuevo:
            messages.success(request, f"{router.nombre} creado. Abre su worker con el comando de abajo "
                                      f"y pulsa «Probar conexión».")
        else:
            messages.success(request, f"{router.nombre} guardado.")
        return redirect("clientes:router_detalle", pk=router.pk)
    return render(request, "routers/form.html", {"form": form, "router": router})


@login_required
@permission_required("ServerMikro.delete_router", raise_exception=True)
@require_POST
def router_eliminar(request, pk):
    router = get_object_or_404(Router, pk=pk)
    n = router.clientes.count()
    if n:
        messages.warning(request, f"No se puede eliminar {router.nombre}: tiene {n} cliente(s). "
                                  f"Pásalos a otro router o elimínalos primero.")
        return redirect("clientes:router_detalle", pk=pk)
    nombre = router.nombre
    router.delete()  # solo borra el registro del sistema; el MikroTik no se toca
    messages.success(request, f"{nombre} eliminado del sistema.")
    return redirect("clientes:routers")



@login_required
@require_POST
def router_fijar_huella(request, pk):
    """Guarda como huella de confianza la que el MikroTik presentó en la última prueba (paso 12)."""
    if not request.user.has_perm("ServerMikro.change_router"):
        raise PermissionDenied
    router = get_object_or_404(Router, pk=pk)
    if router.metodo != "api_ssl" or not router.huella_vista:
        messages.warning(request, "Primero prueba la conexión por API-SSL.")
    else:
        router.huella_ssl = router.huella_vista
        router.save(update_fields=["huella_ssl"])
        messages.success(request, f"{router.nombre}: huella fijada. Desde ahora solo se conecta a este certificado.")
    return redirect("clientes:router_detalle", pk=pk)



# ------------------------------------------------------------ Redes IPv4 (paso 15)

def _uso(redes):
    """Agrega a cada red: usadas, libres, bloqueadas y porcentaje (con UNA sola lectura de clientes)."""
    usadas = ips_de_clientes()
    for r in redes:
        net = r.red_ip
        r.usadas = sum(1 for ip in usadas if ip in net)
        r.n_bloqueadas = len(r.bloqueadas())
        r.disponibles = max(r.total - r.n_bloqueadas, 0)
        r.libres = max(r.disponibles - r.usadas, 0)
        r.porcentaje = round(r.usadas * 100 / r.disponibles, 1) if r.disponibles else 100.0
        r.color = "rojo" if r.porcentaje >= 95 else "naranja" if r.porcentaje >= 75 else "azul"
    return redes


@login_required
def redes(request):
    lista = list(RedIPv4.objects.select_related("router"))
    router_id = request.GET.get("router", "")
    if router_id.isdigit():
        lista = [r for r in lista if r.router_id == int(router_id)]
    lista.sort(key=lambda r: (r.router.nombre, int(r.red_ip.network_address)))
    _uso(lista)
    return render(request, "redes/lista.html", {
        "redes": lista, "routers": Router.objects.order_by("nombre"), "router_id": router_id,
        "resumen": {"redes": len(lista), "usadas": sum(r.usadas for r in lista),
                    "libres": sum(r.libres for r in lista),
                    "llenas": sum(1 for r in lista if r.porcentaje >= 95)},
    })


@login_required
def red_form(request, pk=None):
    if not request.user.has_perm("ServerMikro.change_redipv4" if pk else "ServerMikro.add_redipv4"):
        raise PermissionDenied
    red = get_object_or_404(RedIPv4, pk=pk) if pk else None
    inicial = {}
    if not red and request.GET.get("router", "").isdigit():
        inicial["router"] = int(request.GET["router"])
    form = RedIPv4Form(request.POST or None, instance=red, initial=inicial)
    if request.method == "POST" and form.is_valid():
        red = form.save()
        messages.success(request, f"Red {red.cidr_texto} guardada en {red.router.nombre}.")
        return redirect("clientes:red_detalle", pk=red.pk)
    return render(request, "redes/form.html", {"form": form, "red": red})


MAPA_MAXIMO = 1024  # en redes más grandes que /22 no se dibuja el mapa completo


@login_required
def red_detalle(request, pk):
    red = get_object_or_404(RedIPv4.objects.select_related("router"), pk=pk)
    _uso([red])
    net = red.red_ip
    clientes = {}
    for c in Cliente.objects.select_related("router").only("id", "nombre", "ip", "estado", "router__nombre"):
        if red.contiene(c.ip):
            clientes[c.ip] = c
    bloqueadas = {str(ip) for ip in red.bloqueadas()}
    mapa = []
    if red.total <= MAPA_MAXIMO:
        for ip in red.hosts():
            ip = str(ip)
            c = clientes.get(ip)
            tipo = ("gateway" if ip == red.gateway else "reservada" if ip in bloqueadas
                    else "usada" if c else "libre")
            mapa.append({"ip": ip, "fin": ip.rsplit(".", 1)[-1], "tipo": tipo, "cliente": c})
    ajenos = [c for c in clientes.values() if c.router_id != red.router_id]
    return render(request, "redes/detalle.html", {
        "red": red, "mapa": mapa, "mapa_maximo": MAPA_MAXIMO,
        "clientes": sorted(clientes.values(), key=lambda c: int(ipaddress.IPv4Address(c.ip))),
        "ajenos": ajenos, "siguiente": red.siguiente_libre(ips_de_clientes()),
    })


@login_required
@permission_required("ServerMikro.delete_redipv4", raise_exception=True)
@require_POST
def red_eliminar(request, pk):
    red = get_object_or_404(RedIPv4, pk=pk)
    _uso([red])
    if red.usadas:
        messages.warning(request, f"{red.cidr_texto} tiene {red.usadas} cliente(s) con IP en esa red. "
                                  f"Desmárcala como «Usar para clientes nuevos» en vez de borrarla.")
        return redirect("clientes:red_detalle", pk=pk)
    texto = red.cidr_texto
    red.delete()
    messages.success(request, f"Red {texto} eliminada.")
    return redirect("clientes:redes")


@login_required
def siguiente_ip(request):
    """Para el formulario de cliente: qué IP le tocaría (solo informa; se asigna al guardar)."""
    router = Router.objects.filter(pk=request.GET.get("router") or 0).first()
    if not router:
        return JsonResponse({"ip": "", "texto": "Elige el router"})
    red = RedIPv4.objects.filter(pk=request.GET.get("red") or 0, router=router).first()
    try:
        ip, r = asignar_ip(router, red)
    except ValueError as e:
        return JsonResponse({"ip": "", "texto": str(e)})
    return JsonResponse({"ip": ip, "texto": f"Automática: {ip} ({r.cidr_texto})"})



# ------------------------------------------------------------ Cajas NAP (paso 16)

@login_required
def naps(request):
    busqueda = request.GET.get("q", "").strip()
    cajas = CajaNAP.objects.prefetch_related("clientes")
    if busqueda:
        cajas = cajas.filter(Q(nombre__icontains=busqueda) | Q(ubicacion__icontains=busqueda) |
                             Q(detalles__icontains=busqueda))
    lista = list(cajas)
    total = libres = 0
    for c in lista:
        ocupados = {cl.puerto_nap: cl for cl in c.clientes.all() if cl.puerto_nap}
        c.mapa = [{"n": i, "cliente": ocupados.get(i)} for i in range(1, c.puertos + 1)]
        c.n_ocupados = len(ocupados)
        total += c.puertos
        libres += c.puertos - len(ocupados)
    return render(request, "naps/lista.html", {
        "cajas": lista, "busqueda": busqueda,
        "resumen": {"cajas": len(lista), "puertos": total, "libres": libres, "ocupados": total - libres,
                    "llenas": sum(1 for c in lista if c.n_ocupados >= c.puertos)},
    })


@login_required
def nap_form(request, pk=None):
    if not request.user.has_perm("ServerMikro.change_cajanap" if pk else "ServerMikro.add_cajanap"):
        raise PermissionDenied
    caja = get_object_or_404(CajaNAP, pk=pk) if pk else None
    form = CajaNAPForm(request.POST or None, instance=caja)
    if request.method == "POST" and form.is_valid():
        caja = form.save()
        messages.success(request, f"Caja NAP {caja.nombre} guardada ({caja.puertos} puertos).")
        return redirect("clientes:naps")
    return render(request, "naps/form.html", {"form": form, "caja": caja})


@login_required
@permission_required("ServerMikro.delete_cajanap", raise_exception=True)
@require_POST
def nap_eliminar(request, pk):
    caja = get_object_or_404(CajaNAP, pk=pk)
    n = caja.clientes.count()
    if n:
        messages.warning(request, f"{caja.nombre} tiene {n} cliente(s) conectados. Cámbialos de caja primero.")
        return redirect("clientes:nap_editar", pk=pk)
    nombre = caja.nombre
    caja.delete()
    messages.success(request, f"Caja NAP {nombre} eliminada.")
    return redirect("clientes:naps")



# ------------------------------------------------------------ Buscador de clientes (paso 17)

@login_required
def buscar_clientes(request):
    """
    Para los <select> con buscador (Select2): devuelve hasta 20 clientes que coinciden.
    ?q=texto busca en nombre, usuario PPPoE, cédula, IP y teléfono. ?sin_onu=1 = solo los que no tienen ONU.
    """
    q = request.GET.get("q", "").strip()
    qs = Cliente.objects.select_related("caja_nap").order_by("nombre")
    if request.GET.get("sin_onu"):
        # Paso 20: sin ONU AUTORIZADA (si su ONU se eliminó o falló, el cliente vuelve a salir)
        from Aplicaciones.ServerOLT.forms import clientes_con_onu
        qs = qs.exclude(pk__in=clientes_con_onu())
    for palabra in q.split()[:4]:
        qs = qs.filter(Q(nombre__icontains=palabra) | Q(usuario_pppoe__icontains=palabra) |
                       Q(cedula__icontains=palabra) | Q(ip__icontains=palabra) |
                       Q(telefono__icontains=palabra))
    resultados = []
    for c in qs.distinct()[:20]:
        resultados.append({
            "id": c.pk,
            "text": f"{c.nombre} · {c.usuario_pppoe} ({c.get_estado_display()})",
            "extra": {"nap": f"{c.caja_nap.nombre} puerto {c.puerto_nap}" if c.caja_nap_id else ""},
        })
    return JsonResponse({"results": resultados})
