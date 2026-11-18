from django.db import models


class OLT(models.Model):
    """Un equipo OLT. Datos para conectarse por SSH/Telnet y SNMP."""
    PROTOCOLOS = [("ssh", "SSH"), ("telnet", "Telnet")]

    nombre = models.CharField(max_length=50, unique=True)
    host = models.CharField(max_length=100)
    puerto_ssh = models.PositiveIntegerField("Puerto (SSH o Telnet)", default=22)
    usuario = models.CharField(max_length=50)
    clave = models.CharField(max_length=100)
    protocolo = models.CharField(max_length=6, choices=PROTOCOLOS, default="ssh")
    solo_lectura = models.BooleanField(
        default=False,
        help_text="Márcalo si esta OLT la maneja otro sistema (ej. SmartOLT): "
                  "solo se lee, nunca se autoriza ni se da de baja desde aquí.")
    snmp_comunidad = models.CharField(max_length=50, blank=True)
    snmp_puerto = models.PositiveIntegerField(default=161)
    activa = models.BooleanField(default=True)

    # NUEVO (paso 10): lo que se lee de la OLT con "Actualizar info" (solo lectura)
    ACCIONES = [("", "—"), ("info", "Leyendo info de la OLT"),
                # NUEVO (paso 19): órdenes de la pestaña Puertos PON
                ("puertos", "Leyendo puertos PON"), ("puerto", "Cambiando puertos PON"),
                ("reset_pon", "Reiniciando ONUs de un puerto"),
                # NUEVO (paso 20): pestaña VLANs
                ("vlans", "Leyendo VLANs"), ("crear_vlan", "Creando VLANs"),
                ("perfiles_ont", "Leyendo perfiles")]
    modelo_equipo = models.CharField("Modelo", max_length=40, blank=True)
    version = models.CharField("Versión", max_length=60, blank=True)
    encendida_desde = models.DateTimeField(null=True, blank=True)
    tarjetas = models.JSONField(default=list, blank=True)
    conectada = models.BooleanField(null=True, blank=True, help_text="Resultado de la última conexión")
    info_leida = models.DateTimeField(null=True, blank=True)
    accion_pendiente = models.CharField(max_length=12, choices=ACCIONES, blank=True, default="")
    accion_desde = models.DateTimeField(null=True, blank=True)
    ultimo_error = models.TextField(blank=True)
    # NUEVO (paso 18): copia de los perfiles que tiene la OLT (line/srv profiles, traffic tables)
    perfiles = models.JSONField(default=dict, blank=True)

    def __str__(self):
        return self.nombre

    MINUTOS_TRABADA = 5  # paso 20b: una orden de OLT que no termina en este tiempo se da por fallida

    @property
    def accion_trabada(self):
        from datetime import timedelta
        from django.utils import timezone
        return bool(self.accion_pendiente and (self.accion_desde is None or
                    self.accion_desde < timezone.now() - timedelta(minutes=self.MINUTOS_TRABADA)))

    def liberar_si_trabada(self):
        """
        Si la orden lleva más de MINUTOS_TRABADA sin terminar, la marca como fallida y libera la OLT.
        Pasa cuando el worker se cerró, se reinició, o tenía código viejo y no conocía la tarea
        (en su ventana sale "Received unregistered task"). Devuelve True si la liberó.
        """
        if not self.accion_trabada:
            return False
        texto = self.get_accion_pendiente_display()
        n = OLT.objects.filter(pk=self.pk, accion_pendiente=self.accion_pendiente).update(
            accion_pendiente="", accion_desde=None,
            ultimo_error=(f"«{texto}» no terminó en {self.MINUTOS_TRABADA} minutos. Lo más común: el worker se "
                          f"reinició o tenía el código viejo y no conocía la tarea (mira si su ventana dice "
                          f"'Received unregistered task'). Reinicia el worker y vuelve a pulsar el botón."))
        self.refresh_from_db()
        return bool(n)

    @property
    def tarjetas_pon(self):
        """Tarjetas PON (vuelve a clasificar por nombre, por si se leyeron con una versión vieja)."""
        from .huawei import _puertos_pon, _tipo_tarjeta
        salida = []
        for t in self.tarjetas or []:
            if _tipo_tarjeta(t.get("nombre", "")) == "GPON":
                salida.append(dict(t, tipo="GPON", puertos=t.get("puertos") or _puertos_pon(t["nombre"])))
        return salida


