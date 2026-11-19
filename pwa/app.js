"use strict";
// PWA: entrada con passkey, panel, sugerencia de dosis, registro de dosis e hidratos, Tresiba y revisión.
// Sin dependencias externas. Los registros sin conexión esperan en una cola y se reenvían solos.

const ZONA = "Europe/Madrid";
const $ = (id) => document.getElementById(id);

// ------------------------------------------------------------------ utilidades
const b64aBytes = (s) => {
  s = s.replace(/-/g, "+").replace(/_/g, "/");
  const bin = atob(s + "===".slice((s.length + 3) % 4));
  return Uint8Array.from(bin, (c) => c.charCodeAt(0)).buffer;
};
const bytesAB64 = (buf) => btoa(String.fromCharCode(...new Uint8Array(buf)))
  .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");

async function pedir(ruta, cuerpo) {
  const r = await fetch(ruta, {
    method: cuerpo === undefined ? "GET" : "POST",
    headers: cuerpo === undefined ? {} : { "Content-Type": "application/json" },
    body: cuerpo === undefined ? undefined : JSON.stringify(cuerpo),
    credentials: "same-origin",
  });
  const datos = await r.json().catch(() => ({}));
  if (!r.ok) throw Object.assign(new Error(datos.detail || `Error ${r.status}`), { estado: r.status });
  return datos;
}

const hora = (iso) => new Date(iso).toLocaleTimeString("es-ES", { timeZone: ZONA, hour: "2-digit", minute: "2-digit" });

// ------------------------------------------------------------------ passkey
function credencialAJson(c) {
  const r = c.response;
  const json = { id: c.id, rawId: bytesAB64(c.rawId), type: c.type,
                 clientExtensionResults: c.getClientExtensionResults ? c.getClientExtensionResults() : {},
                 authenticatorAttachment: c.authenticatorAttachment || undefined, response: {} };
  json.response.clientDataJSON = bytesAB64(r.clientDataJSON);
  if (r.attestationObject) {
    json.response.attestationObject = bytesAB64(r.attestationObject);
    json.response.transports = r.getTransports ? r.getTransports() : [];
  } else {
    json.response.authenticatorData = bytesAB64(r.authenticatorData);
    json.response.signature = bytesAB64(r.signature);
    if (r.userHandle) json.response.userHandle = bytesAB64(r.userHandle);
  }
  return json;
}

async function darDeAlta() {
  const msg = $("msg-entrada");
  msg.textContent = "";
  try {
    const { id_reto, opciones: o } = await pedir("/auth/alta/opciones", { codigo_alta: $("codigo").value });
    o.challenge = b64aBytes(o.challenge);
    o.user.id = b64aBytes(o.user.id);
    (o.excludeCredentials || []).forEach((c) => (c.id = b64aBytes(c.id)));
    const cred = await navigator.credentials.create({ publicKey: o });
    await pedir("/auth/alta/verificar", { id_reto, credencial: credencialAJson(cred), nombre: $("nombre").value });
    iniciar();
  } catch (e) {
    msg.textContent = e.message || String(e);
  }
}

async function entrar() {
  const msg = $("msg-entrada");
  msg.textContent = "";
  try {
    const { id_reto, opciones: o } = await pedir("/auth/entrada/opciones", {});
    o.challenge = b64aBytes(o.challenge);
    (o.allowCredentials || []).forEach((c) => (c.id = b64aBytes(c.id)));
    const cred = await navigator.credentials.get({ publicKey: o });
    await pedir("/auth/entrada/verificar", { id_reto, credencial: credencialAJson(cred) });
    iniciar();
  } catch (e) {
    msg.textContent = e.message || String(e);
  }
}

// ------------------------------------------------------------------ panel
function clase(v, [bajo, alto]) { return v < bajo ? "bajo" : v > alto ? "alto" : "rango"; }

function pintarActual(a, rango) {
  const valor = $("valor");
  if (!a) { valor.textContent = "--"; $("edad").textContent = "Sin lecturas todavía"; return; }
  valor.textContent = a.valor_mgdl;
  valor.className = `valor ${clase(a.valor_mgdl, rango)}${a.antigua ? " antigua" : ""}`;
  $("flecha").textContent = a.flecha || "";
  $("delta").textContent = a.delta_5min_mgdl == null ? "" :
    `${a.delta_5min_mgdl > 0 ? "+" : ""}${Math.round(a.delta_5min_mgdl)}`;
  const min = Math.round(a.minutos_desde_lectura);
  $("edad").textContent = `${hora(a.ts)} · hace ${min} min`;
  const aviso = $("aviso");
  aviso.classList.toggle("oculto", !a.antigua);
  aviso.textContent = a.antigua ? `Lectura antigua (${min} min). Comprueba el sensor o LibreLinkUp.` : "";
}

