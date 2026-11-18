"""
Lógica del sistema: qué hacer en la base de datos y en los equipos.
El admin (y luego Celery) solo llaman a estas funciones.
"""
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from netmiko.exceptions import NetmikoAuthenticationException, NetmikoTimeoutException

from Aplicaciones.ServerMikro.servicios import crear_en_mikrotik

from . import huawei
from .models import ONU, VLAN, PerfilONT, PerfilVelocidad, PuertoPON


def verificar_escritura(olt):
    """Las OLTs marcadas 'solo lectura' nunca reciben comandos que cambien algo."""
    if olt.solo_lectura:
        raise ValueError(f"{olt} está en modo SOLO LECTURA: no se autoriza ni se da de baja "
                         f"desde este sistema (la maneja otro, ej. SmartOLT).")


def buscar_onus_nuevas(olt):
    """
    Lee 'display ont autofind all' y deja "Sin configurar" IGUAL a lo que muestra la OLT:
      - las que aparecen  -> 'sin_autorizar' (nuevas o que volvieron)
      - las que ya NO aparecen:
          * si la OLT las tiene autorizadas (un intento que se cortó a mitad, o alguien las
            autorizó a mano) -> pasan a 'activa' con su ONT-ID            (NUEVO paso 14)
          * si no -> 'no_detectada' (desconectadas)
    No se borra nada: si la ONU vuelve a conectarse, la próxima búsqueda la trae de nuevo.
    Devuelve (en_autofind, nuevas, quitadas, ya_autorizadas).
    """
    with huawei.conectar(olt) as con:
        encontradas = huawei.leer_autofind(con)
        vistas = [d["sn"] for d in encontradas]
        # Antes de darlas por desconectadas, preguntamos a la OLT si las tiene registradas
        # (también las 'no_detectada' del último día: pudieron autorizarse a mano o por un intento cortado)
        un_dia = timezone.now() - timedelta(days=1)
        desaparecidas = list(ONU.objects.select_related("plan")
                             .filter(olt=olt, ont_id__isnull=True)
                             .filter(Q(estado="sin_autorizar") |
                                     Q(estado="no_detectada", actualizado__gte=un_dia))
                             .exclude(sn__in=vistas).order_by("-actualizado")[:20])
        registradas = []
        for onu in desaparecidas:
            info = huawei.buscar_por_sn(con, onu.sn)
            if info:
                indice = None
                if onu.vlan_efectiva:
                    indice = huawei.buscar_service_port(con, info["frame"], info["slot"],
                                                        info["puerto"], info["ont_id"], onu.vlan_efectiva)
                registradas.append((onu, info, indice))

    nuevas = 0
    for d in encontradas:
        previa = ONU.objects.filter(sn=d["sn"]).first()
        if previa and previa.ont_id is not None:
            # Ya está autorizada (tiene ONT-ID): NO se pisa. Seguramente movieron la fibra a otro puerto
            # y la OLT la ve en autofind ahí, pero sigue registrada en el puerto viejo.
            previa.ultimo_error = (f"La OLT la ve en autofind en {d['frame']}/{d['slot']}/{d['puerto']}, "
                                   f"pero está registrada en {previa.fsp}:{previa.ont_id}. "
                                   f"¿Se movió la fibra de puerto?")
            previa.save(update_fields=["ultimo_error", "actualizado"])
            continue
        if previa and previa.estado == "autorizando" and not previa.autorizando_trabada:
            continue  # NUEVO (paso 14): tiene una autorización en la fila; no se le cambia nada
        onu, creada = ONU.objects.update_or_create(
            sn=d["sn"],
            defaults={"olt": olt, "frame": d["frame"], "slot": d["slot"],
                      "puerto": d["puerto"], "modelo": d["modelo"],
                      "estado": "sin_autorizar", "ont_id": None, "service_port": None},
        )
        nuevas += creada

    # Las que la OLT ya tiene autorizadas: se pasan a "ONUs configuradas"
    ids_registradas = []
    for onu, info, indice in registradas:
        onu.frame, onu.slot, onu.puerto = info["frame"], info["slot"], info["puerto"]
        onu.ont_id, onu.service_port = info["ont_id"], indice
        onu.estado = "activa"
        onu.ultimo_error = ("" if onu.plan_efectivo else
                            "Estaba autorizada en la OLT pero sin Plan OLT en el sistema: asígnale uno.")
        try:
            onu.save()
            ids_registradas.append(onu.pk)
        except IntegrityError:
            pass  # otra ONU del sistema ocupa esa posición; se deja como estaba

    # Las que esta OLT tenía en "Sin configurar" y ya no salen en su autofind
    quitadas = (ONU.objects.filter(olt=olt, estado="sin_autorizar", ont_id__isnull=True)
                .exclude(sn__in=vistas).exclude(pk__in=ids_registradas)
                .update(estado="no_detectada"))
    return len(encontradas), nuevas, quitadas, len(ids_registradas)