class PuertoPON(models.Model):
    """
    Paso 19: un puerto PON de la OLT, como la pestaña 'PON ports' de SmartOLT.
    Estado, láser, Tx y rango se leen de la OLT ('display port state all').
    La descripción es nuestra (solo se guarda aquí).
    """
    olt = models.ForeignKey(OLT, on_delete=models.CASCADE, related_name="puertos_pon")
    frame = models.PositiveSmallIntegerField(default=0)
    slot = models.PositiveSmallIntegerField()
    puerto = models.PositiveSmallIntegerField()
    tipo = models.CharField(max_length=5, default="GPON")
    descripcion = models.CharField("Descripción", max_length=60, blank=True,
                                   help_text="Ej. H1D5N11-N12-N13 (las cajas NAP de este puerto)")
    max_onus = models.PositiveSmallIntegerField("Máximo de ONUs", default=128,
                                                help_text="Para la barra de carga. GPON Huawei: 128.")
    habilitado = models.BooleanField(null=True, blank=True, help_text="None = todavía no se ha leído")
    autofind = models.BooleanField(null=True, blank=True)
    estado = models.CharField(max_length=20, blank=True)
    laser = models.CharField(max_length=20, blank=True)
    tx_power = models.FloatField("Tx (dBm)", null=True, blank=True)
    distancia_max = models.FloatField("Rango máx. (km)", null=True, blank=True)
    datos = models.JSONField(default=dict, blank=True, help_text="Todo lo que respondió la OLT")
    leido = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "puerto PON"
        verbose_name_plural = "puertos PON"
        ordering = ["olt", "frame", "slot", "puerto"]
        constraints = [models.UniqueConstraint(fields=["olt", "frame", "slot", "puerto"], name="puerto_pon_unico")]

    def __str__(self):
        return f"{self.olt} {self.fsp}"

    @property
    def fsp(self):
        return f"{self.frame}/{self.slot}/{self.puerto}"


class VLAN(models.Model):
    """
    Paso 20: VLANs de UNA OLT (como la pestaña VLANs de SmartOLT). La VLAN 500 de una OLT
    no tiene nada que ver con la VLAN 500 de otra: por eso cuelga de la OLT.
    Las casillas (gestión, IPTV, DHCP snooping, LAN-to-LAN) por ahora solo se guardan aquí.
    """
    olt = models.ForeignKey(OLT, on_delete=models.CASCADE, related_name="vlans")
    vlan_id = models.PositiveSmallIntegerField("VLAN-ID")
    descripcion = models.CharField("Descripción", max_length=60, blank=True)
    gestion = models.BooleanField("Gestión / TR069 / VoIP", default=False)
    iptv = models.BooleanField("Multicast (IPTV)", default=False)
    dhcp_snooping = models.BooleanField("DHCP snooping", default=False)
    lan_to_lan = models.BooleanField("LAN-to-LAN", default=False)
    # Lo que se leyó de la OLT con 'display vlan all'
    en_olt = models.BooleanField(null=True, blank=True, help_text="None = no se ha leído")
    tipo_olt = models.CharField(max_length=10, blank=True)
    service_ports = models.PositiveIntegerField(null=True, blank=True)
    creado = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "VLAN"
        verbose_name_plural = "VLANs"
        ordering = ["olt", "vlan_id"]
        constraints = [models.UniqueConstraint(fields=["olt", "vlan_id"], name="vlan_unica_por_olt")]

    def __str__(self):
        return f"{self.vlan_id}" + (f" - {self.descripcion}" if self.descripcion else "")


