"""
Habla con la OLT Huawei (MA5600/MA5800) por SSH usando netmiko.
Aquí están SOLO los comandos que tú escribías a mano. Nada más.
"""
import logging
import re
from contextlib import contextmanager

from netmiko import ConnectHandler


class ErrorOLT(Exception):
    """La OLT respondió con un error (Failure, Parameter error, etc.)."""


# Palabras que la OLT usa cuando un comando falla
ERRORES = ("Failure", "Parameter error", "Unknown command", "Incomplete command")

# Validaciones: como armamos comandos con texto, nunca dejamos pasar
# caracteres raros (evita que alguien "inyecte" otro comando).
RE_SN = re.compile(r"^[0-9A-F]{16}$")
RE_DESC = re.compile(r"[^A-Za-z0-9_\-]")


def validar_sn(sn):
    sn = (sn or "").strip().upper()
    if not RE_SN.match(sn):
        raise ErrorOLT(f"SN inválido: {sn!r} (deben ser 16 caracteres hexadecimales)")
    return sn


def limpiar_desc(texto):
    return RE_DESC.sub("_", texto or "")[:32] or "SIN_DESC"


@contextmanager
def _silenciar(*nombres):
    """Baja temporalmente el volumen de los logs de netmiko/paramiko."""
    loggers = [logging.getLogger(n) for n in nombres]
    antes = [lg.level for lg in loggers]
    for lg in loggers:
        lg.setLevel(logging.CRITICAL + 1)
    try:
        yield
    finally:
        for lg, nivel in zip(loggers, antes):
            lg.setLevel(nivel)


@contextmanager
def conectar(olt):
    """Abre la sesión (SSH o Telnet), entra a enable + config, y SIEMPRE la cierra al final."""
    # Mismo código para las dos: netmiko solo cambia la forma de conectarse
    tipo = "huawei_olt_telnet" if olt.protocolo == "telnet" else "huawei_smartax"
    con = ConnectHandler(
        device_type=tipo,
        host=olt.host,
        port=olt.puerto_ssh,
        username=olt.usuario,
        password=olt.clave,
        conn_timeout=20,
    )
    try:
        con.enable()
        con.config_mode()
        yield con
    finally:
        # Al despedirse, la OLT cierra el socket antes de que netmiko termine;
        # eso imprime "Socket is closed" aunque todo salió bien. Lo silenciamos
        # SOLO durante el cierre (los errores reales se siguen viendo).
        with _silenciar("netmiko", "paramiko"):
            try:
                con.disconnect()
            except Exception:
                pass


def enviar(con, comando, permitir_error=False, timeout=30):
    """Envía un comando. Si la OLT pregunta (y/n), responde 'y'. Si hay error, lanza ErrorOLT."""
    salida = con.send_command(comando, expect_string=r"#\s*$|\(y/n\)", read_timeout=timeout)
    if "(y/n)" in salida:
        salida += con.send_command("y", expect_string=r"#\s*$", read_timeout=timeout)
    if not permitir_error and any(e in salida for e in ERRORES):
        raise ErrorOLT(f"> {comando}\n{salida.strip()}")
    return salida


# ---------------------------------------------------------------- lectura

def leer_autofind(con):
    """Devuelve una lista de ONUs sin autorizar: [{'frame','slot','puerto','sn','modelo'}]"""
    salida = enviar(con, "display ont autofind all", permitir_error=True)
    if "do not exist" in salida:
        return []
    onus = []
    # Cada ONU viene en un bloque que empieza con "Number : N"
    for bloque in re.split(r"\n\s*Number\s*:", salida)[1:]:
        fsp = re.search(r"F/S/P\s*:\s*(\d+)/(\d+)/(\d+)", bloque)
        sn = re.search(r"Ont SN\s*:\s*([0-9A-Fa-f]{16})", bloque)
        modelo = re.search(r"Ont EquipmentID\s*:\s*(\S+)", bloque)
        if fsp and sn:
            onus.append({
                "frame": int(fsp.group(1)),
                "slot": int(fsp.group(2)),
                "puerto": int(fsp.group(3)),
                "sn": sn.group(1).upper(),
                "modelo": modelo.group(1) if modelo else "",
            })
    if not onus and "Ont SN" in salida:
        # La OLT sí mostró ONUs pero no las pudimos leer: mejor fallar que vaciar "Sin configurar"
        raise ErrorOLT("No se pudo interpretar la salida de 'display ont autofind all':\n" + salida[:800])
    return onus