def autorizar(onu):
    """
    Autoriza la ONU en la OLT y, si tiene cliente pendiente, crea su PPPoE en el MikroTik.
    Paso 14: es "idempotente": si la OLT ya tiene ese SN (un intento anterior sí llegó),
    NO hace otro 'ont add', toma el ONT-ID que ya existe. Devuelve un aviso o "".
    """
    # La vista ya la marcó 'autorizando' al hacer clic (tasks.encolar_autorizacion).
    # Si nos llaman desde otro lado con la ONU 'sin_autorizar', la marcamos aquí.
    if onu.estado == "sin_autorizar":
        with transaction.atomic():
            filas = ONU.objects.filter(pk=onu.pk, estado="sin_autorizar").update(estado="autorizando")
        if not filas:
            raise ValueError(f"{onu.sn} ya la está autorizando otra persona")
    elif onu.estado != "autorizando":
        raise ValueError(f"{onu.sn} no está 'sin autorizar' (está {onu.get_estado_display()})")

    aviso = ""
    try:
        verificar_escritura(onu.olt)
        if not onu.plan_efectivo:  # paso 21: line/srv profile y VLAN (de la ONU o de su preset)
            raise ValueError(f"{onu.sn}: faltan el line-profile, el service-profile o la VLAN")
        if onu.plan and onu.plan.olt_id not in (None, onu.olt_id):  # paso 20: los perfiles son de cada OLT
            raise ValueError(f"{onu.sn}: el plan «{onu.plan.nombre}» es de otra OLT ({onu.plan.olt})")
        desc = onu.descripcion or (onu.cliente.usuario_pppoe if onu.cliente else onu.sn)
        with huawei.conectar(onu.olt) as con:
            # Paso 18: primero las traffic tables (velocidad), así el service-port ya nace con ellas
            trafico = (huawei.asegurar_traffic_table(con, onu.vel_subida),
                       huawei.asegurar_traffic_table(con, onu.vel_bajada))
            _marcar_creados(onu)
            existente = huawei.buscar_por_sn(con, onu.sn)
            if existente:
                onu.frame, onu.slot, onu.puerto = (existente["frame"], existente["slot"],
                                                   existente["puerto"])
                ont_id, indice = huawei.completar_onu(con, onu.frame, onu.slot, onu.puerto,
                                                      existente["ont_id"], onu.plan_efectivo, modo=onu.modo,
                                                      trafico=trafico)
                aviso = (f"ya estaba en la OLT como {onu.fsp}:{ont_id}; "
                         f"se tomó ese ONT-ID (no se agregó otra vez)")
            else:
                ont_id, indice = huawei.autorizar_onu(
                    con, onu.frame, onu.slot, onu.puerto, onu.sn, onu.plan_efectivo, desc, modo=onu.modo,
                    trafico=trafico)
            # Paso 17: ONU mode. Si falla, la ONU YA está autorizada: solo se deja un aviso.
            try:
                pppoe = ((onu.cliente.usuario_pppoe, onu.cliente.clave_pppoe)
                         if onu.cliente else None)
                aviso_modo = huawei.configurar_modo(con, onu.frame, onu.slot, onu.puerto, ont_id,
                                                    onu.vlan_efectiva, onu.modo, pppoe)
            except Exception as e:
                aviso_modo = (f"Routing: la ONU quedó autorizada pero se cortó al configurar la WAN "
                              f"({type(e).__name__}). Configúrala en la web de la ONU.")
        onu.ont_id, onu.service_port = ont_id, indice
        onu.estado, onu.ultimo_error, onu.aviso = "activa", "", aviso_modo
        onu.save()
        if aviso_modo:
            aviso = "; ".join(x for x in (aviso, aviso_modo) if x)
    except Exception as e:
        texto = str(e)
        if isinstance(e, (NetmikoTimeoutException, NetmikoAuthenticationException)):
            texto += ("\n\nNo se reintenta solo. Revisa que nadie tenga una sesión abierta en la OLT "
                      "y pulsa Reintentar: si la OLT ya la había agregado, el sistema lo detecta "
                      "y no la duplica.")
        onu.estado, onu.ultimo_error = "sin_autorizar", texto
        onu.save(update_fields=["estado", "ultimo_error", "actualizado"])
        raise

    # Parte MikroTik: solo si el cliente todavía no existe allá
    if onu.cliente and onu.cliente.estado == "pendiente":
        crear_en_mikrotik(onu.cliente)
    return aviso