function pintarGrafica(serie, [bajo, alto]) {
  const svg = $("grafica"), W = 360, H = 180, P = 24, maxV = 300, minV = 40;
  const ahora = Date.now(), t0 = ahora - 24 * 3600e3;
  const x = (t) => P + ((t - t0) / (ahora - t0)) * (W - 2 * P);
  const y = (v) => H - P - ((Math.min(maxV, Math.max(minV, v)) - minV) / (maxV - minV)) * (H - 2 * P);
  const ns = "http://www.w3.org/2000/svg";
  const el = (tag, at) => { const e = document.createElementNS(ns, tag); for (const k in at) e.setAttribute(k, at[k]); return e; };
  svg.replaceChildren();
  svg.append(el("rect", { x: P, y: y(alto), width: W - 2 * P, height: y(bajo) - y(alto), class: "banda" }));
  for (const v of [bajo, alto]) {
    svg.append(el("line", { x1: P, x2: W - P, y1: y(v), y2: y(v), class: "limite" }));
    const t = el("text", { x: 2, y: y(v) + 4, class: "eje" }); t.textContent = v; svg.append(t);
  }
  for (let h = 0; h <= 24; h += 6) {
    const t = el("text", { x: x(t0 + h * 3600e3), y: H - 6, class: "eje", "text-anchor": "middle" });
    t.textContent = h === 24 ? "ahora" : hora(new Date(t0 + h * 3600e3).toISOString());
    svg.append(t);
  }
  for (const p of serie) {
    const t = Date.parse(p.ts);
    svg.append(el("circle", { cx: x(t).toFixed(1), cy: y(p.valor_mgdl).toFixed(1), r: 1.6,
      class: `p-${clase(p.valor_mgdl, [bajo, alto])}` }));
  }
}

function pintarTir(div, titulo, t) {
  div.replaceChildren();
  const h = document.createElement("div");
  h.className = "leyenda";
  if (t.pct_en_rango == null) { h.textContent = `${titulo}: sin datos`; div.append(h); return; }
  const barra = document.createElement("div");
  barra.className = "barra";
  for (const [c, v] of [["b", t.pct_bajo], ["r", t.pct_en_rango], ["a", t.pct_alto]]) {
    const s = document.createElement("span"); s.className = c; s.style.width = `${v}%`; barra.append(s);
  }
  const partes = [[titulo, ""], [`↓ ${t.pct_bajo}%`, ""], [`${t.pct_en_rango}% en rango`, "fuerte"], [`↑ ${t.pct_alto}%`, ""]];
  for (const [texto, c] of partes) { const s = document.createElement("span"); s.textContent = texto; s.className = c; h.append(s); }
  const cob = document.createElement("div");
  cob.className = "leyenda suave";
  cob.textContent = `Media ${Math.round(t.media_mgdl)} mg/dL · datos ${t.cobertura_pct}% del tiempo` +
    (t.cobertura_pct < 70 ? " (menos del 70 %: poco fiable)" : "");
  div.append(barra, h, cob);
}

async function refrescar() {
  try {
    const p = await pedir("/api/panel");
    pintarActual(p.actual, p.rango_mgdl);
    pintarGrafica(p.serie_24h, p.rango_mgdl);
    $("rango").textContent = `(${p.rango_mgdl[0]}–${p.rango_mgdl[1]})`;
    pintarTir($("tir24"), "24 h", p.tir_24h);
    pintarTir($("tir14"), "14 días", p.tir_14d);
    pintarFase2(p);
    estadoLibreView();
    avisoSegunda();
    const r = p.recoleccion;
    $("recoleccion").textContent = r.error ? `Recolección con error: ${r.error}` :
      r.ultima ? `Recolectado a las ${hora(r.ultima)}` : "Recolección sin ejecutar";
  } catch (e) {
    if (e.estado === 401) return mostrar("entrada");
    $("recoleccion").textContent = "Sin conexión con el servidor";
  }
}

// ------------------------------------------------------------------ formato
const num = (v, d = 1) => (v == null ? "--" : Number(v).toLocaleString("es-ES", { maximumFractionDigits: d }));
const fechaHora = (iso) => new Date(iso).toLocaleString("es-ES", { timeZone: ZONA, day: "2-digit", month: "2-digit",
  hour: "2-digit", minute: "2-digit" });
const leerNumero = (s) => { const v = parseFloat(String(s).replace(",", ".")); return Number.isFinite(v) ? v : NaN; };
const dosDig = (n) => String(n).padStart(2, "0");
function valorLocal(d = new Date()) {     // para <input type="datetime-local"> (hora del móvil)
  return `${d.getFullYear()}-${dosDig(d.getMonth() + 1)}-${dosDig(d.getDate())}T${dosDig(d.getHours())}:${dosDig(d.getMinutes())}`;
}
const desdeLocal = (v) => new Date(v).toISOString();
function nuevoId() {
  if (crypto.randomUUID) return crypto.randomUUID();
  const b = crypto.getRandomValues(new Uint8Array(16));
  return Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
}
const li = (texto, clase = "") => { const e = document.createElement("li"); e.textContent = texto; if (clase) e.className = clase; return e; };

let avisoT;
function aviso(texto, error = false) {
  const m = $("msg");
  m.textContent = texto; m.classList.toggle("error", error); m.classList.remove("oculto");
  clearTimeout(avisoT); avisoT = setTimeout(() => m.classList.add("oculto"), 4000);
}

const ESTADOS = {
  pendiente: "esperando a la pluma", emparejada: "confirmada por la pluma", discrepancia: "no coincide con la pluma",
  solo_app: "la pluma no la tiene", solo_pluma: "solo en la pluma", descartada: "descartada",
};
const ORIGENES = { app: "app", pluma_libreview: "pluma", csv_libreview: "pluma (CSV)", manual: "manual" };