class PerfilONT(models.Model):
    """
    Paso 21: line-profile o service-profile que EXISTE en una OLT (se lee con
    'display ont-lineprofile/srvprofile gpon all'). Así se eligen por nombre, como en SmartOLT.
    """
    TIPOS = [("line", "Line-profile"), ("srv", "Service-profile")]
    olt = models.ForeignKey(OLT, on_delete=models.CASCADE, related_name="perfiles_ont")
    tipo = models.CharField(max_length=4, choices=TIPOS)
    profile_id = models.PositiveIntegerField("Profile-ID")
    nombre = models.CharField(max_length=64, blank=True)
    enlaces = models.PositiveIntegerField("ONUs que lo usan (binding times)", null=True, blank=True)
    # Solo line-profile: GEM ports y VLANs que deja pasar (de 'display ont-lineprofile gpon profile-id N')
    gems = models.JSONField(default=list, blank=True)
    vlans = models.JSONField(default=list, blank=True)
    modo_mapeo = models.CharField(max_length=20, blank=True, help_text="vlan, priority, vlan-priority…")
    existe = models.BooleanField(default=True, help_text="False = ya no apareció en la última lectura")
    leido = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "perfil ONT (line/srv)"
        verbose_name_plural = "perfiles ONT (line/srv)"
        ordering = ["olt", "tipo", "profile_id"]
        constraints = [models.UniqueConstraint(fields=["olt", "tipo", "profile_id"], name="perfil_ont_unico")]

    def __str__(self):
        return f"{self.profile_id} · {self.nombre}" if self.nombre else str(self.profile_id)

    @property
    def gem(self):
        """El GEM port que se usa en el service-port: el primero del line-profile (o 1)."""
        return self.gems[0] if self.gems else 1

    def acepta_vlan(self, vlan):
        """¿Este line-profile deja pasar esa VLAN? None = no se sabe (no se leyó el detalle)."""
        if not self.leido or (not self.vlans and not self.gems):
            return None
        if self.modo_mapeo and "vlan" not in self.modo_mapeo.lower():
            return True  # mapea por prioridad/puerto: cualquier VLAN pasa
        return not self.vlans or vlan in self.vlans


class PlanOLT(models.Model):
    """
    Junta lo que escribiste a mano en 'ont add' y 'service-port'.
    Paso 20: cada plan es de UNA OLT (sus line/srv profiles solo existen en esa OLT).
    """
    olt = models.ForeignKey(OLT, on_delete=models.CASCADE, related_name="planes", null=True, blank=True,
                            help_text="Vacío = plan viejo (de antes del paso 20): edítalo y elige su OLT")
    nombre = models.CharField(max_length=50)
    lineprofile_id = models.PositiveIntegerField()
    srvprofile_id = models.PositiveIntegerField()
    vlan = models.PositiveIntegerField()
    gemport = models.PositiveIntegerField(default=1)

    def __str__(self):
        return f"{self.nombre} (line {self.lineprofile_id} / srv {self.srvprofile_id} / VLAN {self.vlan})"


