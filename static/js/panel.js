// Panel TvnetLago: pequeños comportamientos sin librerías externas.

// 1) Botones con data-cargando: al enviar el formulario se desactivan
//    (evita doble clic = doble orden en la fila).
document.addEventListener("submit", function (ev) {
  var boton = ev.target.querySelector("button[data-cargando]");
  if (!boton) return;
  setTimeout(function () {
    boton.disabled = true;
    boton.classList.add("cargando");
    if (boton.dataset.cargando) boton.lastChild.textContent = " " + boton.dataset.cargando;
  }, 0);
});

// 2) Selects que filtran apenas los cambias (ej. puerto PON)
document.addEventListener("change", function (ev) {
  if (ev.target.matches("select[data-auto-enviar]")) ev.target.form.submit();
});

// 3) "En vivo": cada 5 s se vuelve a pedir esta misma página y se reemplazan
//    SOLO los bloques marcados con data-vivo (tabla, contadores, menú),
//    sin recargar y sin borrar lo que estás escribiendo en el buscador.
(function () {
  if (!document.querySelector("[data-vivo]")) return;

  function actualizar() {
    // No refrescar si la pestaña está oculta o hay un detalle de error abierto
    if (document.hidden || document.querySelector("[data-vivo] details[open]")) return;
    fetch(location.href, { credentials: "same-origin" })
      .then(function (r) {
        if (r.redirected || !r.ok) throw new Error("sesión o servidor");
        return r.text();
      })
      .then(function (html) {
        var nuevo = new DOMParser().parseFromString(html, "text/html");
        document.querySelectorAll("[data-vivo]").forEach(function (viejo) {
          var reemplazo = nuevo.getElementById(viejo.id);
          if (reemplazo) viejo.innerHTML = reemplazo.innerHTML;
        });
        marcar(true);
      })
      .catch(function () { marcar(false); });
  }

  function marcar(ok) {
    document.querySelectorAll(".en-vivo").forEach(function (p) { p.classList.toggle("caido", !ok); });
  }

  setInterval(actualizar, 5000);
})();


// 4) Paso 17: selects con buscador (Select2) en todos los formularios.
//    - <select data-buscar-url="..."> busca en el servidor mientras escribes (clientes: miles).
//    - Los demás buscan entre sus opciones (el cuadro de búsqueda sale si hay más de 8).
//    - <select data-sin-buscador> se queda como select normal.
//    Select2 avisa los cambios a su manera (jQuery); aquí se reenvían como 'change' normal
//    para que los scripts de cada página (filtrar redes, puertos NAP…) sigan funcionando.
window.refrescarSelect = function (el) {
  if (window.jQuery && el && jQuery(el).data("select2")) jQuery(el).trigger("change.select2");
};
function iniciarSelect2() {
  var $ = jQuery;

  // Oculta las opciones marcadas hidden (ej. redes de otro router)
  function buscador(params, data) {
    if (data.element && data.element.hidden) return null;
    if (!params.term || !params.term.trim()) return data;
    var t = params.term.toLowerCase().normalize("NFD").replace(/[\u0300-\u036f]/g, "");
    var x = (data.text || "").toLowerCase().normalize("NFD").replace(/[\u0300-\u036f]/g, "");
    return t.split(/\s+/).every(function (p) { return x.indexOf(p) >= 0; }) ? data : null;
  }

  function avisarCambio(el) {
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }

  document.querySelectorAll(".formulario select, .formulario-ancho select").forEach(function (el) {
    if (el.multiple || el.hasAttribute("data-sin-buscador") || el.hasAttribute("data-auto-enviar")) return;
    // Solo con data-placeholder se usa "placeholder" de Select2 (texto fijo + botón ×).
    // Sin él, la opción vacía se muestra tal cual, y si un script cambia su texto se ve al momento.
    var opciones = {
      language: "es",
      width: "100%",
      minimumResultsForSearch: el.dataset.buscarUrl ? 0 : 8,
      matcher: buscador,
    };
    if (el.dataset.placeholder) {
      opciones.placeholder = el.dataset.placeholder;
      opciones.allowClear = !el.required;
    }
    if (el.dataset.buscarUrl) {
      opciones.ajax = {
        url: el.dataset.buscarUrl, dataType: "json", delay: 250, cache: true,
        data: function (p) { return { q: p.term || "" }; },
      };
      opciones.minimumInputLength = 0;
      delete opciones.matcher;
    }
    var $el = $(el).select2(opciones);
    $el.on("select2:select", function (e) {
      var extra = (e.params.data && e.params.data.extra) || {};
      Object.keys(extra).forEach(function (k) { el.dataset[k] = extra[k]; });
      avisarCambio(el);
    });
    $el.on("select2:clear", function () {
      Object.keys(el.dataset).forEach(function (k) { if (k === "nap") delete el.dataset[k]; });
      setTimeout(function () { avisarCambio(el); }, 0);
    });
  });
}