def dar_de_baja(onu):
    """Quita la ONU de la OLT (service-port + ont delete). No toca el MikroTik."""
    verificar_escritura(onu.olt)
    if onu.ont_id is None:
        raise ValueError(f"{onu.sn} no tiene ONT-ID; no está autorizada")
    with huawei.conectar(onu.olt) as con:
        huawei.eliminar_onu(con, onu.frame, onu.slot, onu.puerto, onu.ont_id,
                            onu.service_port, onu.vlan_efectiva)
    onu.ont_id = onu.service_port = None
    onu.estado, onu.ultimo_error = "sin_autorizar", ""
    onu.cliente = None  # paso 20: como en SmartOLT, la ONU eliminada ya no es de ese cliente
    onu.save()


# ---------------------------------------------------------------- ficha de la ONU (paso 9)

def _requiere_autorizada(onu):
    if onu.ont_id is None:
        raise ValueError(f"{onu.sn} no está autorizada (no tiene ONT-ID)")


def leer_senal(onu):
    """Solo lectura: se permite también en OLTs 'solo lectura'."""
    from django.utils import timezone
    _requiere_autorizada(onu)
    with huawei.conectar(onu.olt) as con:
        d = huawei.leer_senal(con, onu.frame, onu.slot, onu.puerto, onu.ont_id)
    onu.en_linea = d["en_linea"]
    onu.rx_power, onu.tx_power = d["rx"], d["tx"]
    onu.olt_rx_power, onu.temperatura = d["olt_rx"], d["temperatura"]
    onu.senal_leida = timezone.now()
    onu.save(update_fields=["en_linea", "rx_power", "tx_power", "olt_rx_power",
                            "temperatura", "senal_leida", "actualizado"])
    return "en línea" if d["en_linea"] else "fuera de línea"


def reiniciar(onu):
    verificar_escritura(onu.olt)
    _requiere_autorizada(onu)
    with huawei.conectar(onu.olt) as con:
        huawei.reiniciar_onu(con, onu.frame, onu.slot, onu.puerto, onu.ont_id)
    return "reiniciada"


def desactivar(onu):
    """Corte físico en la OLT (distinto de suspender el PPPoE en el MikroTik)."""
    verificar_escritura(onu.olt)
    _requiere_autorizada(onu)
    with huawei.conectar(onu.olt) as con:
        huawei.desactivar_onu(con, onu.frame, onu.slot, onu.puerto, onu.ont_id)
    onu.estado = "suspendida"
    onu.save(update_fields=["estado", "actualizado"])
    return "desactivada"


def activar(onu):
    verificar_escritura(onu.olt)
    _requiere_autorizada(onu)
    with huawei.conectar(onu.olt) as con:
        huawei.activar_onu(con, onu.frame, onu.slot, onu.puerto, onu.ont_id)
    onu.estado = "activa"
    onu.save(update_fields=["estado", "actualizado"])
    return "activada"


def _marcar_creados(onu):
    """Los perfiles que ya están en una OLT no se pueden editar (la OLT no se enteraría)."""
    ids = [p.pk for p in (onu.vel_subida, onu.vel_bajada) if p is not None]
    if ids:
        PerfilVelocidad.objects.filter(pk__in=ids).update(creado_en_olt=True)


def cambiar_velocidad(onu):
    """Paso 18: aplica en la OLT los perfiles de velocidad que la ONU tiene en el sistema."""
    _requiere_autorizada(onu)
    verificar_escritura(onu.olt)
    if not onu.plan_efectivo:
        raise ValueError(f"{onu.sn} no tiene line-profile/VLAN guardados: no se sabe su VLAN ni su GEM port")
    with huawei.conectar(onu.olt) as con:
        trafico = (huawei.asegurar_traffic_table(con, onu.vel_subida),
                   huawei.asegurar_traffic_table(con, onu.vel_bajada))
        _marcar_creados(onu)
        onu.service_port = huawei.cambiar_trafico(con, onu.frame, onu.slot, onu.puerto, onu.ont_id,
                                                  onu.plan_efectivo, onu.service_port, trafico)
    onu.save(update_fields=["service_port", "actualizado"])
    baja = onu.vel_bajada.nombre if onu.vel_bajada else "ilimitada"
    sube = onu.vel_subida.nombre if onu.vel_subida else "ilimitada"
    return f"velocidad en la OLT: bajada {baja}, subida {sube}"