class PerfilVelocidad(models.Model):
    """
    Paso 18: "Speed profile" como SmartOLT. En la Huawei es una 'traffic table ip' que se pega
    al service-port: OUTBOUND = bajada (download) e INBOUND = subida (upload) del cliente.
    """
    DIRECCIONES = [("bajada", "Bajada (download)"), ("subida", "Subida (upload)")]
    TIPOS = [("internet", "Internet"), ("iptv", "IPTV"), ("voip", "VoIP"), ("gestion", "Gestión")]

    nombre = models.CharField(max_length=40, help_text="Ej. 100M")
    direccion = models.CharField("Dirección", max_length=6, choices=DIRECCIONES, default="bajada")
    usar_prefijo = models.BooleanField("Usar prefijo y sufijo", default=True,
                                       help_text="En la OLT se llamará TVN_<nombre>_DOWN / _UP")
    tipo = models.CharField(max_length=10, choices=TIPOS, default="internet")
    kbps = models.PositiveIntegerField("Velocidad (kbps)", help_text="Ej. 102400 = 100 Mbps")
    # Paso 19: 100 ms por defecto, igual que SmartOLT
    cbs_ms = models.PositiveSmallIntegerField(
        "CBS - committed burst (ms)", null=True, blank=True, default=100,
        help_text="Cuántos ms de tráfico absorbe la OLT antes de descartar. SmartOLT usa 100. Vacío = lo que ponga la OLT.")
    pbs_ms = models.PositiveSmallIntegerField(
        "PBS - peak burst (ms)", null=True, blank=True, default=100,
        help_text="Cuántos ms tolera por encima del CBS. SmartOLT usa 100. Vacío = lo que ponga la OLT.")
    predeterminado = models.BooleanField("Predeterminado para ONUs nuevas", default=False)
    indice_olt = models.PositiveSmallIntegerField(
        "Índice de traffic table", unique=True, null=True, blank=True,
        help_text="Número de la traffic table en la OLT. Vacío = el sistema elige uno libre desde 100.")
    creado_en_olt = models.BooleanField(default=False, editable=False,
                                        help_text="Ya se creó en alguna OLT: la velocidad ya no se puede editar")
    creado = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "perfil de velocidad"
        verbose_name_plural = "perfiles de velocidad"
        ordering = ["direccion", "kbps"]
        constraints = [models.UniqueConstraint(fields=["nombre", "direccion"], name="perfil_vel_unico")]

    def __str__(self):
        return f"{self.nombre} · {self.velocidad_texto}"

    @property
    def sufijo(self):
        return "DOWN" if self.direccion == "bajada" else "UP"

    @property
    def nombre_olt(self):
        import re
        base = f"TVN_{self.nombre}_{self.sufijo}" if self.usar_prefijo else self.nombre
        return re.sub(r"[^A-Za-z0-9_-]", "_", base)[:32]

    @property
    def velocidad_texto(self):
        k = self.kbps or 0
        if k >= 1024 * 1024:
            return f"{k / 1048576:.1f} Gbps".replace(".0 ", " ")
        if k >= 1024:
            return f"{k / 1024:.1f} Mbps".replace(".0 ", " ")
        return f"{k} kbps"

    def burst_bytes(self, ms):
        """ms de tráfico a esta velocidad -> bytes, dentro de lo que acepta Huawei (2 000 a 10 240 000)."""
        if not ms:
            return None
        return max(2000, min(10240000, int(self.kbps * ms / 8)))

    def comando_huawei(self):
        cbs, pbs = self.burst_bytes(self.cbs_ms), self.burst_bytes(self.pbs_ms)
        cir = f"cir {self.kbps}" + (f" cbs {cbs}" if cbs else "")
        pir = f"pir {self.kbps}" + (f" pbs {pbs}" if pbs else "")
        return (f'traffic table ip index {self.indice_olt} name "{self.nombre_olt}" {cir} {pir} '
                f"priority 0 priority-policy local-setting")

    def save(self, *args, **kwargs):
        if self.indice_olt is None:
            ultimo = PerfilVelocidad.objects.exclude(pk=self.pk).aggregate(m=models.Max("indice_olt"))["m"]
            self.indice_olt = max(ultimo or 99, 99) + 1
        super().save(*args, **kwargs)
        if self.predeterminado:  # solo uno predeterminado por dirección
            PerfilVelocidad.objects.filter(direccion=self.direccion, predeterminado=True) \
                .exclude(pk=self.pk).update(predeterminado=False)


