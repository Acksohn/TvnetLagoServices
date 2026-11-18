import re

from django import forms
from django.db.models import Q
from django.urls import reverse

from Aplicaciones.ServerMikro.forms import CamposNAP
from Aplicaciones.ServerMikro.models import CajaNAP, Cliente

from .models import OLT, ONU, VLAN, PerfilONT, PerfilVelocidad, PlanOLT

RE_DESC = re.compile(r"^[A-Za-z0-9_\-]*$")


def clientes_con_onu():
    """Clientes que ya tienen una ONU autorizada (o autorizándose). Esos no se ofrecen al autorizar."""
    return (ONU.objects.filter(Q(ont_id__isnull=False) | Q(estado="autorizando"), cliente__isnull=False)
            .values("cliente_id"))


def texto_cliente(c):
    return f"{c.nombre} · {c.usuario_pppoe} ({c.get_estado_display()})"


SIN_LIMITE = "Ilimitado (traffic table 6 de la OLT)"


def preparar_velocidades(form):
    """Paso 18: selects de bajada/subida. En una ONU nueva se propone el perfil predeterminado."""
    for campo, direccion in (("vel_bajada", "bajada"), ("vel_subida", "subida")):
        f = form.fields[campo]
        f.queryset = PerfilVelocidad.objects.filter(direccion=direccion).order_by("kbps")
        f.empty_label = SIN_LIMITE
        f.required = False
        if not form.is_bound and getattr(form.instance, f"{campo}_id") is None and not form.instance.ont_id:
            pred = f.queryset.filter(predeterminado=True).first()
            if pred:
                form.initial[campo] = pred.pk
    form.fields["vel_bajada"].help_text = "Lo que el cliente descarga (outbound en el service-port)."
    form.fields["vel_subida"].help_text = "Lo que el cliente sube (inbound en el service-port)."