# Acciones permitidas desde la ficha y en qué estado de la ONU se puede hacer cada una
ACCIONES_ONU = {
    "senal": (leer_senal, ("activa", "suspendida", "error")),
    "reiniciar": (reiniciar, ("activa",)),
    "desactivar": (desactivar, ("activa",)),
    "activar": (activar, ("suspendida",)),
    "baja": (dar_de_baja, ("activa", "suspendida", "error")),
    "velocidad": (cambiar_velocidad, ("activa", "suspendida")),  # paso 18
}


# ---------------------------------------------------------------- info de la OLT (paso 10)

def leer_info_olt(olt):
    """Solo lectura: modelo, versión, encendida desde y tarjetas. Vale también para 'solo lectura'."""
    from datetime import timedelta

    from django.utils import timezone
    with huawei.conectar(olt) as con:
        d = huawei.leer_info(con)
    ahora = timezone.now()
    olt.modelo_equipo = d["modelo"][:40]
    olt.version = d["version"][:60]
    olt.encendida_desde = ahora - timedelta(seconds=d["uptime_segundos"]) if d["uptime_segundos"] else None
    if d["tarjetas"]:  # si no se pudo leer, se conservan las de antes
        olt.tarjetas = d["tarjetas"]
    olt.perfiles = dict(d.get("perfiles") or {}, leidos=ahora.isoformat())  # paso 18
    olt.conectada, olt.info_leida = True, ahora
    olt.save(update_fields=["modelo_equipo", "version", "encendida_desde", "tarjetas",
                            "conectada", "info_leida", "perfiles"])
    return f"{olt.modelo_equipo or 'OLT'} {olt.version}, {len(d['tarjetas'])} tarjetas"


# ---------------------------------------------------------------- puertos PON (paso 19)

def _slots_gpon(olt):
    return [(0, t["slot"], t["puertos"]) for t in olt.tarjetas_pon]


def leer_puertos_olt(olt):
    """Solo lectura (vale para OLTs en solo lectura): estado de todos los puertos PON."""
    from django.utils import timezone
    slots = _slots_gpon(olt)
    if not slots:
        raise ValueError(f"{olt}: todavía no se conocen sus tarjetas GPON. Pulsa primero «Actualizar info».")
    ahora, total = timezone.now(), 0
    with huawei.conectar(olt) as con:
        for frame, slot, _n in slots:
            for p in huawei.leer_puertos(con, frame, slot):
                PuertoPON.objects.update_or_create(
                    olt=olt, frame=frame, slot=slot, puerto=p["puerto"],
                    defaults={"estado": p["estado"][:20], "laser": p["laser"][:20],
                              "habilitado": p["habilitado"], "tx_power": p["tx_power"],
                              "distancia_max": p["distancia_max"], "datos": p["datos"], "leido": ahora,
                              # si este firmware no muestra el autofind, se conserva lo que sabíamos
                              **({"autofind": p["autofind"]} if p["autofind"] is not None else {})})
                total += 1
    return f"{total} puertos PON leídos"


def configurar_puertos(olt, puertos, habilitar=None, autofind=None):
    """puertos = [[frame, slot, puerto], ...]. Habilitar / autofind en la OLT y se guarda aquí."""
    verificar_escritura(olt)
    from django.utils import timezone
    with huawei.conectar(olt) as con:
        for frame, slot, puerto in puertos:
            huawei.configurar_puerto(con, frame, slot, puerto, habilitar=habilitar, autofind=autofind)
            cambios = {}
            if habilitar is not None:
                cambios["habilitado"] = habilitar
            if autofind is not None:
                cambios["autofind"] = autofind
            PuertoPON.objects.update_or_create(olt=olt, frame=frame, slot=slot, puerto=puerto, defaults=cambios)
        # Se vuelve a leer el estado real de esas tarjetas, para que la tabla muestre lo que quedó
        tocados = {(f, s) for f, s, _p in puertos}
        for frame, slot in sorted(tocados):
            try:
                for p in huawei.leer_puertos(con, frame, slot):
                    if [frame, slot, p["puerto"]] in [list(x) for x in puertos]:
                        PuertoPON.objects.filter(olt=olt, frame=frame, slot=slot, puerto=p["puerto"]).update(
                            estado=p["estado"][:20], laser=p["laser"][:20], tx_power=p["tx_power"],
                            leido=timezone.now(), **({"habilitado": p["habilitado"]} if p["habilitado"] is not None else {}))
            except Exception:
                pass  # la lectura es un extra: el cambio ya se hizo
    partes = []
    if habilitar is not None:
        partes.append("habilitado" if habilitar else "deshabilitado")
    if autofind is not None:
        partes.append("autofind " + ("activado" if autofind else "desactivado"))
    return f"{len(puertos)} puerto(s) PON: {', '.join(partes)}"