// ------------------------------------------------------------------ cola sin conexión
// Cada registro lleva un id_cliente: si el envío se repite, el servidor no duplica nada.
const CLAVE_COLA = "md-cola-v1";
const leerCola = () => { try { return JSON.parse(localStorage.getItem(CLAVE_COLA) || "[]"); } catch { return []; } };
const guardarCola = (c) => { try { localStorage.setItem(CLAVE_COLA, JSON.stringify(c)); } catch { /* sin almacenamiento */ } };

function pintarCola() {
  const c = leerCola(), div = $("cola");
  div.classList.toggle("oculto", c.length === 0);
  div.replaceChildren();
  if (!c.length) return;
  const pend = c.filter((x) => !x.error), mal = c.filter((x) => x.error);
  if (pend.length) {
    const p = document.createElement("div");
    p.textContent = `${pend.length} registro${pend.length > 1 ? "s" : ""} sin enviar (se enviará${pend.length > 1 ? "n" : ""} al recuperar la conexión): ` +
      pend.map((x) => x.texto).join(" · ") + ". La insulina activa aún no los incluye.";
    div.append(p);
  }
  for (const x of mal) {
    const p = document.createElement("div");
    p.className = "error";
    p.textContent = `Rechazado: ${x.texto} (${x.error}). `;
    const b = document.createElement("button");
    b.className = "enlace"; b.textContent = "Quitar";
    b.addEventListener("click", () => { guardarCola(leerCola().filter((y) => y.cuerpo.id_cliente !== x.cuerpo.id_cliente)); pintarCola(); });
    p.append(b); div.append(p);
  }
}

let enviando = false;
async function vaciarCola() {
  if (enviando) return;
  enviando = true;
  try {
    for (const x of leerCola()) {
      if (x.error) continue;
      try {
        await pedir(x.ruta, x.cuerpo);
        guardarCola(leerCola().filter((y) => y.cuerpo.id_cliente !== x.cuerpo.id_cliente));
      } catch (e) {
        if (e.estado === 401) { mostrar("entrada"); break; }
        if (e.estado && e.estado >= 400 && e.estado < 500) {      // datos inválidos: no reintentar
          guardarCola(leerCola().map((y) => (y.cuerpo.id_cliente === x.cuerpo.id_cliente ? { ...y, error: e.message } : y)));
          continue;
        }
        break;                                                     // sin conexión o servidor caído
      }
    }
  } finally {
    enviando = false;
    pintarCola();
  }
}

async function registrar(ruta, cuerpo, texto) {
  const c = leerCola();
  c.push({ ruta, cuerpo, texto, creado: new Date().toISOString() });
  guardarCola(c);
  await vaciarCola();
  const sigue = leerCola().find((y) => y.cuerpo.id_cliente === cuerpo.id_cliente);
  if (!sigue) aviso(`Guardado: ${texto}`);
  else if (sigue.error) aviso(`No se ha guardado: ${sigue.error}`, true);
  else aviso(`Sin conexión: ${texto} queda en cola y se enviará solo`);
  refrescarVista();
}

// ------------------------------------------------------------------ LibreView (lectura y código del SMS)
// LibreView solo se lee cuando se pide. La app deja la petición en la base y el recolector la
// atiende en unos segundos.
let sondeoLv = null, esperandoDesde = 0, antesEsperando = false;

function pintarLibreView(e) {
  $("lv-ultimo").textContent = e.ultimo_ok ? `· leída a las ${hora(e.ultimo_ok)}` : "";
  $("lv-actualizar").disabled = e.accion === "esperar";
  const acabaDeLeer = antesEsperando && e.accion !== "esperar";
  antesEsperando = e.accion === "esperar";
  if (acabaDeLeer && e.estado === "ok") { refrescar(); }
  const caja = $("aviso-libreview");
  caja.classList.toggle("oculto", !e.mensaje);
  $("lv-texto").textContent = e.mensaje || "";
  $("lv-sms").classList.toggle("oculto", e.accion !== "pedir_sms");
  const f = $("lv-form");
  const ver = e.accion === "codigo";
  if (ver && f.classList.contains("oculto")) setTimeout(() => $("lv-codigo").focus(), 50);
  f.classList.toggle("oculto", !ver);
  // Mientras el recolector trabaja, mira cada 3 s (máximo 2 min).
  clearTimeout(sondeoLv);
  if (e.accion !== "esperar") { esperandoDesde = 0; return; }
  esperandoDesde ||= Date.now();
  if (Date.now() - esperandoDesde < 120e3) sondeoLv = setTimeout(estadoLibreView, 3000);
}

async function estadoLibreView() {
  try { pintarLibreView(await pedir("/api/libreview/estado")); } catch { /* sin conexión: se reintenta */ }
}

async function accionLibreView(ruta, cuerpo) {
  try {
    esperandoDesde = 0;
    pintarLibreView(await pedir(ruta, cuerpo));
  } catch (e) {
    if (e.estado === 401) return mostrar("entrada");
    aviso(e.message, true);
    estadoLibreView();
  }
}

