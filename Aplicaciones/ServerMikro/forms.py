import re

from django import forms
from django.db.models import Q

import ipaddress

from .models import (CajaNAP, Cliente, Plan, RedIPv4, Router, _parsear_reservadas, asignar_ip,
                     ips_de_clientes, red_de)

RE_USUARIO = re.compile(r"^[a-z0-9._-]{3,50}$")
RE_VELOCIDAD = re.compile(r"^\d+(\.\d+)?[kKMG]/\d+(\.\d+)?[kKMG]$")
RE_LIMITE = re.compile(r"^\d+(\.\d+)?[kKMG]$")


class SelectRed(forms.Select):
    """<select> de redes: cada opción lleva data-router para que el JS filtre por router."""
    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        op = super().create_option(name, value, label, selected, index, subindex, attrs)
        instancia = getattr(value, "instance", None)
        if instancia is not None:
            op["attrs"]["data-router"] = instancia.router_id
        return op


class SelectNap(forms.Select):
    """<select> de cajas NAP: cada opción lleva sus puertos libres (data-libres="1,3,5")."""
    libres = {}

    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        op = super().create_option(name, value, label, selected, index, subindex, attrs)
        instancia = getattr(value, "instance", None)
        if instancia is not None:
            op["attrs"]["data-libres"] = ",".join(str(p) for p in self.libres.get(instancia.pk, []))
        return op


MAX_PUERTOS = 128


class CamposNAP:
    """Paso 16: Caja NAP + Puerto NAP (solo puertos libres). Lo usan el alta y la ficha del cliente."""

    def preparar_nap(self, cliente=None):
        cajas = list(CajaNAP.objects.all())
        libres = {c.pk: c.puertos_libres(excepto_cliente=cliente) for c in cajas}
        campo = self.fields["caja_nap"]
        campo.queryset = CajaNAP.objects.all()
        campo.empty_label = "Ninguna"
        campo.widget = SelectNap(attrs={"data-nap": "caja"})
        campo.widget.libres = libres
        campo.widget.choices = campo.choices
        campo.label_from_instance = (lambda c: f"{c.nombre} ({len(libres.get(c.pk, []))} libres de {c.puertos})"
                                     + (f" · {c.ubicacion}" if c.ubicacion else ""))
        maximo = max([c.puertos for c in cajas] or [16])
        self.fields["puerto_nap"] = forms.TypedChoiceField(
            label="Puerto NAP", required=False, coerce=int, empty_value=None,
            choices=[("", "Ninguno")] + [(i, f"Puerto {i}") for i in range(1, maximo + 1)],
            widget=forms.Select(attrs={"data-nap": "puerto"}),
            help_text="Solo salen los libres.")
        self._cliente_nap = cliente

    def validar_nap(self, datos):
        caja, puerto = datos.get("caja_nap"), datos.get("puerto_nap")
        if puerto and not caja:
            self.add_error("caja_nap", "Elige la caja NAP de ese puerto.")
        elif caja and not puerto:
            self.add_error("puerto_nap", "Elige el puerto de la caja.")
        elif caja and puerto:
            if puerto > caja.puertos:
                self.add_error("puerto_nap", f"{caja.nombre} solo tiene {caja.puertos} puertos.")
            elif puerto not in caja.puertos_libres(excepto_cliente=self._cliente_nap):
                ocupante = caja.clientes.filter(puerto_nap=puerto).first()
                self.add_error("puerto_nap", f"El puerto {puerto} de {caja.nombre} ya lo usa {ocupante}.")
        return datos