class ONU(models.Model):
    ESTADOS = [
        ("sin_autorizar", "Sin autorizar"),
        ("autorizando", "Autorizando..."),
        ("activa", "Activa"),
        ("suspendida", "Suspendida"),
        ("error", "Error"),
        ("no_detectada", "Ya no aparece en autofind"),  # NUEVO (paso 13)
    ]
    ACCIONES = [  # NUEVO (paso 9): orden en la fila que todavía no termina
        ("", "—"),
        ("senal", "Leyendo señal"),
        ("reiniciar", "Reiniciando"),
        ("desactivar", "Suspendiendo"),
        ("activar", "Reactivando"),
        ("baja", "Eliminando de la OLT"),
        ("velocidad", "Cambiando velocidad"),  # paso 18
    ]
    olt = models.ForeignKey(OLT, on_delete=models.PROTECT, related_name="onus")
    cliente = models.ForeignKey("ServerMikro.Cliente", on_delete=models.SET_NULL,
                                null=True, blank=True, related_name="onus")
    plan = models.ForeignKey(PlanOLT, on_delete=models.PROTECT, null=True, blank=True)
    # NUEVO (paso 20): "User VLAN-ID" como en SmartOLT. Vacío = la VLAN del plan.
    vlan = models.PositiveSmallIntegerField("User VLAN-ID", null=True, blank=True)
    # NUEVO (paso 21): perfiles elegidos al autorizar (como SmartOLT). Vacío = los del plan (preset).
    lineprofile_id = models.PositiveIntegerField("Line-profile", null=True, blank=True)
    srvprofile_id = models.PositiveIntegerField("Service-profile", null=True, blank=True)
    gemport = models.PositiveSmallIntegerField("GEM port", null=True, blank=True)
    sn = models.CharField("SN", max_length=16, unique=True)
    modelo = models.CharField(max_length=50, blank=True)
    frame = models.PositiveSmallIntegerField(default=0)
    slot = models.PositiveSmallIntegerField()
    puerto = models.PositiveSmallIntegerField()
    ont_id = models.PositiveSmallIntegerField(null=True, blank=True)
    service_port = models.PositiveIntegerField(null=True, blank=True)
    descripcion = models.CharField(max_length=60, blank=True)
    # NUEVO (paso 17): ONU mode (como SmartOLT)
    MODOS = [("routing", "Routing (la ONU marca PPPoE)"), ("bridging", "Bridging (el router del cliente marca PPPoE)")]
    modo = models.CharField("ONU mode", max_length=8, choices=MODOS, default="bridging")
    aviso = models.TextField(blank=True, help_text="Algo que no salió del todo bien al autorizar (no es un error)")
    # NUEVO (paso 18): velocidad en la OLT (traffic tables del service-port). Vacío = ilimitado.
    vel_bajada = models.ForeignKey(PerfilVelocidad, on_delete=models.PROTECT, null=True, blank=True,
                                   related_name="onus_bajada", limit_choices_to={"direccion": "bajada"},
                                   verbose_name="Download speed")
    vel_subida = models.ForeignKey(PerfilVelocidad, on_delete=models.PROTECT, null=True, blank=True,
                                   related_name="onus_subida", limit_choices_to={"direccion": "subida"},
                                   verbose_name="Upload speed")
    estado = models.CharField(max_length=15, choices=ESTADOS, default="sin_autorizar")
    rx_power = models.FloatField("Señal RX (dBm)", null=True, blank=True)
    # NUEVO (paso 9)
    tx_power = models.FloatField("Señal TX de la ONU (dBm)", null=True, blank=True)
    olt_rx_power = models.FloatField("RX en la OLT (dBm)", null=True, blank=True)
    temperatura = models.FloatField("Temperatura (°C)", null=True, blank=True)
    en_linea = models.BooleanField("En línea", null=True, blank=True)
    senal_leida = models.DateTimeField("Señal leída", null=True, blank=True)
    accion_pendiente = models.CharField(max_length=12, choices=ACCIONES, blank=True, default="")

    ultimo_error = models.TextField(blank=True)
    actualizado = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["olt", "frame", "slot", "puerto", "ont_id"],
                                    name="onu_posicion_unica"),
        ]

    # NUEVO (paso 14): si una ONU lleva más de estos minutos en "Autorizando", se considera
    # trabada (worker apagado, fila vaciada...) y se puede volver a autorizar.
    MINUTOS_TRABADA = 5

    @property
    def autorizando_trabada(self):
        from datetime import timedelta
        from django.utils import timezone
        return (self.estado == "autorizando" and self.actualizado is not None
                and self.actualizado < timezone.now() - timedelta(minutes=self.MINUTOS_TRABADA))

    @property
    def vlan_efectiva(self):
        """La VLAN con la que se autoriza: la elegida en la ONU o, si no, la de su plan."""
        return self.vlan or (self.plan.vlan if self.plan else None)

    @property
    def plan_efectivo(self):
        """
        Lo que se le pasa a huawei.py: line/srv profile, GEM y VLAN de la ONU (paso 21) o,
        si faltan, los del plan (preset). None si falta algo.
        """
        from types import SimpleNamespace
        p = self.plan
        line = self.lineprofile_id if self.lineprofile_id is not None else (p.lineprofile_id if p else None)
        srv = self.srvprofile_id if self.srvprofile_id is not None else (p.srvprofile_id if p else None)
        gem = self.gemport or (p.gemport if p else None) or 1
        vlan = self.vlan_efectiva
        if line is None or srv is None or not vlan:
            return None
        return SimpleNamespace(nombre=p.nombre if p else f"line {line} / srv {srv}", lineprofile_id=line,
                               srvprofile_id=srv, gemport=gem, vlan=vlan)

    @property
    def fsp(self):
        return f"{self.frame}/{self.slot}/{self.puerto}"

    def __str__(self):
        return f"{self.sn} ({self.fsp}:{self.ont_id})"