$("lv-sms").addEventListener("click", () => accionLibreView("/api/libreview/sms", {}));
$("lv-actualizar").addEventListener("click", () => accionLibreView("/api/libreview/actualizar", {}));
$("lv-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const codigo = $("lv-codigo").value.replace(/\D/g, "");
  if (codigo.length < 4) return aviso("El código son de 4 a 8 cifras", true);
  $("lv-codigo").value = "";
  accionLibreView("/api/libreview/codigo", { codigo });
});

// ------------------------------------------------------------------ panel (fase 2)
function pintarFase2(p) {
  const i = p.insulina_activa;
  $("iob").textContent = num(i.unidades, 2);
  const n = i.dosis.length;
  $("iob-detalle").textContent = n ? `${n} dosis en las últimas 5 h · curva Fiasp (pico ${p.curva.pico_min} min, ${p.curva.duracion_min / 60} h)` +
    (i.con_dudas ? " · incluye dosis sin confirmar por la pluma" : "") : "Sin dosis de Fiasp en las últimas 5 h";
  const ap = $("aviso-pluma");
  ap.classList.toggle("oculto", !p.aviso_pluma);
  ap.textContent = p.aviso_pluma ? p.aviso_pluma.texto : "";
  const r = $("aviso-revisar");
  r.classList.toggle("oculto", !p.por_revisar);
  r.textContent = p.por_revisar ? `${p.por_revisar} dosis por revisar →` : "";
  const b = $("badge");
  b.classList.toggle("oculto", !p.por_revisar); b.textContent = p.por_revisar || "";
  const m = p.metricas_14d, dl = $("metricas");
  dl.replaceChildren();
  const filas = [
    ["< 54", m.pct_muy_bajo, "%", "b"], ["54–69", m.pct_bajo, "%", "b"], ["70–180 (TIR)", m.pct_en_rango, "%", "r"],
    ["181–250", m.pct_alto, "%", "a"], ["> 250", m.pct_muy_alto, "%", "a"], ["GMI", m.gmi_pct, "%", ""],
    ["CV", m.cv_pct, "%", m.cv_pct > 36 ? "a" : ""], ["Media", m.media_mgdl, " mg/dL", ""],
  ];
  for (const [k, v, u, c] of filas) {
    const dt = document.createElement("dt"); dt.textContent = k;
    const dd = document.createElement("dd"); dd.textContent = v == null ? "--" : `${num(v)}${u}`; if (c) dd.className = `t-${c}`;
    dl.append(dt, dd);
  }
  const dt = document.createElement("dt"); dt.textContent = "Datos";
  const dd = document.createElement("dd"); dd.textContent = `${num(m.cobertura_pct)}%` + (m.suficiente ? "" : " (poco fiable)");
  dl.append(dt, dd);
}

// ------------------------------------------------------------------ registrar
async function cargarRegistrar() {
  if (!$("t-fiasp").dataset.tocado) $("t-fiasp").value = valorLocal();
  if (!$("hc-t").dataset.tocado) $("hc-t").value = valorLocal();
  try {
    const [d, h, c] = await Promise.all([pedir("/api/dosis?horas=48"), pedir("/api/hidratos-correccion?horas=48"),
      pedir("/api/comidas/recientes")]);
    const sel = $("c-fiasp"), actual = sel.value;
    sel.replaceChildren(new Option("Sin comida", ""));
    for (const x of c.comidas) sel.append(new Option(`${hora(x.ts)} · ${x.descripcion || "comida"}${x.hidratos_g != null ? ` (${num(x.hidratos_g, 0)} g)` : ""}`, x.id));
    sel.value = actual;
    const items = [
      ...d.dosis.map((x) => ({ ts: x.ts, texto: `${fechaHora(x.ts)} · ${x.insulina === "fiasp" ? "Fiasp" : "Tresiba"} ${num(x.unidades)} U · ${ORIGENES[x.origen] || x.origen} · ${ESTADOS[x.estado] || x.estado}${x.cuenta ? "" : " · no cuenta"}`,
        clase: x.requiere_revision ? "revisar" : (x.cuenta ? "" : "tenue") })),
      ...h.hidratos.map((x) => ({ ts: x.ts, texto: `${fechaHora(x.ts)} · Hidratos ${num(x.gramos)} g · ${x.motivo}`, clase: "" })),
    ].sort((a, b) => b.ts.localeCompare(a.ts));
    const ul = $("historial");
    ul.replaceChildren(...(items.length ? items.map((x) => li(x.texto, x.clase)) : [li("Nada registrado", "suave")]));
  } catch (e) {
    if (e.estado === 401) return mostrar("entrada");
    $("historial").replaceChildren(li("Sin conexión: el historial no está disponible", "suave"));
  }
}

function enviarFiasp(ev) {
  ev.preventDefault();
  const u = leerNumero($("u-fiasp").value);
  if (!(u > 0)) return aviso("Escribe las unidades", true);
  if (!$("t-fiasp").value) return aviso("Falta la hora", true);
  const cuerpo = { id_cliente: nuevoId(), ts: desdeLocal($("t-fiasp").value), unidades: u };
  if ($("c-fiasp").value) cuerpo.comida_id = Number($("c-fiasp").value);
  if ($("n-fiasp").value.trim()) cuerpo.notas = $("n-fiasp").value.trim();
  $("f-fiasp").reset(); delete $("t-fiasp").dataset.tocado; $("t-fiasp").value = valorLocal();
  registrar("/api/dosis", cuerpo, `Fiasp ${num(u)} U a las ${hora(cuerpo.ts)}`);
}