def buscar_service_port(con, frame, slot, puerto, ont_id, vlan):
    """Busca el índice del service-port de una ONU (para poder borrarlo luego)."""
    salida = enviar(con, f"display service-port port {frame}/{slot}/{puerto} ont {ont_id}",
                    permitir_error=True)
    m = re.search(rf"^\s*(\d+)\s+{vlan}\s", salida, re.M)
    return int(m.group(1)) if m else None


def buscar_por_sn(con, sn):
    """
    NUEVO (paso 14). 'display ont info by-sn SN' (solo lectura).
    Si la OLT YA tiene autorizada esa ONU devuelve {'frame','slot','puerto','ont_id'}; si no, None.
    Si la salida no se entiende (firmware distinto) también devuelve None: en el peor caso
    el 'ont add' fallará con "SN already exists", que es un error claro y no rompe nada.
    """
    sn = validar_sn(sn)
    salida = _mostrar(con, f"display ont info by-sn {sn}")
    fsp = re.search(r"F/S/P\s*:\s*(\d+)/(\d+)/(\d+)", salida)
    ont = re.search(r"ONT-ID\s*:\s*(\d+)", salida)
    if fsp and ont:
        return {"frame": int(fsp.group(1)), "slot": int(fsp.group(2)),
                "puerto": int(fsp.group(3)), "ont_id": int(ont.group(1))}
    return None


# ---------------------------------------------------------------- velocidad (paso 18)

TT_ILIMITADA = 6  # traffic table de fábrica de Huawei: sin límite


def comando_service_port(frame, slot, puerto, ont_id, plan, trafico=(None, None), indice=None):
    """
    El 'service-port' une la ONU (su GEM port) con la VLAN que sale hacia el MikroTik.
    trafico = (indice_subida, indice_bajada) de traffic table. Subida = INBOUND (lo que el cliente
    manda), bajada = OUTBOUND (lo que el cliente recibe). None = ilimitado (tabla 6 de fábrica).
    """
    cmd = (f"service-port {indice} vlan" if indice is not None else "service-port vlan")
    cmd += (f" {plan.vlan} gpon {frame}/{slot}/{puerto} ont {ont_id} gemport {plan.gemport} "
            f"multi-service user-vlan {plan.vlan} tag-transform translate")
    sube, baja = trafico
    if sube is not None or baja is not None:
        cmd += (f" inbound traffic-table index {TT_ILIMITADA if sube is None else sube}"
                f" outbound traffic-table index {TT_ILIMITADA if baja is None else baja}")
    return cmd


def asegurar_traffic_table(con, perfil):
    """
    Deja creada en la OLT la traffic table de un Perfil de velocidad y devuelve su índice.
    None (ilimitado) -> None. Si el índice ya existe con OTRO nombre, NO lo pisa: avisa.
    """
    if perfil is None:
        return None
    salida = _mostrar(con, f"display traffic table ip index {perfil.indice_olt}")
    if perfil.nombre_olt in salida:
        return perfil.indice_olt  # ya estaba
    if re.search(r"not exist|Failure", salida, re.I):
        enviar(con, perfil.comando_huawei())
        return perfil.indice_olt
    raise ErrorOLT(
        f"La traffic table {perfil.indice_olt} ya existe en la OLT con otro nombre (no es "
        f"{perfil.nombre_olt}). Cambia el 'Índice de traffic table' del perfil {perfil.nombre} "
        f"por uno libre.\n{salida.strip()[:600]}")


def cambiar_trafico(con, frame, slot, puerto, ont_id, plan, indice, trafico):
    """
    Cambia la velocidad de una ONU ya autorizada: se borra su service-port y se vuelve a crear
    IGUAL (mismo índice, misma VLAN) pero con las nuevas traffic tables. Corte de ~1 segundo.
    Si el nuevo falla, se vuelve a crear como estaba (sin límite) para no dejar al cliente sin servicio.
    Devuelve el índice del service-port.
    """
    if indice is None:
        indice = buscar_service_port(con, frame, slot, puerto, ont_id, plan.vlan)
    if indice is not None:
        enviar(con, f"undo service-port {indice}")
    try:
        enviar(con, comando_service_port(frame, slot, puerto, ont_id, plan, trafico, indice))
    except ErrorOLT:
        enviar(con, comando_service_port(frame, slot, puerto, ont_id, plan, (None, None), indice),
               permitir_error=True)
        raise
    nuevo = buscar_service_port(con, frame, slot, puerto, ont_id, plan.vlan)
    enviar(con, "save", permitir_error=True, timeout=120)
    return nuevo if nuevo is not None else indice


