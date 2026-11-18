from django.db import models


class Router(models.Model):
    METODOS = [("api_ssl", "API-SSL (cifrada, puerto 8729)"),
               ("api", "API sin cifrar (puerto 8728)"),
               ("rest", "REST por HTTPS (www-ssl, puerto 443)")]

    nombre = models.CharField(max_length=100)
    ip = models.GenericIPAddressField()
    usuario = models.CharField(max_length=50, default="django-api")
    clave = models.CharField(max_length=100)  # TEMPORAL: luego la ciframos
    # NUEVO (paso 12): cómo se conecta el sistema a este router
    metodo = models.CharField("Conexión", max_length=8, choices=METODOS, default="rest")
    puerto = models.PositiveIntegerField(null=True, blank=True,
                                         help_text="Vacío = el puerto normal del método (8729, 8728 o 443)")
    huella_ssl = models.CharField("Huella SHA-256 del certificado", max_length=100, blank=True,
                                  help_text="Opcional, solo API-SSL. Si la pones, se comprueba en cada conexión.")
    huella_vista = models.CharField(max_length=64, blank=True,
                                    help_text="Huella que presentó el MikroTik en la última prueba")

    # NUEVO (paso 11): lo que se lee con "Probar conexión" (solo lectura)
    ACCIONES = [("", "—"), ("info", "Leyendo info del router")]
    modelo = models.CharField(max_length=60, blank=True)
    version = models.CharField(max_length=40, blank=True)
    encendido_desde = models.DateTimeField(null=True, blank=True)
    cpu = models.PositiveSmallIntegerField("CPU %", null=True, blank=True)
    memoria_libre = models.PositiveSmallIntegerField("Memoria libre %", null=True, blank=True)
    ppp_activos = models.PositiveIntegerField("PPPoE conectados", null=True, blank=True)
    conectado = models.BooleanField(null=True, blank=True)
    info_leida = models.DateTimeField(null=True, blank=True)
    accion_pendiente = models.CharField(max_length=10, choices=ACCIONES, blank=True, default="")
    accion_desde = models.DateTimeField(null=True, blank=True)
    ultimo_error = models.TextField(blank=True)

    def __str__(self):
        return f"{self.nombre} ({self.ip})"

    def conectar(self):
        """Úsalo con 'with router.conectar() as mk:' para que la conexión se cierre sola."""
        from .mikrotik import conectar
        return conectar(self)

    @property
    def puerto_efectivo(self):
        from .mikrotik import PUERTOS
        return self.puerto or PUERTOS.get(self.metodo, 443)


class Plan(models.Model):
    """Plan de internet (como en MikroWisp): nombre, velocidad y precio."""
    nombre = models.CharField(max_length=60, unique=True)          # ej. "Plan 20 Megas"
    subida = models.CharField(max_length=10, help_text="Ej. 10M, 512k")
    bajada = models.CharField(max_length=10, help_text="Ej. 20M")
    precio = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    descripcion = models.CharField(max_length=200, blank=True)
    activo = models.BooleanField(default=True, help_text="Los planes inactivos no salen al crear clientes")

    class Meta:
        ordering = ["precio", "nombre"]

    @property
    def velocidad(self):
        """Formato del MikroTik (max-limit): subida/bajada."""
        return f"{self.subida}/{self.bajada}"

    def __str__(self):
        return f"{self.nombre} · {self.velocidad} · ${self.precio}"