// 5) Paso 19: si Select2 NO cargó (faltan los archivos de static/vendor, caché del navegador…),
//    los <select data-buscar-url> igual funcionan: se pone un cuadro de búsqueda encima
//    y las opciones se piden al servidor. Así nunca queda un select vacío.
function buscadorSimple() {
  if (document.querySelector("select[data-buscar-url]")) {
    console.warn("[TvnetLago] Select2 no cargó: revisa que exista static/vendor/select2/select2.min.js. Se usa el buscador simple.");
  }
  document.querySelectorAll("select[data-buscar-url]").forEach(function (sel) {
    var caja = document.createElement("input");
    caja.type = "search";
    caja.placeholder = sel.dataset.placeholder || "Buscar…";
    caja.autocomplete = "off";
    caja.style.marginBottom = "6px";
    sel.parentNode.insertBefore(caja, sel);
    var vacia = sel.options[0] && !sel.options[0].value ? sel.options[0] : null;
    if (vacia && !vacia.textContent.trim()) vacia.textContent = "Elige…";
    var turno = 0, espera;
    function cargar() {
      var mio = ++turno;
      var url = sel.dataset.buscarUrl + (sel.dataset.buscarUrl.indexOf("?") >= 0 ? "&" : "?") + "q=" + encodeURIComponent(caja.value.trim());
      fetch(url, { credentials: "same-origin" }).then(function (r) { return r.json(); }).then(function (d) {
        if (mio !== turno) return;
        var actual = sel.value;
        Array.from(sel.options).forEach(function (o) { if (o.value && o.value !== actual) o.remove(); });
        (d.results || []).forEach(function (r) {
          if (String(r.id) === actual) return;
          var o = new Option(r.text, r.id);
          Object.keys(r.extra || {}).forEach(function (k) { o.dataset[k] = r.extra[k]; });
          sel.add(o);
        });
        if (vacia) vacia.textContent = d.results && d.results.length ? "Elige… (" + d.results.length + " encontrados)" : "Nada encontrado";
        if (!actual && d.results && d.results.length === 1 && caja.value.trim()) {
          sel.value = String(d.results[0].id);
          sel.dispatchEvent(new Event("change", { bubbles: true }));
        }
      }).catch(function () { if (vacia) vacia.textContent = "No se pudo buscar (¿sesión cerrada?)"; });
    }
    sel.addEventListener("change", function () {
      var o = sel.options[sel.selectedIndex];
      Object.keys(sel.dataset).forEach(function (k) { if (k === "nap") delete sel.dataset[k]; });
      if (o && o.dataset.nap) sel.dataset.nap = o.dataset.nap;
    }, true);
    caja.addEventListener("input", function () { clearTimeout(espera); espera = setTimeout(cargar, 250); });
    cargar();
  });
}


// 6) Paso 20: arranque. Si el base.html NO trae los <script> de jQuery/Select2 (por ejemplo un
//    base.html propio de antes del paso 17), panel.js los carga solo desde static/vendor.
//    Si tampoco existen, queda el buscador simple. Nunca un select de clientes vacío.
(function () {
  if (!document.querySelector(".formulario select, .formulario-ancho select")) return;
  var yo = document.querySelector('script[src*="js/panel.js"]');
  var base = yo ? yo.getAttribute("src").replace(/js\/panel\.js.*$/, "") : "/static/";
  function cargar(src) {
    return new Promise(function (ok, mal) {
      var s = document.createElement("script");
      s.src = base + src; s.onload = ok; s.onerror = function () { mal(new Error("no se encontró " + base + src)); };
      document.head.appendChild(s);
    });
  }
  function listo() {
    try { iniciarSelect2(); console.info("[TvnetLago] Select2 activo"); }
    catch (e) { console.error("[TvnetLago] Select2 falló, se usa el buscador simple:", e); buscadorSimple(); }
  }
  if (window.jQuery && jQuery.fn.select2) return listo();
  console.warn("[TvnetLago] El base.html no trae jQuery/Select2; cargándolos desde " + base + "vendor/ …");
  if (!document.querySelector('link[href*="select2"]')) {
    var css = document.createElement("link"); css.rel = "stylesheet"; css.href = base + "vendor/select2/select2.min.css";
    document.head.appendChild(css);
  }
  (window.jQuery ? Promise.resolve() : cargar("vendor/jquery/jquery.min.js"))
    .then(function () { return jQuery.fn.select2 ? null : cargar("vendor/select2/select2.min.js"); })
    .then(function () { return cargar("vendor/select2/i18n/es.js").catch(function () {}); })
    .then(listo)
    .catch(function (e) { console.error("[TvnetLago] " + e.message); buscadorSimple(); });
})();