def leer_perfiles(con):
    """Solo lectura: lo que la OLT tiene configurado (texto tal cual, para verlo en el panel)."""
    salida = {}
    for clave, comando in (("lineprofiles", "display ont-lineprofile gpon all"),
                           ("srvprofiles", "display ont-srvprofile gpon all"),
                           ("traffic", "display traffic table ip from-index 0")):
        try:
            salida[clave] = _mostrar(con, comando, timeout=60).strip()[:20000]
        except Exception as e:  # un comando que no exista en este firmware no rompe el resto
            salida[clave] = f"(no se pudo leer: {e})"
    return salida


# ---------------------------------------------------------------- escritura

def autorizar_onu(con, frame, slot, puerto, sn, plan, descripcion, modo="bridging", trafico=(None, None)):
    """
    Lo mismo que hiciste a mano:
      interface gpon F/S -> ont add -> ont port native-vlan -> quit -> service-port -> save
    Devuelve (ont_id, indice_service_port).
    Si algo falla después del 'ont add', borra la ONU para no dejar nada a medias.
    """
    sn = validar_sn(sn)
    desc = limpiar_desc(descripcion)

    enviar(con, f"interface gpon {frame}/{slot}")
    salida = enviar(con,
        f'ont add {puerto} sn-auth "{sn}" omci '
        f"ont-lineprofile-id {plan.lineprofile_id} ont-srvprofile-id {plan.srvprofile_id} "
        f'desc "{desc}"')
    m = re.search(r"ONTID\s*:\s*(\d+)", salida)
    if not m:
        enviar(con, "quit", permitir_error=True)
        raise ErrorOLT(f"La OLT no devolvió ONTID:\n{salida}")
    ont_id = int(m.group(1))

    try:
        if modo != "routing":
            # Bridging: el puerto LAN 1 de la ONU sale directo a la VLAN (el router del cliente marca PPPoE).
            # En ONUs que no son Huawei (HGU) esto a veces falla; no es grave.
            enviar(con, f"ont port native-vlan {puerto} {ont_id} eth 1 vlan {plan.vlan}",
                   permitir_error=True)
        enviar(con, "quit")
        enviar(con, comando_service_port(frame, slot, puerto, ont_id, plan, trafico))
        indice = buscar_service_port(con, frame, slot, puerto, ont_id, plan.vlan)
    except Exception:
        # Deshacer: quitar la ONU para que no quede a medias
        enviar(con, f"interface gpon {frame}/{slot}", permitir_error=True)
        enviar(con, f"ont delete {puerto} {ont_id}", permitir_error=True)
        enviar(con, "quit", permitir_error=True)
        raise

    enviar(con, "save", permitir_error=True, timeout=120)
    return ont_id, indice


def completar_onu(con, frame, slot, puerto, ont_id, plan, modo="bridging", trafico=(None, None)):
    """
    NUEVO (paso 14). La ONU YA está en la OLT (un intento anterior sí llegó, o la pusieron a mano):
    NO se vuelve a hacer 'ont add'. Solo se crea el service-port si le falta.
    Devuelve (ont_id, indice_service_port).
    """
    indice = buscar_service_port(con, frame, slot, puerto, ont_id, plan.vlan)
    if indice is None:
        if modo != "routing":
            _en_interfaz(con, frame, slot, f"ont port native-vlan {puerto} {ont_id} eth 1 vlan {plan.vlan}",
                         permitir_error=True)
        enviar(con, comando_service_port(frame, slot, puerto, ont_id, plan, trafico))
        indice = buscar_service_port(con, frame, slot, puerto, ont_id, plan.vlan)
        enviar(con, "save", permitir_error=True, timeout=120)
    return ont_id, indice


RE_CREDENCIAL = re.compile(r"^[A-Za-z0-9._@-]{1,64}$")