function enviarHidratos(ev) {
  ev.preventDefault();
  const g = leerNumero($("hc-gramos").value);
  if (!(g > 0)) return aviso("Escribe los gramos", true);
  const cuerpo = { id_cliente: nuevoId(), ts: desdeLocal($("hc-t").value), gramos: g, motivo: $("hc-motivo").value };
  $("f-hc").reset(); delete $("hc-t").dataset.tocado; $("hc-t").value = valorLocal();
  registrar("/api/hidratos-correccion", cuerpo, `hidratos ${num(g)} g a las ${hora(cuerpo.ts)}`);
}

// ------------------------------------------------------------------ Tresiba
const franja = (c) => (!c.hora_fin_local || c.hora_fin_local === c.hora_local
  ? `a las ${c.hora_local}` : `entre las ${c.hora_local} y las ${c.hora_fin_local}`);
async function cargarTresiba() {
  if (!$("tr-desde").dataset.tocado) $("tr-desde").value = valorLocal();
  try {
    const t = await pedir("/api/tresiba");
    const v = t.vigente;
    $("tr-vigente").textContent = v ? `${num(v.unidades)} U cada día ${franja(v)}` : "Sin configurar todavía.";
    if (v && !$("tr-u").value) {
      $("tr-u").value = String(v.unidades).replace(".", ",");
      $("tr-hora").value = v.hora_local; $("tr-hora-fin").value = v.hora_fin_local;
    }
    $("tr-historial").replaceChildren(...(t.historial.length ? t.historial.map((h) =>
      li(`Desde ${fechaHora(h.vigente_desde)}: ${num(h.unidades)} U ${franja(h)}${h.notas ? ` · ${h.notas}` : ""}`))
      : [li("Sin cambios registrados", "suave")]));
  } catch (e) {
    if (e.estado === 401) return mostrar("entrada");
    $("tr-vigente").textContent = "Sin conexión con el servidor.";
  }
}

async function enviarTresiba(ev) {
  ev.preventDefault();
  const u = leerNumero($("tr-u").value);
  if (!(u > 0) || !$("tr-hora").value || !$("tr-hora-fin").value) return aviso("Faltan las unidades o la franja", true);
  try {
    await pedir("/api/tresiba", { unidades: u, hora_local: $("tr-hora").value, hora_fin_local: $("tr-hora-fin").value,
      vigente_desde: desdeLocal($("tr-desde").value),
      notas: $("tr-notas").value.trim() || null });
    aviso(`Tresiba: ${num(u)} U ${franja({ hora_local: $("tr-hora").value, hora_fin_local: $("tr-hora-fin").value })}`);
    $("tr-notas").value = ""; delete $("tr-desde").dataset.tocado;
    cargarTresiba();
  } catch (e) {
    aviso(e.estado ? e.message : "Sin conexión: el cambio de Tresiba necesita conexión", true);
  }
}

// ------------------------------------------------------------------ revisar
function descripcion(d) {
  return `${ORIGENES[d.origen] || d.origen}: ${num(d.unidades)} U a las ${hora(d.ts)} (${fechaHora(d.ts).split(",")[0]})`;
}

async function accionRevision(id, accion) {
  try {
    await pedir(`/api/dosis/${id}/revision`, { accion });
    aviso("Hecho");
    cargarRevisar();
  } catch (e) {
    aviso(e.estado ? e.message : "Sin conexión: la revisión necesita conexión", true);
  }
}

async function cargarRevisar() {
  const ul = $("revisar-lista");
  try {
    const { dosis } = await pedir("/api/dosis/revisar");
    if (!dosis.length) { ul.replaceChildren(li("Nada que revisar", "suave")); $("badge").classList.add("oculto"); return; }
    ul.replaceChildren(...dosis.map((d) => {
      const e = document.createElement("li");
      e.className = "caso";
      const t = document.createElement("div"); t.className = "fuerte";
      t.textContent = descripcion(d) + (d.par ? ` · ${descripcion(d.par)}` : "");
      const m = document.createElement("div"); m.className = "suave pequeno"; m.textContent = d.motivo || ESTADOS[d.estado];
      const acciones = document.createElement("div"); acciones.className = "acciones";
      const boton = (texto, id, accion) => { const b = document.createElement("button"); b.textContent = texto;
        b.addEventListener("click", () => accionRevision(id, accion)); acciones.append(b); };
      boton(d.estado === "discrepancia" ? "Vale, cuenta la pluma" : "Está bien así", d.id, "aceptar");
      if (d.par) {
        boton("Son dos dosis distintas", d.id, "separar");
        const app = d.origen === "app" ? d : d.par, pluma = d.origen === "app" ? d.par : d;
        boton("Borrar la de la app", app.id, "descartar");
        boton("No es una dosis (pluma)", pluma.id, "descartar");
      } else {
        boton(d.origen === "app" ? "Borrar: registro erróneo" : "No es una dosis (cebado)", d.id, "descartar");
      }
      e.append(t, m, acciones);
      return e;
    }));
  } catch (e) {
    if (e.estado === 401) return mostrar("entrada");
    ul.replaceChildren(li("Sin conexión con el servidor", "suave"));
  }
}