class Cliente(models.Model):
    ESTADOS = [("pendiente", "Pendiente"), ("activo", "Activo"), ("suspendido", "Suspendido")]
    ACCIONES = [("", "—"), ("crear", "Creando en MikroTik"),
                ("suspender", "Suspendiendo"), ("activar", "Activando"),
                ("plan", "Cambiando plan")]

    nombre = models.CharField(max_length=150)
    usuario_pppoe = models.CharField(max_length=50, unique=True)
    clave_pppoe = models.CharField(max_length=50)
    ip = models.GenericIPAddressField(unique=True)
    velocidad = models.CharField(max_length=20, default="10M/10M")  # la que está APLICADA en el MikroTik
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT, null=True, blank=True,
                             related_name="clientes")  # NUEVO (paso 9)
    router = models.ForeignKey(Router, on_delete=models.PROTECT, related_name="clientes")
    estado = models.CharField(max_length=20, choices=ESTADOS, default="pendiente")

    # NUEVO (paso 7): datos de contacto y control de la fila
    cedula = models.CharField("Cédula / RUC", max_length=13, blank=True)
    telefono = models.CharField("Teléfono", max_length=20, blank=True)
    direccion = models.CharField("Dirección", max_length=200, blank=True)
    accion_pendiente = models.CharField(max_length=10, choices=ACCIONES, blank=True, default="",
                                        help_text="Orden en la fila que todavía no termina")
    # NUEVO (paso 16): dónde está conectado en la red de fibra
    caja_nap = models.ForeignKey("CajaNAP", on_delete=models.PROTECT, null=True, blank=True,
                                 related_name="clientes", verbose_name="Caja NAP")
    puerto_nap = models.PositiveSmallIntegerField("Puerto NAP", null=True, blank=True)
    ultimo_error = models.TextField(blank=True)
    actualizado = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["caja_nap", "puerto_nap"], name="puerto_nap_unico")]

    def __str__(self):
        return self.nombre


# ---------------------------------------------------------------- Redes IPv4 (paso 15)

def _parsear_reservadas(texto):
    """'172.16.130.2, 172.16.130.250-172.16.130.254' -> conjunto de IPv4Address."""
    import ipaddress
    salida = set()
    for parte in (texto or "").replace(";", ",").replace("\n", ",").split(","):
        parte = parte.strip()
        if not parte:
            continue
        if "-" in parte:
            a, b = (ipaddress.IPv4Address(x.strip()) for x in parte.split("-", 1))
            if int(b) < int(a):
                a, b = b, a
            if int(b) - int(a) > 65535:
                raise ValueError(f"Rango demasiado grande: {parte}")
            salida.update(ipaddress.IPv4Address(i) for i in range(int(a), int(b) + 1))
        else:
            salida.add(ipaddress.IPv4Address(parte))
    return salida


class RedIPv4(models.Model):
    """
    Una red de IPs para los clientes de un router (como "Redes IPv4" de MikroWisp).
    Al crear un cliente sin IP, el sistema le da la siguiente IP libre de las redes de su router.
    El USO se calcula con las IPs de los clientes: si borras un cliente, su IP queda libre sola.
    """
    nombre = models.CharField(max_length=60, blank=True, help_text="Vacío = la red, ej. 172.16.130.0/24")
    router = models.ForeignKey(Router, on_delete=models.PROTECT, related_name="redes")
    red = models.GenericIPAddressField("Red", protocol="IPv4")
    cidr = models.PositiveSmallIntegerField("CIDR", default=24)
    gateway = models.GenericIPAddressField(
        "IP del MikroTik (gateway)", protocol="IPv4", null=True, blank=True,
        help_text="La IP del router en esta red. Se usa como local-address del PPPoE y nunca se "
                  "da a un cliente. Vacío = no se envía local-address (usa la del perfil).")
    reservadas = models.TextField(
        "IPs reservadas", blank=True,
        help_text="No se dan a clientes. Separadas por coma; rangos con guion. "
                  "Ej: 172.16.130.2, 172.16.130.250-172.16.130.254")
    activa = models.BooleanField("Usar para clientes nuevos", default=True)
    creada = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "red IPv4"
        verbose_name_plural = "redes IPv4"
        ordering = ["router__nombre", "red"]
        constraints = [models.UniqueConstraint(fields=["red", "cidr"], name="red_ipv4_unica")]

    def __str__(self):
        return self.nombre or self.cidr_texto

    @property
    def cidr_texto(self):
        return f"{self.red}/{self.cidr}"

    @property
    def red_ip(self):
        import ipaddress
        return ipaddress.IPv4Network(self.cidr_texto, strict=False)

    def hosts(self):
        """IPs usables. /32 = la propia IP; /31 = las dos; resto = sin red ni broadcast."""
        r = self.red_ip
        if r.prefixlen >= 31:
            return list(r)
        return r.hosts()  # generador: no arma listas enormes en redes grandes

    @property
    def total(self):
        r = self.red_ip
        return r.num_addresses if r.prefixlen >= 31 else r.num_addresses - 2

    def bloqueadas(self):
        """Gateway + reservadas (las que nunca se dan a clientes)."""
        import ipaddress
        s = _parsear_reservadas(self.reservadas)
        if self.gateway:
            s.add(ipaddress.IPv4Address(self.gateway))
        elif self.red_ip.prefixlen <= 30:
            # Paso 16: sin gateway escrito, la primera IP (.1) se guarda para el router, como MikroWisp
            s.add(next(iter(self.red_ip.hosts())))
        return {ip for ip in s if ip in self.red_ip}

    def contiene(self, ip):
        import ipaddress
        try:
            return ipaddress.IPv4Address(str(ip)) in self.red_ip
        except ValueError:
            return False

    def siguiente_libre(self, usadas):
        """Primera IP que no está en 'usadas' (conjunto de IPv4Address) ni bloqueada."""
        fuera = usadas | self.bloqueadas()
        for ip in self.hosts():
            if ip not in fuera:
                return str(ip)
        return None