def reiniciar_onus_pon(olt, frame, slot, puerto):
    """Reinicia (ont reset) todas las ONUs autorizadas de un puerto PON."""
    verificar_escritura(olt)
    ids = list(olt.onus.filter(frame=frame, slot=slot, puerto=puerto, ont_id__isnull=False,
                               estado="activa").values_list("ont_id", flat=True))
    if not ids:
        return f"{frame}/{slot}/{puerto}: no hay ONUs activas para reiniciar"
    with huawei.conectar(olt) as con:
        ok, mal = huawei.reiniciar_onus_puerto(con, frame, slot, puerto, ids)
    texto = f"{frame}/{slot}/{puerto}: {len(ok)} ONU(s) reiniciadas"
    return texto + (f"; no respondieron: {', '.join(map(str, mal))}" if mal else "")


def leer_vlans_olt(olt):
    """Solo lectura: trae las VLANs de la OLT ('display vlan all') a la pestaña VLANs."""
    with huawei.conectar(olt) as con:
        vlans = huawei.leer_vlans(con)
    vistas = set()
    for v in vlans:
        VLAN.objects.update_or_create(olt=olt, vlan_id=v["vlan"], defaults={
            "en_olt": True, "tipo_olt": v["tipo"][:10], "service_ports": v["service_ports"]})
        vistas.add(v["vlan"])
    # Las que están en el sistema pero ya no en la OLT
    olt.vlans.exclude(vlan_id__in=vistas).update(en_olt=False, service_ports=None)
    return f"{len(vlans)} VLANs leídas de la OLT"


def crear_vlans_olt(olt, vlans):
    """vlans = [[vlan_id, descripcion], ...]. Las crea en la OLT ('vlan V smart')."""
    verificar_escritura(olt)
    with huawei.conectar(olt) as con:
        for vlan_id, desc in vlans:
            huawei.crear_vlan(con, vlan_id, desc)
            VLAN.objects.filter(olt=olt, vlan_id=vlan_id).update(en_olt=True)
    return f"{len(vlans)} VLAN(s) creadas en la OLT"


def leer_perfiles_ont_olt(olt):
    """Solo lectura (sirve en OLTs de SmartOLT): line/srv profiles por nombre, con su GEM y VLANs."""
    from django.utils import timezone
    with huawei.conectar(olt) as con:
        d = huawei.leer_perfiles_ont(con)
    ahora = timezone.now()
    for tipo in ("line", "srv"):
        vistos = []
        for p in d[tipo]:
            datos = {"nombre": p["nombre"], "enlaces": p["enlaces"], "existe": True}
            if tipo == "line" and p.get("detalle"):
                datos.update(gems=p["gems"], vlans=p["vlans"], modo_mapeo=p["modo"][:20], leido=ahora)
            PerfilONT.objects.update_or_create(olt=olt, tipo=tipo, profile_id=p["id"], defaults=datos)
            vistos.append(p["id"])
        olt.perfiles_ont.filter(tipo=tipo).exclude(profile_id__in=vistos).update(existe=False)
    return f"{len(d['line'])} line-profiles y {len(d['srv'])} service-profiles"


ACCIONES_OLT = {
    "perfiles_ont": (leer_perfiles_ont_olt, False),  # paso 21
    "vlans": (leer_vlans_olt, False),       # paso 20
    "crear_vlan": (crear_vlans_olt, True),  # paso 20
    # accion: (función, ¿escribe en la OLT?)
    "puertos": (leer_puertos_olt, False),
    "puerto": (configurar_puertos, True),
    "reset_pon": (reiniciar_onus_pon, True),
}