class AutorizarONUForm(CamposNAP, forms.ModelForm):
    # Paso 17: caja NAP y puerto del cliente (como Splitter / Splitter port de SmartOLT)
    caja_nap = forms.ModelChoiceField(queryset=CajaNAP.objects.none(), required=False, label="Caja NAP")

    class Meta:
        model = ONU
        fields = ["plan", "lineprofile_id", "srvprofile_id", "modo", "vlan", "vel_bajada", "vel_subida",
                  "cliente", "descripcion"]
        labels = {
            "plan": "Usar preset (opcional)",
            "vel_bajada": "Download speed (bajada)",
            "vel_subida": "Upload speed (subida)",
            "modo": "ONU mode",
            "cliente": "Cliente",
            "descripcion": "Descripción en la OLT",
        }
        widgets = {"modo": forms.RadioSelect(attrs={"class": "radios"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        olt = self.instance.olt
        # Paso 21: como SmartOLT. El Plan OLT es solo un PRESET opcional que llena los campos.
        planes = PlanOLT.objects.filter(Q(olt=olt) | Q(olt__isnull=True)).order_by("olt", "nombre")
        self.fields["plan"].required = False
        self.fields["plan"].queryset = planes
        self.fields["plan"].label_from_instance = lambda p: (
            f"{p.nombre} (line {p.lineprofile_id} / srv {p.srvprofile_id} / VLAN {p.vlan})"
            + ("  ⚠ sin OLT asignada" if p.olt_id is None else ""))
        self.fields["plan"].empty_label = "Ninguno (elijo yo abajo)"
        self.fields["plan"].help_text = "Llena line-profile, service-profile y VLAN de una vez. Puedes cambiarlos."
        self.presets = {p.pk: {"line": p.lineprofile_id, "srv": p.srvprofile_id, "vlan": p.vlan} for p in planes}

        # Line-profile y Service-profile: los que EXISTEN en esta OLT, por nombre
        perfiles = list(olt.perfiles_ont.filter(existe=True))
        self.info_line = {p.profile_id: {"gem": p.gem, "vlans": p.vlans, "modo": p.modo_mapeo,
                                         "leido": bool(p.leido)} for p in perfiles if p.tipo == "line"}
        self.sin_perfiles = not perfiles
        for campo, tipo, etiqueta in (("lineprofile_id", "line", "Line-profile"),
                                      ("srvprofile_id", "srv", "Service-profile")):
            opciones = [(p.profile_id, f"{p.nombre or 'sin nombre'} (ID {p.profile_id})")
                        for p in perfiles if p.tipo == tipo]
            ids = {v for v, _t in opciones}
            # Los números que usan los presets de esta OLT, aunque todavía no se hayan leído
            for pl in planes:
                n = pl.lineprofile_id if tipo == "line" else pl.srvprofile_id
                if n not in ids:
                    opciones.append((n, f"ID {n} (del preset {pl.nombre})"))
                    ids.add(n)
            self.fields[campo] = forms.TypedChoiceField(
                label=etiqueta, required=False, coerce=int, empty_value=None,
                choices=[("", "Elige…")] + opciones)
        self.fields["lineprofile_id"].help_text = "Cómo viaja la ONU por la fibra (GEM y VLANs que deja pasar)."
        self.fields["srvprofile_id"].help_text = "Los puertos de la ONU. Se propone el de su modelo."
        if not self.is_bound and self.instance.srvprofile_id is None:
            self.initial["srvprofile_id"] = self._srv_sugerido(perfiles)

        # User VLAN-ID: solo las VLANs de esta OLT
        vlans = list(olt.vlans.order_by("vlan_id"))
        ids_vlan = {v.vlan_id for v in vlans}
        extra = sorted({pl.vlan for pl in planes} - ids_vlan)
        self.fields["vlan"] = forms.TypedChoiceField(
            label="User VLAN-ID", required=False, coerce=int, empty_value=None,
            choices=[("", "Elige la VLAN…")] + [(v.vlan_id, str(v)) for v in vlans]
                    + [(v, f"{v} (del preset)") for v in extra],
            help_text=(f"Las VLANs de {olt.nombre} (pestaña VLANs de la OLT)." if vlans else
                       f"{olt.nombre} todavía no tiene VLANs: cárgalas en OLT → VLANs → «Leer VLANs de la OLT»."))
        self.fields["modo"].help_text = ("Routing: la ONU hace de router y marca el PPPoE del cliente. "
                                         "Bridging: la ONU solo pasa la VLAN y marca el router del cliente.")

        # Solo clientes que todavía no tienen ONU (o el que ya tenía asignado esta)
        campo = self.fields["cliente"]
        # Paso 20: "sin ONU" = sin ONU AUTORIZADA. Antes se pedía que no tuviera NINGUNA fila de ONU,
        # y un cliente cuya ONU se eliminó (o falló al autorizar) quedaba fuera de la lista para siempre.
        campo.queryset = (Cliente.objects.exclude(pk__in=clientes_con_onu()) | Cliente.objects.filter(
            pk=self.instance.cliente_id)).distinct().order_by("nombre")
        campo.label_from_instance = texto_cliente
        # Paso 17: con miles de clientes NO se cargan todos en la página. El <select> solo trae
        # el elegido y el buscador (Select2) pide los demás al servidor mientras escribes.
        elegido = self.data.get(self.add_prefix("cliente")) if self.is_bound else self.instance.cliente_id
        # Paso 19: como en SmartOLT, toda ONU se autoriza para un cliente (obligatorio)
        campo.required = True
        campo.error_messages["required"] = "Elige el cliente de esta ONU."
        opciones = [("", "")]
        if str(elegido or "").isdigit():
            opciones += [(c.pk, texto_cliente(c)) for c in Cliente.objects.filter(pk=elegido)]
        # Paso 20: además se traen los primeros clientes sin ONU en el HTML. Así la lista sale
        # aunque el JavaScript no cargue; con Select2 activo, el buscador consulta al servidor.
        ya = {str(v) for v, _t in opciones}
        opciones += [(c.pk, texto_cliente(c)) for c in
                     campo.queryset.exclude(pk=self.instance.cliente_id).order_by("nombre")[:100] if str(c.pk) not in ya]
        campo.widget = forms.Select(attrs={
            "data-buscar-url": reverse("clientes:buscar") + "?sin_onu=1",
            "data-placeholder": "Buscar por nombre, usuario PPPoE, cédula o IP…"})
        campo.widget.choices = opciones

        self.fields["descripcion"].widget.attrs.update(
            {"placeholder": "Si lo dejas vacío, se usa el usuario PPPoE del cliente",
             "maxlength": 32})
        preparar_velocidades(self)
        self.preparar_nap()
        self.fields["caja_nap"].help_text = "Opcional. Se guarda en el cliente (vacío = no cambia)."
        self.order_fields(["plan", "lineprofile_id", "srvprofile_id", "modo", "vlan", "vel_bajada", "vel_subida",
                           "cliente", "caja_nap", "puerto_nap", "descripcion"])

    def _srv_sugerido(self, perfiles):
        """Como el 'Default (modelo)' de SmartOLT: el service-profile cuyo nombre se parece al modelo de la ONU."""
        srvs = [p for p in perfiles if p.tipo == "srv"]
        limpio = lambda t: re.sub(r"[^A-Z0-9]", "", (t or "").upper())
        modelo = limpio(self.instance.modelo)
        for p in srvs:
            n = limpio(p.nombre)
            if modelo and n and (n in modelo or modelo in n or modelo[:8] in n):
                return p.profile_id
        for p in srvs:
            if re.search(r"generic|default|flex|adaptive", p.nombre or "", re.I):
                return p.profile_id
        return srvs[0].profile_id if len(srvs) == 1 else None

    def clean(self):
        datos = super().clean()
        cliente = datos.get("cliente")
        self._cliente_nap = cliente
        plan = datos.get("plan")
        if plan and plan.olt_id not in (None, self.instance.olt_id):
            self.add_error("plan", f"Ese preset es de {plan.olt}, no de {self.instance.olt}.")
        # Paso 21: lo que no se eligió a mano sale del preset
        for campo, del_plan in (("lineprofile_id", "lineprofile_id"), ("srvprofile_id", "srvprofile_id"),
                                ("vlan", "vlan")):
            if datos.get(campo) is None and plan:
                datos[campo] = getattr(plan, del_plan)
            if datos.get(campo) is None:
                self.add_error(campo, "Elige uno (o usa un preset arriba).")
        line, vlan = datos.get("lineprofile_id"), datos.get("vlan")
        perfil = self.instance.olt.perfiles_ont.filter(tipo="line", profile_id=line).first() if line is not None else None
        if perfil and vlan and perfil.acepta_vlan(vlan) is False:
            self.add_error("vlan", f"El line-profile «{perfil.nombre}» no deja pasar la VLAN {vlan} "
                                   f"(solo {', '.join(map(str, perfil.vlans))}). La ONU no navegaría: "
                                   f"elige otra VLAN u otro line-profile.")
        # GEM port: se deduce del line-profile (como SmartOLT, no se pregunta)
        self.instance.gemport = perfil.gem if perfil else (plan.gemport if plan else 1)
        if not cliente:
            return datos  # el error "Elige el cliente" ya lo pone el campo
        return self.validar_nap(datos)

    def guardar_nap(self, cliente):
        """Guarda caja/puerto en el cliente (solo si se eligió una caja)."""
        caja, puerto = self.cleaned_data.get("caja_nap"), self.cleaned_data.get("puerto_nap")
        if cliente and caja and puerto:
            Cliente.objects.filter(pk=cliente.pk).update(caja_nap=caja, puerto_nap=puerto)

    def clean_descripcion(self):
        desc = self.cleaned_data["descripcion"].strip()
        if not RE_DESC.match(desc):
            raise forms.ValidationError(
                "Usa solo letras, números, guion (-) y guion bajo (_), sin espacios.")
        if len(desc) > 32:
            raise forms.ValidationError("Máximo 32 caracteres (límite de la OLT).")
        return desc


RE_HOST = re.compile(r"^[A-Za-z0-9.\-:]+$")


class OLTForm(forms.ModelForm):
    """Alta / edición de una OLT. La clave no se muestra; si la dejas vacía al editar, no cambia."""
    clave = forms.CharField(label="Clave", required=False,
                            widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password"}))

    class Meta:
        model = OLT
        fields = ["nombre", "host", "protocolo", "puerto_ssh", "usuario", "clave",
                  "solo_lectura", "snmp_comunidad", "snmp_puerto", "activa"]
        labels = {"host": "IP o dominio", "puerto_ssh": "Puerto", "usuario": "Usuario de la OLT",
                  "solo_lectura": "Solo lectura (la maneja otro sistema, ej. SmartOLT)",
                  "snmp_comunidad": "Comunidad SNMP", "snmp_puerto": "Puerto SNMP",
                  "activa": "Activa"}
        help_texts = {"solo_lectura": "Solo se leerá información: no se autoriza, reinicia ni da de baja desde aquí.",
                      "puerto_ssh": "SSH normalmente 22, Telnet 23 (o el que tengas en el dst-nat)."}
        widgets = {"nombre": forms.TextInput(attrs={"placeholder": "OLT Dayuma"}),
                   "host": forms.TextInput(attrs={"placeholder": "192.168.3.250"}),
                   "usuario": forms.TextInput(attrs={"autocomplete": "off", "placeholder": "django"}),
                   "snmp_comunidad": forms.TextInput(attrs={"autocomplete": "off"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.fields["clave"].help_text = "Déjala vacía para mantener la clave actual."
        else:
            self.fields["clave"].required = True

    def clean_host(self):
        h = self.cleaned_data["host"].strip()
        if not RE_HOST.match(h):
            raise forms.ValidationError("Escribe solo la IP o el dominio, sin http:// ni espacios.")
        return h

    def save(self, commit=True):
        clave_nueva = self.cleaned_data.get("clave")
        olt = super().save(commit=False)
        if not clave_nueva and self.instance.pk:
            olt.clave = OLT.objects.only("clave").get(pk=self.instance.pk).clave
        if commit:
            olt.save()
        return olt



class PlanOLTForm(forms.ModelForm):
    """Plan OLT = los perfiles y la VLAN que se usan en 'ont add' y 'service-port' al autorizar."""

    class Meta:
        model = PlanOLT
        fields = ["olt", "nombre", "lineprofile_id", "srvprofile_id", "vlan", "gemport"]
        labels = {"olt": "OLT", "nombre": "Nombre del preset", "lineprofile_id": "Line-profile (ID)",
                  "srvprofile_id": "Service-profile (ID)", "vlan": "User VLAN-ID", "gemport": "GEM port"}
        help_texts = {"lineprofile_id": "El número de 'ont-lineprofile gpon profile-id' en la OLT.",
                      "srvprofile_id": "El número de 'ont-srvprofile gpon profile-id' en la OLT.",
                      "vlan": "La VLAN que llega al MikroTik (la del PPPoE).",
                      "gemport": "Normalmente 1 (el gem que definiste en el lineprofile)."}
        widgets = {"nombre": forms.TextInput(attrs={"placeholder": "Internet VLAN 100"})}

    def clean_vlan(self):
        v = self.cleaned_data["vlan"]
        if not 1 <= v <= 4094:
            raise forms.ValidationError("La VLAN va de 1 a 4094.")
        return v

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["olt"].required = True  # paso 20: cada plan es de una OLT
        self.fields["olt"].queryset = OLT.objects.order_by("nombre")
        self.fields["olt"].empty_label = "Elige la OLT…"
        self.fields["olt"].help_text = "Los line/srv profiles y las VLANs son de cada OLT."
        self.fields["olt"].widget.attrs["data-recargar"] = "olt"
        # Paso 21: con la OLT elegida, los perfiles y las VLANs salen en listas por nombre (como SmartOLT)
        olt_id = (self.data.get(self.add_prefix("olt")) if self.is_bound else
                  self.initial.get("olt"))
        self.olt_elegida = OLT.objects.filter(pk=olt_id).first() if str(olt_id or "").isdigit() else None
        self.fields["gemport"].required = False
        self.fields["gemport"].help_text = "Se toma solo del line-profile. Cámbialo solo si sabes lo que haces."
        o = self.olt_elegida
        if o:
            for campo, tipo, etiqueta in (("lineprofile_id", "line", "Line-profile"),
                                          ("srvprofile_id", "srv", "Service-profile")):
                ps = list(o.perfiles_ont.filter(tipo=tipo, existe=True))
                if ps:
                    actual = getattr(self.instance, campo)
                    opciones = [(p.profile_id, f"{p.nombre or 'sin nombre'} (ID {p.profile_id})") for p in ps]
                    if actual is not None and actual not in {p.profile_id for p in ps}:
                        opciones.append((actual, f"ID {actual} (ya no está en la OLT)"))
                    self.fields[campo] = forms.TypedChoiceField(label=etiqueta, coerce=int,
                                                                choices=[("", "Elige…")] + opciones)
            vs = list(o.vlans.order_by("vlan_id"))
            if vs:
                opciones = [(v.vlan_id, str(v)) for v in vs]
                if self.instance.vlan and self.instance.vlan not in {v.vlan_id for v in vs}:
                    opciones.append((self.instance.vlan, f"{self.instance.vlan} (no está en la OLT)"))
                self.fields["vlan"] = forms.TypedChoiceField(label="User VLAN-ID", coerce=int,
                                                             choices=[("", "Elige…")] + opciones)

    def clean_nombre(self):
        return self.cleaned_data["nombre"].strip()

    def clean(self):
        datos = super().clean()
        olt, n, v = datos.get("olt"), datos.get("nombre"), datos.get("vlan")
        if olt and n and PlanOLT.objects.filter(olt=olt, nombre__iexact=n).exclude(pk=self.instance.pk).exists():
            self.add_error("nombre", f"{olt} ya tiene un plan con ese nombre.")
        if olt and v and olt.vlans.exists() and not olt.vlans.filter(vlan_id=v).exists():
            self.add_error("vlan", f"La VLAN {v} no está en {olt}. Agrégala primero en la pestaña VLANs de esa OLT.")
        line = datos.get("lineprofile_id")
        perfil = olt.perfiles_ont.filter(tipo="line", profile_id=line).first() if olt and line is not None else None
        if perfil and v and perfil.acepta_vlan(v) is False:
            self.add_error("vlan", f"El line-profile «{perfil.nombre}» no deja pasar la VLAN {v} "
                                   f"(solo {', '.join(map(str, perfil.vlans))}).")
        if not datos.get("gemport"):
            datos["gemport"] = perfil.gem if perfil else 1  # paso 21: el GEM sale del line-profile
        return datos


class VelocidadONUForm(forms.ModelForm):
    """Paso 18: cambiar la velocidad de una ONU ya autorizada (desde su ficha)."""

    class Meta:
        model = ONU
        fields = ["vel_bajada", "vel_subida"]
        labels = {"vel_bajada": "Download (bajada)", "vel_subida": "Upload (subida)"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        preparar_velocidades(self)


class PerfilVelocidadForm(forms.ModelForm):
    """Paso 18: Speed profile como en SmartOLT. Se crea en la OLT la primera vez que se usa."""
    bloqueado = False
    BLOQUEADOS = ["nombre", "direccion", "usar_prefijo", "kbps", "cbs_ms", "pbs_ms", "indice_olt"]

    class Meta:
        model = PerfilVelocidad
        fields = ["nombre", "direccion", "usar_prefijo", "tipo", "kbps", "predeterminado",
                  "cbs_ms", "pbs_ms", "indice_olt"]
        widgets = {"nombre": forms.TextInput(attrs={"placeholder": "100M"}),
                   "direccion": forms.RadioSelect(attrs={"class": "radios"}),
                   "kbps": forms.NumberInput(attrs={"placeholder": "102400", "min": 64, "max": 10240000}),
                   "cbs_ms": forms.NumberInput(attrs={"placeholder": "vacío"}),
                   "pbs_ms": forms.NumberInput(attrs={"placeholder": "vacío"}),
                   "indice_olt": forms.NumberInput(attrs={"placeholder": "automático"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk and self.instance.creado_en_olt:
            for nombre in self.BLOQUEADOS:
                self.fields[nombre].disabled = True
            self.bloqueado = True

    def clean_nombre(self):
        n = self.cleaned_data["nombre"].strip()
        if not re.match(r"^[A-Za-z0-9_\-.]+$", n):
            raise forms.ValidationError("Usa letras, números, punto, guion o guion bajo, sin espacios (ej. 100M).")
        return n

    def clean_kbps(self):
        k = self.cleaned_data["kbps"]
        if not 64 <= k <= 10240000:
            raise forms.ValidationError("Entre 64 kbps y 10 240 000 kbps (10 Gbps), que es lo que acepta la Huawei.")
        return k

    def clean_indice_olt(self):
        i = self.cleaned_data.get("indice_olt")
        if i is not None and not 10 <= i <= 6000:
            raise forms.ValidationError("Usa un índice entre 10 y 6000 (del 0 al 9 son los de fábrica de Huawei).")
        return i

    def clean(self):
        datos = super().clean()
        n, d = datos.get("nombre"), datos.get("direccion")
        if n and d and PerfilVelocidad.objects.filter(nombre__iexact=n, direccion=d).exclude(pk=self.instance.pk).exists():
            self.add_error("nombre", "Ya existe un perfil con ese nombre en esa dirección.")
        return datos


class VLANForm(forms.ModelForm):
    """Paso 20: Add VLAN (como SmartOLT), siempre dentro de una OLT."""
    crear_en_olt = forms.BooleanField(label="Crear también en la OLT (vlan V smart)", required=False, initial=True)

    class Meta:
        model = VLAN
        fields = ["vlan_id", "descripcion", "gestion", "iptv", "dhcp_snooping", "lan_to_lan"]
        widgets = {"vlan_id": forms.NumberInput(attrs={"min": 1, "max": 4094, "placeholder": "500"}),
                   "descripcion": forms.TextInput(attrs={"placeholder": "INTERNET_SANSAHUARI"})}

    def __init__(self, *args, olt, **kwargs):
        super().__init__(*args, **kwargs)
        self.olt = olt
        if olt.solo_lectura:
            del self.fields["crear_en_olt"]
        if self.instance.pk:
            self.fields["vlan_id"].disabled = True
            self.fields.pop("crear_en_olt", None)

    def clean_vlan_id(self):
        v = self.cleaned_data["vlan_id"]
        if not 1 <= v <= 4094:
            raise forms.ValidationError("La VLAN va de 1 a 4094.")
        if VLAN.objects.filter(olt=self.olt, vlan_id=v).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError(f"{self.olt} ya tiene la VLAN {v}.")
        return v

    def save(self, commit=True):
        self.instance.olt = self.olt
        return super().save(commit)


def leer_rango_vlans(texto):
    """'500-505, 600' -> [500, 501, ..., 505, 600]. Lanza ValueError si algo no es válido."""
    salida = []
    for parte in re.split(r"[,\s]+", texto.strip()):
        if not parte:
            continue
        m = re.fullmatch(r"(\d{1,4})(?:-(\d{1,4}))?", parte)
        if not m:
            raise ValueError(f"«{parte}» no es una VLAN ni un rango (ej. 500-510).")
        a, b = int(m.group(1)), int(m.group(2) or m.group(1))
        if not (1 <= a <= b <= 4094):
            raise ValueError(f"«{parte}»: las VLANs van de 1 a 4094 y el rango de menor a mayor.")
        salida.extend(range(a, b + 1))
    if not salida:
        raise ValueError("Escribe al menos una VLAN.")
    if len(salida) > 200:
        raise ValueError("Máximo 200 VLANs de una vez.")
    return sorted(set(salida))


class VLANMultipleForm(forms.Form):
    """Paso 20: Add multiple VLANs."""
    vlans = forms.CharField(label="VLANs", help_text="Ej. 500-510, 600, 700-702",
                            widget=forms.TextInput(attrs={"placeholder": "500-510, 600"}))
    descripcion = forms.CharField(label="Descripción (opcional)", required=False, max_length=50,
                                  help_text="Se le agrega el número: VLAN_ → VLAN_500, VLAN_501…")
    crear_en_olt = forms.BooleanField(label="Crear también en la OLT", required=False, initial=True)

    def __init__(self, *args, olt, **kwargs):
        super().__init__(*args, **kwargs)
        self.olt = olt
        if olt.solo_lectura:
            del self.fields["crear_en_olt"]

    def clean_vlans(self):
        try:
            return leer_rango_vlans(self.cleaned_data["vlans"])
        except ValueError as e:
            raise forms.ValidationError(str(e))