def configurar_modo(con, frame, slot, puerto, ont_id, vlan, modo, pppoe=None):
    """
    NUEVO (paso 17). ONU mode, como en SmartOLT:
      - bridging: no hace nada más (ya se puso 'ont port native-vlan ... eth 1').
      - routing:  la ONU hace de router. Por OMCI se le crea la WAN en la VLAN del plan:
            ont ipconfig P ID ip-index 0 pppoe vlan V priority 0 user-account username U password C
            (o 'dhcp vlan V priority 0' si no hay cliente con PPPoE)
            ont internet-config P ID ip-index 0
            ont wan-config P ID ip-index 0 profile-id 0
    NO falla la autorización si la ONU no acepta la WAN (pasa con ONUs que no son Huawei):
    devuelve un aviso para configurarla a mano en la web de la ONU. Devuelve "" si todo fue bien.
    La clave PPPoE NUNCA se copia en el aviso (se guarda en la base de datos y en el registro de tareas).
    """
    if modo != "routing":
        return ""
    if pppoe:
        usuario, clave = pppoe
        if not (RE_CREDENCIAL.match(usuario or "") and RE_CREDENCIAL.match(clave or "")):
            return ("Routing: el usuario o la clave PPPoE tienen espacios o símbolos que la OLT no acepta "
                    "por comando. Configura la WAN PPPoE en la web de la ONU.")
        wan = (f"ont ipconfig {puerto} {ont_id} ip-index 0 pppoe vlan {vlan} priority 0 "
               f"user-account username {usuario} password {clave}")
        tipo = f"PPPoE ({usuario})"
    else:
        wan = f"ont ipconfig {puerto} {ont_id} ip-index 0 dhcp vlan {vlan} priority 0"
        tipo = "DHCP (sin cliente asignado)"
    fallos = []
    enviar(con, f"interface gpon {frame}/{slot}")
    try:
        for comando, nombre in ((wan, "ont ipconfig"),
                                (f"ont internet-config {puerto} {ont_id} ip-index 0", "ont internet-config"),
                                (f"ont wan-config {puerto} {ont_id} ip-index 0 profile-id 0", "ont wan-config")):
            salida = enviar(con, comando, permitir_error=True)
            if any(e in salida for e in ERRORES):
                linea = next((l.strip() for l in salida.splitlines() if any(e in l for e in ERRORES)), "")
                fallos.append(f"{nombre}: {linea[:120]}")
                if nombre == "ont ipconfig":
                    break  # sin WAN no tiene sentido seguir
    finally:
        enviar(con, "quit", permitir_error=True)
    enviar(con, "save", permitir_error=True, timeout=120)
    if fallos:
        return ("Routing: la ONU quedó autorizada, pero no aceptó la WAN por OMCI ("
                + "; ".join(fallos) + "). Si no es una ONU Huawei, configura la WAN en su web "
                "o autorízala en Bridging.")
    return ""


def eliminar_onu(con, frame, slot, puerto, ont_id, indice_service_port=None, vlan=None):
    """Baja completa: borra el service-port y luego la ONU. La ONU vuelve a salir en autofind."""
    if indice_service_port is None and vlan is not None:
        indice_service_port = buscar_service_port(con, frame, slot, puerto, ont_id, vlan)
    if indice_service_port is not None:
        enviar(con, f"undo service-port {indice_service_port}")
    enviar(con, f"interface gpon {frame}/{slot}")
    try:
        enviar(con, f"ont delete {puerto} {ont_id}")
    finally:
        enviar(con, "quit", permitir_error=True)
    enviar(con, "save", permitir_error=True, timeout=120)


# ---------------------------------------------------------------- ficha de la ONU (paso 9)

def _en_interfaz(con, frame, slot, comando, permitir_error=False):
    """Entra a 'interface gpon F/S', ejecuta UN comando y SIEMPRE sale con 'quit'."""
    enviar(con, f"interface gpon {frame}/{slot}")
    try:
        return enviar(con, comando, permitir_error=permitir_error)
    finally:
        enviar(con, "quit", permitir_error=True)


def _numero(patron, texto):
    m = re.search(patron, texto, re.I | re.M)
    try:
        return float(m.group(1)) if m else None
    except ValueError:
        return None