// ------------------------------------------------------------------ dosis (fase 3)
// Antes de cada cálculo se lee la pluma en LibreView y se pregunta si falta alguna dosis por apuntar.
let tipoSug = "comida", ocupado = false;
const NIVEL = { alta: "Incertidumbre alta", media: "Incertidumbre media", baja: "Incertidumbre baja" };
const DECISION = { hecho: "Hecho", modificado: "Modificado", ignorado: "Ignorado" };
const espera = (ms) => new Promise((r) => setTimeout(r, ms));
function nodo(tag, cls = "", texto = "") { const e = document.createElement(tag); if (cls) e.className = cls; if (texto) e.textContent = texto; return e; }
const numero = (id) => { const v = leerNumero($(id).value); return Number.isFinite(v) ? v : undefined; };

function peticionSugerencia() {
  if (tipoSug === "correccion") return { tipo: "correccion" };
  const hc = numero("s-hc");
  if (!(hc >= 0)) throw new Error("Escribe los hidratos (0 si no comes)");
  const p = { tipo: "comida", hidratos_g: hc };
  for (const [id, k] of [["s-hc-min", "hidratos_min_g"], ["s-hc-max", "hidratos_max_g"], ["s-grasa", "grasa_g"], ["s-prot", "proteina_g"]]) {
    const v = numero(id); if (v !== undefined) p[k] = v;
  }
  return p;
}

async function leerPluma() {
  const est = $("s-estado");
  est.textContent = "Leyendo las dosis de la pluma en LibreView…";
  const { solicitud } = await pedir("/api/libreview/actualizar", {});
  for (let i = 0; i < 45; i++) {
    const s = await pedir(`/api/libreview/actualizar/${solicitud}`);
    if (s.resultado) {
      est.textContent = s.resultado === "ok" ? "Pluma leída." : s.resultado === "sin_sesion"
        ? "LibreView pide el código del SMS (dáselo en el panel) o apunta tus dosis."
        : `No se ha podido leer la pluma: ${s.detalle || "error"}.`;
      return solicitud;
    }
    await espera(2000);
  }
  est.textContent = "El lector de la pluma no responde.";
  return solicitud;
}

// Leer LibreView no basta: la pluma solo sube sus dosis cuando la acercas a LibreLink. Antes de cada
// cálculo se pregunta si hay alguna inyección posterior a esa sincronización sin apuntar.
function preguntar(sinc, leida) {
  const caja = $("s-confirmar");
  $("s-pregunta").textContent = (leida
    ? (sinc ? `La pluma pasó por LibreLink por última vez a las ${hora(sinc)} (${fechaHora(sinc).split(",")[0]}).` : "No sé cuándo pasó la pluma por LibreLink.")
    : "No se han podido leer las dosis de la pluma.") + " ¿Te has puesto Fiasp después sin apuntarla?";
  caja.classList.remove("oculto");
  return new Promise((resolver) => {
    const fin = (v) => { caja.classList.add("oculto"); $("s-no").onclick = $("s-si").onclick = null; resolver(v); };
    $("s-no").onclick = () => fin(false);
    $("s-si").onclick = () => fin(true);
  });
}

// Pide al servidor (que responde 409 mientras la lectura no ha terminado) y pinta el resultado.
async function conPluma(ruta, cuerpo) {
  if (ocupado) return;
  ocupado = true; $("s-calcular").disabled = true;
  try {
    cuerpo.solicitud_lectura_id = await leerPluma();
    const [lect, est] = await Promise.all([pedir(`/api/libreview/actualizar/${cuerpo.solicitud_lectura_id}`),
      pedir("/api/libreview/estado")]);
    if (await preguntar(est.ultima_sincronizacion, lect.resultado === "ok")) {
      aviso("Apunta la dosis y vuelve a calcular");
      return ir("registrar", "u-fiasp");
    }
    cuerpo.dosis_apuntadas = true;
    let s;
    for (let i = 0; i < 10 && !s; i++) {
      try { s = await pedir(ruta, cuerpo); } catch (e) { if (e.estado !== 409) throw e; await espera(3000); }
    }
    if (!s) throw new Error("La lectura de la pluma no termina: inténtalo de nuevo");
    pintarSugerencia(s);
    cargarDosis();
  } catch (e) {
    if (e.estado === 401) return mostrar("entrada");
    aviso(e.estado ? e.message : "Sin conexión: el cálculo necesita conexión", true);
  } finally { ocupado = false; $("s-calcular").disabled = false; }
}

function calcularDosis(ev) {
  ev.preventDefault();
  let cuerpo;
  try { cuerpo = peticionSugerencia(); } catch (e) { return aviso(e.message, true); }
  conPluma("/api/sugerencias", cuerpo);
}

const comprobarSegunda = (id) => { ir("dosis"); conPluma(`/api/sugerencias/${id}/segunda`, {}); };

function textoDecision(d) {
  let t = `${DECISION[d.decision]}${d.unidades != null ? `: ${num(d.unidades)} U` : ""} a las ${hora(d.ts)}`;
  const r = d.resultado;
  if (r && r.lecturas) t += ` · a las 3 h ${r.glucosa_3h ?? "--"}, mín ${r.minima}, máx ${r.maxima}, ${num(r.pct_en_rango, 0)}% en rango${r.hipo ? " · HIPO" : ""}`;
  else if (!r) t += " · resultado a las 4 h";
  return t;
}