class ClienteForm(CamposNAP, forms.ModelForm):
    # NUEVO (paso 15): la IP puede salir sola de las Redes IPv4 del router
    red = forms.ModelChoiceField(
        queryset=RedIPv4.objects.none(), required=False, label="Red IPv4",
        empty_label="Automática (la primera red del router con IPs libres)", widget=SelectRed)
    crear_ahora = forms.BooleanField(
        required=False, initial=True, label="Crear ya en el MikroTik",
        help_text="Desmárcalo si el cliente aún no tiene instalación: quedará Pendiente "
                  "y se creará solo cuando autorices su ONU.")

    class Meta:
        model = Cliente
        fields = ["nombre", "cedula", "telefono", "direccion",
                  "usuario_pppoe", "clave_pppoe", "router", "red", "ip", "plan", "caja_nap", "puerto_nap"]
        labels = {
            "nombre": "Nombre completo",
            "usuario_pppoe": "Usuario PPPoE",
            "clave_pppoe": "Clave PPPoE",
            "ip": "IP fija",
            "plan": "Plan de internet",
            "router": "Router MikroTik",
        }
        error_messages = {"ip": {"unique": "Esa IP ya la tiene otro cliente."},
                          "usuario_pppoe": {"unique": "Ese usuario PPPoE ya existe."}}
        widgets = {
            "clave_pppoe": forms.TextInput(attrs={"autocomplete": "off"}),
            "ip": forms.TextInput(attrs={"placeholder": "Automática", "autocomplete": "off"}),
            "usuario_pppoe": forms.TextInput(attrs={"placeholder": "jperez", "autocomplete": "off"}),
            "telefono": forms.TextInput(attrs={"placeholder": "09xxxxxxxx"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        routers = self.fields["router"].queryset
        self.fields["router"].empty_label = "Elige el router…"
        if routers.count() == 1 and not self.is_bound:
            self.initial["router"] = routers.first().pk  # si hay uno solo, ya viene elegido
        # Plan: obligatorio y solo los activos
        self.fields["plan"].queryset = Plan.objects.filter(activo=True)
        self.fields["plan"].required = True
        self.fields["plan"].empty_label = "Elige el plan…"
        if not Plan.objects.filter(activo=True).exists():
            self.fields["plan"].help_text = "No hay planes activos: créalos primero en Planes."
        # Paso 15: IP opcional; si va vacía se asigna sola
        self.fields["ip"].required = False
        self.fields["ip"].label = "IP a mano"
        self.fields["ip"].help_text = "Solo si necesitas una IP concreta. Vacía = la pone el sistema."
        # Paso 16: cada red dice cuántas IPs le quedan, como MikroWisp
        redes = list(RedIPv4.objects.filter(activa=True).select_related("router"))
        usadas = ips_de_clientes()
        disponibles = {}
        for r in redes:
            bloqueadas = r.bloqueadas()
            ocupadas = sum(1 for ip in usadas if ip in r.red_ip and ip not in bloqueadas)
            disponibles[r.pk] = max(r.total - len(bloqueadas) - ocupadas, 0)
        self.fields["red"].queryset = RedIPv4.objects.filter(activa=True).select_related("router")
        self.fields["red"].label_from_instance = (
            lambda r: f"{r.cidr_texto} ({disponibles.get(r.pk, 0)} IPs disponibles)")
        self.fields["red"].help_text = "El sistema le da la siguiente IP libre de esa red al guardar."
        self.red_asignada = None
        self.preparar_nap()

    def clean(self):
        datos = self.validar_nap(super().clean())
        router, red, ip = datos.get("router"), datos.get("red"), datos.get("ip")
        if not router:
            return datos
        if red and red.router_id != router.pk:
            self.add_error("red", f"Esa red es de {red.router.nombre}, no de {router.nombre}.")
            return datos
        if ip:
            # IP escrita a mano: si el router tiene redes, debe caer en una y no estar reservada
            red_ip = red if red and red.contiene(ip) else red_de(router.pk, ip)
            if red and not red.contiene(ip):
                self.add_error("ip", f"{ip} no está dentro de {red.cidr_texto}.")
            elif red_ip is None and router.redes.exists():
                self.add_error("ip", f"{ip} no pertenece a ninguna Red IPv4 de {router.nombre}.")
            elif red_ip and ipaddress.IPv4Address(ip) in red_ip.bloqueadas():
                self.add_error("ip", f"{ip} es el gateway o una IP reservada de {red_ip.cidr_texto}.")
            self.red_asignada = red_ip
        elif "ip" not in self.errors:
            try:
                datos["ip"], self.red_asignada = asignar_ip(router, red)
            except ValueError as e:
                self.add_error("ip" if not red else "red", str(e))
        return datos

    def clean_usuario_pppoe(self):
        u = self.cleaned_data["usuario_pppoe"].strip().lower()
        if not RE_USUARIO.match(u):
            raise forms.ValidationError("Solo minúsculas, números, punto, guion y guion bajo (3 a 50).")
        return u

    def save(self, commit=True):
        cliente = super().save(commit=False)
        cliente.velocidad = cliente.plan.velocidad  # la cola se crea con la velocidad del plan
        if commit:
            cliente.save()
        return cliente

    def clean_cedula(self):
        c = self.cleaned_data["cedula"].strip()
        if c and not c.isdigit():
            raise forms.ValidationError("Solo números.")
        return c


def _limite(valor):
    v = valor.strip().replace(" ", "")
    if v and v[-1] in "mg":
        v = v[:-1] + v[-1].upper()  # 10m -> 10M (en el MikroTik la m minúscula es "mili")
    if not RE_LIMITE.match(v):
        raise forms.ValidationError("Ejemplo: 10M, 512k o 1G.")
    return v


class PlanForm(forms.ModelForm):
    aplicar = forms.BooleanField(
        required=False, initial=True, label="Aplicar la nueva velocidad a los clientes de este plan",
        help_text="Si cambias subida o bajada, cada cliente activo o suspendido entra a la fila "
                  "de su router para actualizar su cola en el MikroTik.")

    class Meta:
        model = Plan
        fields = ["nombre", "subida", "bajada", "precio", "descripcion", "activo"]
        labels = {"nombre": "Nombre del plan", "subida": "Subida", "bajada": "Bajada",
                  "precio": "Precio mensual ($)", "descripcion": "Descripción",
                  "activo": "Disponible para clientes nuevos"}
        widgets = {
            "nombre": forms.TextInput(attrs={"placeholder": "Plan Hogar 20 Megas"}),
            "subida": forms.TextInput(attrs={"placeholder": "10M"}),
            "bajada": forms.TextInput(attrs={"placeholder": "20M"}),
            "precio": forms.NumberInput(attrs={"step": "0.01", "min": "0"}),
            "descripcion": forms.TextInput(attrs={"placeholder": "Opcional"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.instance.pk:
            del self.fields["aplicar"]  # en un plan nuevo no hay clientes que actualizar

    def clean_subida(self):
        return _limite(self.cleaned_data["subida"])

    def clean_bajada(self):
        return _limite(self.cleaned_data["bajada"])

    def clean_precio(self):
        p = self.cleaned_data["precio"]
        if p is not None and p < 0:
            raise forms.ValidationError("El precio no puede ser negativo.")
        return p


class CambiarPlanForm(forms.Form):
    plan = forms.ModelChoiceField(queryset=Plan.objects.none(), empty_label=None, label="Nuevo plan")

    def __init__(self, *args, cliente=None, **kwargs):
        super().__init__(*args, **kwargs)
        qs = Plan.objects.filter(activo=True)
        if cliente is not None and cliente.plan_id:
            qs = Plan.objects.filter(Q(activo=True) | Q(pk=cliente.plan_id))
            self.initial["plan"] = cliente.plan_id
        self.fields["plan"].queryset = qs

    def clean_plan(self):
        plan = self.cleaned_data["plan"]
        if not plan.activo:
            raise forms.ValidationError("Ese plan ya no está disponible.")
        return plan



class RouterForm(forms.ModelForm):
    """Alta / edición de un MikroTik. La clave no se muestra; vacía al editar = no cambia."""
    clave = forms.CharField(label="Clave", required=False,
                            widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password"}))

    class Meta:
        model = Router
        fields = ["nombre", "ip", "metodo", "puerto", "usuario", "clave", "huella_ssl"]
        labels = {"ip": "IP del MikroTik", "usuario": "Usuario", "puerto": "Puerto"}
        help_texts = {"metodo": "API-SSL es la recomendada: cifrada y no choca con MikroWisp (que usa la API 8728).",
                      "usuario": "Un usuario propio para el sistema (grupo con read, write, api)."}
        widgets = {"nombre": forms.TextInput(attrs={"placeholder": "MikroTik Central"}),
                   "ip": forms.TextInput(attrs={"placeholder": "192.168.3.250"}),
                   "usuario": forms.TextInput(attrs={"autocomplete": "off"}),
                   "puerto": forms.NumberInput(attrs={"placeholder": "automático"}),
                   "huella_ssl": forms.TextInput(attrs={"placeholder": "AB:CD:…  (opcional)", "class": "mono"})}

    def clean_huella_ssl(self):
        h = re.sub(r"[^0-9a-fA-F]", "", self.cleaned_data.get("huella_ssl") or "").lower()
        if h and len(h) != 64:
            raise forms.ValidationError("La huella SHA-256 tiene 64 caracteres hexadecimales (con o sin ':').")
        return h

    def clean_puerto(self):
        p = self.cleaned_data.get("puerto")
        if p is not None and not 1 <= p <= 65535:
            raise forms.ValidationError("Puerto entre 1 y 65535.")
        return p

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.instance.pk:
            self.initial.setdefault("metodo", "api_ssl")
        if self.instance.pk:
            self.fields["clave"].help_text = "Déjala vacía para mantener la clave actual."
        else:
            self.fields["clave"].required = True

    def save(self, commit=True):
        clave_nueva = self.cleaned_data.get("clave")
        router = super().save(commit=False)
        if not clave_nueva and self.instance.pk:
            router.clave = Router.objects.only("clave").get(pk=self.instance.pk).clave
        if commit:
            router.save()
        return router



class RedIPv4Form(forms.ModelForm):
    """Alta / edición de una red. Acepta '172.16.130.0/24' en el campo Red."""

    class Meta:
        model = RedIPv4
        fields = ["router", "red", "cidr", "gateway", "nombre", "reservadas", "activa"]
        widgets = {"red": forms.TextInput(attrs={"placeholder": "172.16.130.0", "class": "mono"}),
                   "cidr": forms.NumberInput(attrs={"min": 16, "max": 32}),
                   "gateway": forms.TextInput(attrs={"placeholder": "172.16.130.1 (opcional)", "class": "mono"}),
                   "nombre": forms.TextInput(attrs={"placeholder": "automático"}),
                   "reservadas": forms.Textarea(attrs={"rows": 2, "class": "mono",
                                                       "placeholder": "172.16.130.2, 172.16.130.250-172.16.130.254"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["router"].empty_label = "Elige el router…"
        if self.instance.pk and self.instance.router_id:
            self.fields["router"].help_text = "Si la cambias de router, sus clientes no se mueven."
        # El campo 'red' acepta "x.x.x.x/yy": lo separamos antes de validar
        if self.is_bound:
            datos = self.data.copy()
            texto = (datos.get(self.add_prefix("red")) or "").strip()
            if "/" in texto:
                ip, _, pref = texto.partition("/")
                datos[self.add_prefix("red")] = ip.strip()
                if pref.strip().isdigit():
                    datos[self.add_prefix("cidr")] = pref.strip()
            self.data = datos

    def clean_cidr(self):
        c = self.cleaned_data["cidr"]
        if not 16 <= c <= 32:
            raise forms.ValidationError("Entre /16 y /32.")
        return c

    def clean(self):
        datos = super().clean()
        red, cidr = datos.get("red"), datos.get("cidr")
        if not red or cidr is None:
            return datos
        net = ipaddress.IPv4Network(f"{red}/{cidr}", strict=False)
        datos["red"] = str(net.network_address)  # 172.16.130.5/24 -> 172.16.130.0
        self.instance.red = datos["red"]
        # No se puede pisar con otra red (de cualquier router)
        for otra in RedIPv4.objects.exclude(pk=self.instance.pk).select_related("router"):
            if net.overlaps(otra.red_ip):
                raise forms.ValidationError(
                    f"Se cruza con {otra.cidr_texto} de {otra.router.nombre}.")
        gw = datos.get("gateway")
        if gw and ipaddress.IPv4Address(gw) not in net:
            self.add_error("gateway", f"El gateway debe estar dentro de {net}.")
        try:
            fuera = [str(ip) for ip in _parsear_reservadas(datos.get("reservadas")) if ip not in net]
        except ValueError as e:
            self.add_error("reservadas", f"No se entiende: {e}")
        else:
            if fuera:
                self.add_error("reservadas", f"Fuera de la red: {', '.join(sorted(fuera)[:5])}")
        return datos



class NapClienteForm(CamposNAP, forms.ModelForm):
    """Paso 16: cambiar la caja/puerto NAP desde la ficha del cliente (solo base de datos)."""

    class Meta:
        model = Cliente
        fields = ["caja_nap", "puerto_nap"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.preparar_nap(cliente=self.instance)

    def clean(self):
        return self.validar_nap(super().clean())


RE_COORD = re.compile(r"^\s*(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*$")


class CajaNAPForm(forms.ModelForm):
    class Meta:
        model = CajaNAP
        fields = ["nombre", "coordenadas", "ubicacion", "puertos", "detalles"]
        widgets = {"nombre": forms.TextInput(attrs={"placeholder": "H11D1N1"}),
                   "coordenadas": forms.TextInput(attrs={"placeholder": "-0.126572,-76.340706", "class": "mono"}),
                   "ubicacion": forms.TextInput(attrs={"placeholder": "Tarapoa, poste 5"}),
                   "puertos": forms.NumberInput(attrs={"min": 1, "max": MAX_PUERTOS}),
                   "detalles": forms.Textarea(attrs={"rows": 3, "placeholder": "Opcional"})}

    def clean_nombre(self):
        return self.cleaned_data["nombre"].strip()

    def clean_coordenadas(self):
        texto = (self.cleaned_data.get("coordenadas") or "").strip()
        if not texto:
            return ""
        m = RE_COORD.match(texto)
        if not m or not (-90 <= float(m.group(1)) <= 90 and -180 <= float(m.group(2)) <= 180):
            raise forms.ValidationError("Formato: latitud,longitud. Ej: -0.126572,-76.340706")
        return f"{m.group(1)},{m.group(2)}"

    def clean_puertos(self):
        n = self.cleaned_data["puertos"]
        if not 1 <= n <= MAX_PUERTOS:
            raise forms.ValidationError(f"Entre 1 y {MAX_PUERTOS}.")
        if self.instance.pk:
            alto = (self.instance.clientes.exclude(puerto_nap=None)
                    .order_by("-puerto_nap").values_list("puerto_nap", flat=True).first())
            if alto and n < alto:
                raise forms.ValidationError(f"El puerto {alto} está ocupado: no puedes dejarla con {n}.")
        return n
