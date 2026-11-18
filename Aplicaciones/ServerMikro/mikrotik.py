"""
Habla con el MikroTik. Hay 3 formas de conectarse (se elige en cada router):

  - "api_ssl": API de RouterOS cifrada (servicio api-ssl, puerto 8729).  <- RECOMENDADA
  - "api":     API de RouterOS SIN cifrar (servicio api, puerto 8728). La clave viaja en claro.
  - "rest":    API REST por HTTPS (servicio www-ssl, puerto 443).

Las tres tienen los MISMOS métodos, así servicios.py no sabe ni le importa cuál se usa.
Se usan siempre con "with", para que la conexión se cierre al terminar:

    with router.conectar() as mk:
        mk.suspender_cliente("jperez")
"""
import hashlib
import re
import ssl
import unicodedata

import requests
import urllib3

urllib3.disable_warnings()  # REST con certificado propio del MikroTik (sin CA)


def limpiar_texto(texto):
    # "Cedeño Peña" -> "Cedeno Pena"  (la API de RouterOS solo acepta ASCII)
    return unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode("ascii")


class MikrotikError(Exception):
    pass


PUERTOS = {"api_ssl": 8729, "api": 8728, "rest": 443}


def conectar(router):
    """Devuelve la clase correcta según el método del router."""
    metodo = getattr(router, "metodo", "rest") or "rest"
    puerto = getattr(router, "puerto", None) or PUERTOS[metodo]
    huella = getattr(router, "huella_ssl", "") or ""
    if metodo == "rest":
        return MikrotikREST(router.ip, router.usuario, router.clave, puerto=puerto)
    return MikrotikAPI(router.ip, router.usuario, router.clave, puerto=puerto,
                       usar_ssl=(metodo == "api_ssl"), huella=huella)