async function decidirSug(s, decision, unidades) {
  try {
    const cuerpo = { id_cliente: nuevoId(), decision };
    if (decision === "modificado") cuerpo.unidades = unidades;
    const r = await pedir(`/api/sugerencias/${s.id}/decision`, cuerpo);
    aviso(`${DECISION[decision]}${r.sugerencia.decision.unidades ? `: ${num(r.sugerencia.decision.unidades)} U apuntadas` : ""}`);
    pintarSugerencia(r.sugerencia);
    cargarDosis();
  } catch (e) {
    if (e.estado === 401) return mostrar("entrada");
    aviso(e.estado ? e.message : "Sin conexión: la decisión necesita conexión", true);
  }
}

function botonesDecision(s) {
  const caja = nodo("div");
  const fila = nodo("div", "decidir");
  const boton = (texto, cls, f) => { const b = nodo("button", cls, texto); b.type = "button"; b.addEventListener("click", f); fila.append(b); };
  const mod = nodo("form", "modificar oculto");
  const inp = nodo("input"); inp.inputMode = "decimal"; inp.placeholder = "U que te has puesto"; inp.setAttribute("aria-label", "Unidades");
  const ok = nodo("button", "primario", "Guardar"); ok.type = "submit";
  mod.append(inp, ok);
  mod.addEventListener("submit", (ev) => {
    ev.preventDefault();
    const u = leerNumero(inp.value);
    if (!(u >= 0)) return aviso("Escribe las unidades", true);
    decidirSug(s, "modificado", u);
  });
  if (!s.bloqueada) boton(`Hecho (${num(s.u_inmediata)} U)`, "primario", () => decidirSug(s, "hecho"));
  boton(s.bloqueada ? "Me he puesto…" : "Modificado", "", () => { mod.classList.remove("oculto"); inp.focus(); });
  boton(s.bloqueada ? "No me pongo" : "Ignorado", "", () => decidirSug(s, "ignorado"));
  caja.append(fila, mod);
  return caja;
}

function pintarSugerencia(s) {
  const c = $("s-resultado");
  c.classList.remove("oculto"); c.replaceChildren();
  const titulo = { segunda: "Segunda inyección", correccion: "Corrección", comida: "Dosis para la comida" }[s.tipo];
  c.append(nodo("h2", "", `${titulo} · ${hora(s.ts)}`));
  if (s.bloqueada) {
    c.append(nodo("div", "sug-bloqueada", `No se sugiere ninguna dosis. ${s.motivo_bloqueo}`));
  } else {
    const u = nodo("div", "sug-u", num(s.u_inmediata)); u.append(nodo("span", "unidad", " U"));
    const inc = s.incertidumbre, f = nodo("div", "centro");
    f.append(nodo("span", `incert ${inc.nivel}`, NIVEL[inc.nivel] || inc.nivel));
    if (inc.rango_u && inc.rango_u[0] !== inc.rango_u[1]) f.append(nodo("span", "suave", ` · entre ${num(inc.rango_u[0])} y ${num(inc.rango_u[1])} U`));
    c.append(u, f);
    if (inc.motivos.length) c.append(nodo("p", "suave pequeno centro", inc.motivos.join(" ")));
    c.append(nodo("p", "suave pequeno desglose", s.tipo === "segunda"
      ? `Glucosa ${s.glucosa_mgdl} mg/dL · insulina activa ${num(s.insulina_activa_u, 2)} U`
      : `Hidratos ${num(s.u_hidratos, 2)} U + corrección ${num(s.u_correccion, 2)} U − insulina activa ${num(s.insulina_activa_u, 2)} U · glucosa ${s.glucosa_mgdl} mg/dL`));
    if (s.u_segunda) c.append(nodo("p", "fuerte", `Y una segunda inyección de ${num(s.u_segunda)} U a las ${hora(s.ts_segunda)} por la grasa y la proteína (${num(s.ugp)} UGP). A esa hora se vuelve a comprobar tu glucosa.`));
  }
  if (s.avisos.length) { const ul = nodo("ul", "avisos"); for (const a of s.avisos) ul.append(nodo("li", "", a)); c.append(ul); }
  c.append(s.decision ? nodo("p", "suave", textoDecision(s.decision)) : botonesDecision(s));
}

const segundaAbierta = (s, ahora = Date.now()) => s.u_segunda && !s.segunda_comprobada_id && s.decision?.decision !== "ignorado"
  && ahora >= Date.parse(s.ts_segunda) - 15 * 60e3 && ahora <= Date.parse(s.ts_segunda) + 120 * 60e3;