def ips_de_clientes(excluir_pk=None):
    """Todas las IPs que ya tienen clientes (de cualquier router), como IPv4Address."""
    import ipaddress
    qs = Cliente.objects.all()
    if excluir_pk:
        qs = qs.exclude(pk=excluir_pk)
    salida = set()
    for ip in qs.values_list("ip", flat=True):
        try:
            salida.add(ipaddress.IPv4Address(ip))
        except ValueError:
            pass  # IPv6 u otra cosa rara: no cuenta
    return salida


def red_de(router_id, ip):
    """La red (del router) que contiene esa IP, o None."""
    for red in RedIPv4.objects.filter(router_id=router_id):
        if red.contiene(ip):
            return red
    return None


def asignar_ip(router, red=None):
    """
    Siguiente IP libre para un cliente nuevo de 'router'.
    Si se indica 'red', solo de esa; si no, de la primera red ACTIVA del router con espacio.
    Devuelve (ip, red). Lanza ValueError si no hay ninguna libre.
    """
    usadas = ips_de_clientes()
    redes = [red] if red else sorted(RedIPv4.objects.filter(router=router, activa=True),
                                     key=lambda r: int(r.red_ip.network_address))
    if not redes:
        raise ValueError(f"{router.nombre} no tiene Redes IPv4 activas: crea una en Redes IPv4 "
                         f"o escribe la IP a mano.")
    for r in redes:
        ip = r.siguiente_libre(usadas)
        if ip:
            return ip, r
    nombres = ", ".join(str(r) for r in redes)
    raise ValueError(f"No quedan IPs libres en {nombres}. Agrega otra red al router.")



# ---------------------------------------------------------------- Cajas NAP (paso 16)

class CajaNAP(models.Model):
    """Caja NAP de la red de fibra (como en MikroWisp): cada cliente ocupa un puerto."""
    nombre = models.CharField(max_length=60, unique=True)
    coordenadas = models.CharField(max_length=60, blank=True, help_text="Latitud,longitud. Ej: -0.126572,-76.340706")
    ubicacion = models.CharField("Ubicación", max_length=150, blank=True)
    puertos = models.PositiveSmallIntegerField(default=16)
    detalles = models.TextField(blank=True)
    creada = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "caja NAP"
        verbose_name_plural = "cajas NAP"
        ordering = ["nombre"]

    def __str__(self):
        return self.nombre

    def ocupados(self):
        """{puerto: cliente} de esta caja."""
        return {c.puerto_nap: c for c in self.clientes.exclude(puerto_nap=None)}

    def puertos_libres(self, excepto_cliente=None):
        usados = set(self.clientes.exclude(puerto_nap=None)
                     .exclude(pk=getattr(excepto_cliente, "pk", None))
                     .values_list("puerto_nap", flat=True))
        return [p for p in range(1, self.puertos + 1) if p not in usados]

    @property
    def enlace_mapa(self):
        if not self.coordenadas:
            return ""
        return "https://www.google.com/maps?q=" + self.coordenadas.replace(" ", "")