class _Base:
    """Lo que es igual en REST y en API."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.cerrar()

    def cerrar(self):
        pass

    def crear_cliente(self, usuario, clave, ip, velocidad, nombre, local=None):
        comentario = limpiar_texto(nombre)
        secreto = {"name": usuario, "password": clave, "service": "pppoe",
                   "profile": "default", "remote-address": ip, "comment": comentario}
        if local:  # paso 15: IP del MikroTik en la red del cliente (gateway de la Red IPv4)
            secreto["local-address"] = local
        self._agregar("/ppp/secret", secreto)
        self._agregar("/queue/simple", {"name": f"q_{usuario}", "target": f"{ip}/32",
                                        "max-limit": velocidad, "comment": comentario})

    def suspender_cliente(self, usuario):
        sid = self._buscar_id("/ppp/secret", usuario)
        if not sid:
            raise MikrotikError(f"No existe el cliente {usuario} en el router")
        self._cambiar("/ppp/secret", sid, {"disabled": "true"})
        aid = self._buscar_id("/ppp/active", usuario)  # si está conectado, lo desconecta
        if aid:
            self._quitar("/ppp/active", aid)

    def activar_cliente(self, usuario):
        sid = self._buscar_id("/ppp/secret", usuario)
        if not sid:
            raise MikrotikError(f"No existe el cliente {usuario} en el router")
        self._cambiar("/ppp/secret", sid, {"disabled": "false"})

    def cambiar_velocidad(self, usuario, velocidad):
        """Cambia el max-limit de la cola q_<usuario> (cambio de plan)."""
        qid = self._buscar_id("/queue/simple", f"q_{usuario}")
        if not qid:
            raise MikrotikError(f"No existe la cola q_{usuario} en el router")
        self._cambiar("/queue/simple", qid, {"max-limit": velocidad})


# ---------------------------------------------------------------- REST (www-ssl)

class MikrotikREST(_Base):
    def __init__(self, host, usuario, clave, puerto=443, timeout=10):
        self.base = f"https://{host}/rest" if puerto == 443 else f"https://{host}:{puerto}/rest"
        self.sesion = requests.Session()
        self.sesion.auth = (usuario, clave)
        self.sesion.verify = False
        self.timeout = timeout

    def cerrar(self):
        self.sesion.close()

    def _peticion(self, metodo, ruta, **kwargs):
        try:
            r = self.sesion.request(metodo, self.base + ruta, timeout=self.timeout, **kwargs)
        except requests.ConnectionError as e:
            raise MikrotikError(f"No se pudo abrir {self.base} (¿está activo el servicio www-ssl "
                                f"y permitido desde esta IP?): {e}") from e
        if r.status_code >= 400:
            raise MikrotikError(f"{r.status_code}: {r.text}")
        return r.json() if r.content else None

    def _buscar_id(self, ruta, nombre):
        res = self._peticion("GET", ruta, params={"name": nombre})
        return res[0][".id"] if res else None

    def _agregar(self, ruta, datos):
        self._peticion("PUT", ruta, json=datos)

    def _cambiar(self, ruta, id_, datos):
        self._peticion("PATCH", f"{ruta}/{id_}", json=datos)

    def _quitar(self, ruta, id_):
        self._peticion("DELETE", f"{ruta}/{id_}")

    def info(self):
        return self._peticion("GET", "/system/resource")

    def ppp_conectados(self):
        return len(self._peticion("GET", "/ppp/active", params={".proplist": "name"}) or [])


# ---------------------------------------------------------------- API de RouterOS (api / api-ssl)

class MikrotikAPI(_Base):
    """
    API "clásica" de RouterOS con la librería librouteros (py -m pip install librouteros).
    Con api-ssl el MikroTik usa su propio certificado (no lo firma ninguna CA), por eso no se
    valida contra CAs. Si guardas la HUELLA SHA-256 del certificado en el router, se comprueba
    en cada conexión: si alguien se pone en medio con otro certificado, se corta.
    """

    def __init__(self, host, usuario, clave, puerto=8729, usar_ssl=True, huella="", timeout=10):
        try:
            from librouteros import connect
            from librouteros.exceptions import LibRouterosError
        except ImportError as e:
            raise MikrotikError("Falta la librería: py -m pip install librouteros") from e
        self._Error = LibRouterosError
        self.huella_vista = ""
        destino = f"{host}:{puerto}"
        opciones = {"port": puerto, "timeout": timeout}
        if usar_ssl:
            opciones["ssl_wrapper"] = envoltura = self._envoltura_ssl(huella)
        try:
            self.api = connect(host=host, username=usuario, password=clave, **opciones)
            if usar_ssl:
                self.huella_vista = getattr(envoltura, "huella_vista", "")
        except MikrotikError:
            raise
        except ssl.SSLError as e:
            raise MikrotikError(f"Error SSL con {destino} (¿api-ssl tiene certificado asignado?): {e}") from e
        except OSError as e:
            raise MikrotikError(f"No se pudo abrir {destino} (¿servicio {'api-ssl' if usar_ssl else 'api'} "
                                f"activo y permitido desde esta IP en 'available from'?): {e}") from e
        except LibRouterosError as e:
            raise MikrotikError(f"El MikroTik rechazó el login en {destino}: {e}") from e

    @staticmethod
    def _envoltura_ssl(huella):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # certificado propio del MikroTik: se compara la huella abajo
        esperada = re.sub(r"[^0-9a-f]", "", (huella or "").lower())

        def envolver(sock):
            s = ctx.wrap_socket(sock)
            real = hashlib.sha256(s.getpeercert(binary_form=True)).hexdigest()
            envolver.huella_vista = real
            if esperada:
                if real != esperada:
                    s.close()
                    raise MikrotikError(
                        f"La huella del certificado NO coincide (recibida {real}). "
                        f"Si cambiaste el certificado del MikroTik, actualiza la huella en el router; "
                        f"si no, alguien podría estar en medio de la conexión.")
            return s
        return envolver

    def cerrar(self):
        try:
            self.api.close()
        except Exception:
            pass

    def _ejecutar(self, cmd, *palabras):
        try:
            return tuple(self.api.rawCmd(cmd, *palabras))
        except self._Error as e:
            raise MikrotikError(f"{cmd}: {e}") from e

    @staticmethod
    def _palabras(datos):
        return [f"={k}={v}" for k, v in datos.items()]

    def _buscar_id(self, ruta, nombre):
        filas = self._ejecutar(f"{ruta}/print", f"?name={nombre}", "=.proplist=.id")
        return filas[0].get(".id") if filas else None

    def _agregar(self, ruta, datos):
        self._ejecutar(f"{ruta}/add", *self._palabras(datos))

    def _cambiar(self, ruta, id_, datos):
        self._ejecutar(f"{ruta}/set", f"=.id={id_}", *self._palabras(datos))

    def _quitar(self, ruta, id_):
        self._ejecutar(f"{ruta}/remove", f"=.id={id_}")

    def info(self):
        filas = self._ejecutar("/system/resource/print")
        return filas[0] if filas else {}

    def ppp_conectados(self):
        return len(self._ejecutar("/ppp/active/print", "=.proplist=name"))


def segundos_uptime(texto):
    """'3w2d04:05:06' o '3w2d4h5m6s' (formato de RouterOS) -> segundos."""
    total = 0
    for valor, unidad in re.findall(r"(\d+)([wdhms])", texto or ""):
        total += int(valor) * {"w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1}[unidad]
    m = re.search(r"(\d+):(\d+):(\d+)$", texto or "")
    if m:
        total += int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
    return total or None