async function cargarDosis() {
  const ul = $("s-lista");
  try {
    const { sugerencias } = await pedir("/api/sugerencias?horas=48");
    if (!sugerencias.length) { ul.replaceChildren(li("Todavía no hay sugerencias", "suave")); return; }
    ul.replaceChildren(...sugerencias.map((s) => {
      const e = nodo("li", "caso");
      const cab = s.tipo === "comida" ? `comida ${num(s.hidratos_g, 0)} g` : s.tipo === "segunda" ? "segunda inyección" : "corrección";
      const t = nodo("div", "fuerte", `${fechaHora(s.ts)} · ${cab} · ${s.bloqueada ? "bloqueada" : `${num(s.u_inmediata)} U`}`
        + (s.u_segunda ? ` + ${num(s.u_segunda)} U a las ${hora(s.ts_segunda)}` : ""));
      t.addEventListener("click", () => { pintarSugerencia(s); window.scrollTo(0, 0); });
      e.append(t, nodo("div", "suave pequeno", s.decision ? textoDecision(s.decision) : "Sin decidir: toca para verla"));
      if (segundaAbierta(s)) {
        const b = nodo("button", "primario", `Comprobar la segunda inyección (${hora(s.ts_segunda)})`);
        b.addEventListener("click", () => comprobarSegunda(s.id));
        e.append(b);
      }
      return e;
    }));
  } catch (e) {
    if (e.estado === 401) return mostrar("entrada");
    ul.replaceChildren(li("Sin conexión con el servidor", "suave"));
  }
}

async function avisoSegunda() {
  const a = $("aviso-segunda");
  try {
    const { sugerencias } = await pedir("/api/sugerencias?horas=8");
    const s = sugerencias.find((x) => segundaAbierta(x));
    a.classList.toggle("oculto", !s);
    a.textContent = s ? `Segunda inyección de ${num(s.u_segunda)} U a las ${hora(s.ts_segunda)}: compruébala antes de ponerla →` : "";
  } catch { a.classList.add("oculto"); }
}

// ------------------------------------------------------------------ navegación
let temporizador, vista = "panel";
const CARGAS = { panel: refrescar, dosis: cargarDosis, registrar: cargarRegistrar, tresiba: cargarTresiba, revisar: cargarRevisar };
const refrescarVista = () => CARGAS[vista]();

function ir(cual, foco) {
  vista = cual;
  for (const k of Object.keys(CARGAS)) $(`v-${k}`).classList.toggle("oculto", k !== cual);
  document.querySelectorAll(".nav button").forEach((b) => b.classList.toggle("activo", b.dataset.ir === cual));
  window.scrollTo(0, 0);
  refrescarVista();
  if (foco) setTimeout(() => $(foco).focus(), 50);
}

function mostrar(cual) {
  $("entrada").classList.toggle("oculto", cual !== "entrada");
  $("panel").classList.toggle("oculto", cual !== "panel");
  clearInterval(temporizador);
  if (cual === "panel") {
    ir(vista);
    vaciarCola();
    temporizador = setInterval(() => { vaciarCola(); if (vista === "panel") refrescar(); }, 60e3);
  }
}

async function iniciar() {
  pintarCola();
  try {
    const e = await pedir("/auth/estado");
    if (e.autenticado) return mostrar("panel");
    $("alta").open = !e.hay_passkeys;
    mostrar("entrada");
  } catch {
    // Sin conexión: se puede registrar igualmente; la cola se enviará al volver la conexión.
    mostrar("panel");
    aviso("Sin conexión: puedes registrar y se enviará después", true);
  }
}

document.addEventListener("DOMContentLoaded", () => {
  $("btn-entrar").addEventListener("click", entrar);
  $("btn-alta").addEventListener("click", darDeAlta);
  $("btn-salir").addEventListener("click", async () => { await pedir("/auth/salir", {}); mostrar("entrada"); });
  document.querySelectorAll("[data-ir]").forEach((b) => b.addEventListener("click", (ev) => {
    ev.preventDefault(); ir(b.dataset.ir, b.dataset.foco);
  }));
  document.querySelectorAll("[data-paso]").forEach((b) => b.addEventListener("click", () => {
    const v = Math.max(0, (leerNumero($("u-fiasp").value) || 0) + Number(b.dataset.paso));
    $("u-fiasp").value = String(v).replace(".", ",");
  }));
  document.querySelectorAll("[data-g]").forEach((b) => b.addEventListener("click", () => { $("hc-gramos").value = b.dataset.g; }));
  for (const id of ["t-fiasp", "hc-t", "tr-desde"]) $(id).addEventListener("input", () => { $(id).dataset.tocado = "1"; });
  $("f-fiasp").addEventListener("submit", enviarFiasp);
  $("f-hc").addEventListener("submit", enviarHidratos);
  $("f-tresiba").addEventListener("submit", enviarTresiba);
  $("f-sug").addEventListener("submit", calcularDosis);
  const elegirTipo = (tipo) => {
    tipoSug = tipo;
    document.querySelectorAll("[data-tipo]").forEach((x) => x.classList.toggle("activo", x.dataset.tipo === tipo));
    $("s-comida").classList.toggle("oculto", tipo !== "comida");
  };
  document.querySelectorAll("[data-tipo]").forEach((b) => b.addEventListener("click", () => elegirTipo(b.dataset.tipo)));
  $("btn-correccion").addEventListener("click", () => {   // corrección hacia el objetivo, sin comer
    ir("dosis"); elegirTipo("correccion"); conPluma("/api/sugerencias", { tipo: "correccion" });
  });
  window.addEventListener("online", () => { vaciarCola(); refrescarVista(); });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && !$("panel").classList.contains("oculto")) { vaciarCola(); refrescarVista(); }
  });
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js");
  iniciar();
});