def leer_senal(con, frame, slot, puerto, ont_id):
    """
    'display ont optical-info P ID' (solo lectura).
    Devuelve dict con rx, tx, olt_rx, temperatura y en_linea.
    Si la ONU está apagada/desconectada, en_linea=False y los valores en None.
    """
    salida = _en_interfaz(con, frame, slot, f"display ont optical-info {puerto} {ont_id}",
                          permitir_error=True)
    if re.search(r"not online|offline|does not exist", salida, re.I):
        return {"en_linea": False, "rx": None, "tx": None, "olt_rx": None, "temperatura": None}
    return {
        "en_linea": True,
        "rx": _numero(r"^\s*Rx optical power\(dBm\)\s*:\s*(-?[\d.]+)", salida),
        "tx": _numero(r"Tx optical power\(dBm\)\s*:\s*(-?[\d.]+)", salida),
        "olt_rx": _numero(r"OLT Rx ONT optical power\(dBm\)\s*:\s*(-?[\d.]+)", salida),
        "temperatura": _numero(r"Temperature\(C\)\s*:\s*(-?[\d.]+)", salida),
    }


def reiniciar_onu(con, frame, slot, puerto, ont_id):
    """'ont reset P ID' (la OLT pregunta y/n; enviar() responde 'y')."""
    _en_interfaz(con, frame, slot, f"ont reset {puerto} {ont_id}")


def desactivar_onu(con, frame, slot, puerto, ont_id):
    """'ont deactivate P ID': la ONU queda sin servicio (corte físico). Se guarda con 'save'."""
    _en_interfaz(con, frame, slot, f"ont deactivate {puerto} {ont_id}")
    enviar(con, "save", permitir_error=True, timeout=120)


def activar_onu(con, frame, slot, puerto, ont_id):
    """'ont activate P ID': devuelve el servicio. Se guarda con 'save'."""
    _en_interfaz(con, frame, slot, f"ont activate {puerto} {ont_id}")
    enviar(con, "save", permitir_error=True, timeout=120)


# ---------------------------------------------------------------- info de la OLT (paso 10)
# Todo esto es SOLO LECTURA: sirve también para las OLTs en modo solo lectura.

def _mostrar(con, comando, timeout=30):
    """Como enviar(), pero si la OLT pide parámetros opcionales ('{ <cr>|... }:') responde Enter."""
    salida = con.send_command(comando, expect_string=r"#\s*$|\}:\s*$", read_timeout=timeout)
    if salida.rstrip().endswith("}:"):
        salida += con.send_command("", expect_string=r"#\s*$", read_timeout=timeout)
    return salida


def _tipo_tarjeta(nombre):
    n = nombre.upper()
    # GPxx = GPON; CGHF/CSHF = Combo (GPON + XG-PON, MA5800); XGHD/XSHF = XG(S)-PON; TWEF = combo.
    # Todas se manejan con 'interface gpon F/S'.
    if any(x in n for x in ("GP", "CGH", "CSH", "XGH", "XSH", "TWE")):
        return "GPON"
    if "EP" in n:
        return "EPON"
    if any(x in n for x in ("MCU", "SCU", "MPL", "MPS", "CCU")):
        return "Control"
    if any(x in n for x in ("PW", "PIL", "PRT")):
        return "Energía"
    if any(x in n for x in ("GIU", "X2", "X1", "GIC", "NXE")):
        return "Uplink"
    return "Otra"


def _puertos_pon(nombre):
    """Cuántos puertos PON tiene la tarjeta (GPBD/GPBH/GPMD = 8; GPFD/GPHF/GPSF... = 16)."""
    n = nombre.upper()
    return 8 if any(x in n for x in ("GPBD", "GPBH", "GPMD", "GPBC")) else 16


def leer_tarjetas(salida):
    """Convierte 'display board 0' en una lista de tarjetas."""
    tarjetas = []
    for linea in salida.splitlines():
        m = re.match(r"^\s*(\d{1,2})\s+(H\w+)\s+(\S+)", linea)
        if not m:
            continue
        slot, nombre, estado = int(m.group(1)), m.group(2), m.group(3)
        tipo = _tipo_tarjeta(nombre)
        tarjetas.append({"slot": slot, "nombre": nombre, "estado": estado, "tipo": tipo,
                         "puertos": _puertos_pon(nombre) if tipo == "GPON" else 0})
    return tarjetas


def _segundos_uptime(texto):
    """'Uptime is 125 day(s), 3 hour(s), 22 minute(s), 10 second(s)' -> segundos."""
    total = 0
    for valor, unidad in re.findall(r"(\d+)\s*(day|hour|minute|second)", texto or "", re.I):
        total += int(valor) * {"day": 86400, "hour": 3600, "minute": 60, "second": 1}[unidad.lower()]
    return total or None


def leer_info(con):
    """'display version' + 'display board 0'. Devuelve modelo, versión, uptime y tarjetas."""
    ver = _mostrar(con, "display version")
    modelo = re.search(r"PRODUCT\s*:\s*(\S+)", ver)
    version = re.search(r"VERSION\s*:\s*(\S+)", ver)
    uptime = re.search(r"Uptime is\s+(.+)", ver, re.I)
    tablero = _mostrar(con, "display board 0")
    return {
        "modelo": modelo.group(1) if modelo else "",
        "version": version.group(1) if version else "",
        "uptime_segundos": _segundos_uptime(uptime.group(1)) if uptime else None,
        "tarjetas": leer_tarjetas(tablero),
        "perfiles": leer_perfiles(con),  # paso 18
    }


# ---------------------------------------------------------------- perfiles ONT por nombre (paso 21)

def lista_perfiles(texto):
    """
    'display ont-lineprofile gpon all' (o srvprofile) -> [{id, nombre, enlaces}].
    Filas:   10          SMARTOLT_FLEXIBLE_GPON        12
    """
    salida = []
    for m in re.finditer(r"^\s*(\d+)\s+(\S+)\s+(\d+)\s*$", texto or "", re.M):
        salida.append({"id": int(m.group(1)), "nombre": m.group(2)[:64], "enlaces": int(m.group(3))})
    return salida


def detalle_lineprofile(texto):
    """
    'display ont-lineprofile gpon profile-id N' -> {gems, vlans, modo}.
      Mapping mode   : VLAN          <Gem Index 1>          (tabla Mapping: index  VLAN  Priority ...)
    """
    gems = sorted({int(g) for g in re.findall(r"<\s*Gem\s+Index\s+(\d+)\s*>", texto or "", re.I)})
    modo = re.search(r"Mapping\s+mode\s*:\s*(\S+)", texto or "", re.I)
    vlans, en_tabla = set(), False
    for linea in (texto or "").splitlines():
        if re.search(r"^\s*Mapping\s+VLAN", linea, re.I) or re.search(r"^\s*index\s+VLAN", linea, re.I):
            en_tabla = True
            continue
        if en_tabla:
            m = re.match(r"^\s*\|?\s*(\d+)\s+(\d+|-)\s+", linea)
            if m:
                if m.group(2) != "-":
                    vlans.add(int(m.group(2)))
            elif re.match(r"^\s*-{5,}", linea) or re.match(r"^\s*index", linea, re.I) or not linea.strip():
                continue
            else:
                en_tabla = False
    return {"gems": gems, "vlans": sorted(vlans), "modo": (modo.group(1).lower() if modo else "")}


def leer_perfiles_ont(con, max_detalle=60):
    """Solo lectura: line y service profiles de la OLT, con GEM y VLANs de cada line-profile."""
    lineas = lista_perfiles(_mostrar(con, "display ont-lineprofile gpon all", timeout=60))
    servicios = lista_perfiles(_mostrar(con, "display ont-srvprofile gpon all", timeout=60))
    for p in lineas[:max_detalle]:
        try:
            p.update(detalle_lineprofile(_mostrar(con, f"display ont-lineprofile gpon profile-id {p['id']}", timeout=60)))
            p["detalle"] = True
        except Exception:
            p["detalle"] = False
    return {"line": lineas, "srv": servicios}


# ---------------------------------------------------------------- VLANs (paso 20)

def leer_vlans(con):
    """
    Solo lectura: 'display vlan all'. Devuelve [{vlan, tipo, service_ports}].
    Filas tipo:   100  smart     common       1        12   -
    """
    salida = _mostrar(con, "display vlan all", timeout=60)
    vlans = []
    for m in re.finditer(r"^\s*(\d{1,4})\s+(smart|mux|standard|super)\s+\S+\s+(\d+)\s+(\d+)", salida, re.M | re.I):
        vlans.append({"vlan": int(m.group(1)), "tipo": m.group(2).lower(), "service_ports": int(m.group(4))})
    return vlans


def crear_vlan(con, vlan_id, descripcion=""):
    """'vlan V smart' (si ya existe, la OLT responde que existe y se sigue) + descripción."""
    salida = enviar(con, f"vlan {int(vlan_id)} smart", permitir_error=True)
    if re.search(r"Failure|Error", salida) and not re.search(r"exist", salida, re.I):
        raise ErrorOLT(f"> vlan {vlan_id} smart\n{salida.strip()}")
    desc = limpiar_desc(descripcion) if descripcion else ""
    if desc:
        enviar(con, f"vlan desc {int(vlan_id)} description {desc}", permitir_error=True)
    enviar(con, "save", permitir_error=True, timeout=120)


# ---------------------------------------------------------------- puertos PON (paso 19)

def _bloques_clave_valor(texto):
    """
    Convierte la salida tipo
        F/S/P                       0/1/0
        Port state                  Online
    en una lista de dicts, uno por cada 'F/S/P'. Las claves quedan tal cual las escribe la OLT.
    """
    bloques, actual = [], None
    for linea in texto.splitlines():
        m = re.match(r"^\s*([A-Za-z][^:]*?)\s{2,}:?\s*(\S.*?)\s*$", linea) or \
            re.match(r"^\s*([A-Za-z][^:]*?)\s*:\s*(\S.*?)\s*$", linea)
        if not m:
            continue
        clave, valor = m.group(1).strip(), m.group(2).strip()
        if clave.upper() in ("F/S/P", "F/S/P/"):
            actual = {"F/S/P": valor}
            bloques.append(actual)
        elif actual is not None:
            actual[clave] = valor
    return bloques


def _buscar(d, *patrones):
    """Primer valor del dict cuya clave coincide con alguno de los patrones (sin mayúsculas)."""
    for clave, valor in d.items():
        if any(re.search(p, clave, re.I) for p in patrones):
            return valor
    return None


def _flotante(texto):
    m = re.search(r"-?\d+(?:\.\d+)?", texto or "")
    return float(m.group()) if m else None


def leer_puertos(con, frame, slot):
    """
    Solo lectura: 'display port state all' dentro de 'interface gpon F/S'.
    Devuelve [{puerto, estado, laser, habilitado, autofind, tx_power, distancia_max, datos}].
    Lo que este firmware no muestre queda en None (el panel pone '—').
    """
    enviar(con, f"interface gpon {frame}/{slot}")
    try:
        salida = _mostrar(con, "display port state all", timeout=60)
    finally:
        enviar(con, "quit", permitir_error=True)
    puertos = []
    for b in _bloques_clave_valor(salida):
        try:
            puerto = int(b["F/S/P"].split("/")[-1])
        except ValueError:
            continue
        estado = _buscar(b, r"^port state") or ""
        laser = _buscar(b, r"laser") or ""
        auto = _buscar(b, r"auto.?find")
        apagado = re.search(r"shut|disable|close|off\b", f"{estado} {laser}", re.I)
        puertos.append({
            "puerto": puerto,
            "estado": estado,
            "laser": laser,
            "habilitado": None if not (estado or laser) else not apagado,
            "autofind": None if auto is None else bool(re.search(r"enable", auto, re.I)),
            "tx_power": _flotante(_buscar(b, r"^tx power|launch optical power|^tx optical power")),
            "distancia_max": _flotante(_buscar(b, r"max.*distance")),
            "datos": b,
        })
    return puertos


def configurar_puerto(con, frame, slot, puerto, habilitar=None, autofind=None):
    """
    Escritura en un puerto PON. None = no tocar.
      habilitar=True  -> 'undo shutdown P'      habilitar=False -> 'shutdown P' (¡corta a todo el puerto!)
      autofind=True   -> 'port P ont-auto-find enable'   False -> '... disable'
    """
    enviar(con, f"interface gpon {frame}/{slot}")
    try:
        if habilitar is not None:
            enviar(con, f"undo shutdown {puerto}" if habilitar else f"shutdown {puerto}")
        if autofind is not None:
            enviar(con, f"port {puerto} ont-auto-find {'enable' if autofind else 'disable'}")
    finally:
        enviar(con, "quit", permitir_error=True)
    enviar(con, "save", permitir_error=True, timeout=120)


def reiniciar_onus_puerto(con, frame, slot, puerto, ont_ids):
    """'ont reset P ID' para cada ONU del puerto. Devuelve (reiniciadas, fallidas)."""
    ok, mal = [], []
    enviar(con, f"interface gpon {frame}/{slot}")
    try:
        for ont_id in ont_ids:
            try:
                enviar(con, f"ont reset {puerto} {ont_id}")
                ok.append(ont_id)
            except ErrorOLT:
                mal.append(ont_id)  # ej. la ONU está apagada: seguimos con las demás
    finally:
        enviar(con, "quit", permitir_error=True)
    return ok, mal



