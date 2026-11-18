"use strict";
// PWA mínima: entrada con passkey y panel de glucosa. Sin dependencias externas.

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
    const r = p.recoleccion;
    $("recoleccion").textContent = r.error ? `Recolección con error: ${r.error}` :
      r.ultima ? `Recolectado a las ${hora(r.ultima)}` : "Recolección sin ejecutar";
  } catch (e) {
    if (e.estado === 401) return mostrar("entrada");
    $("recoleccion").textContent = "Sin conexión con el servidor";
  }
}

let temporizador;
function mostrar(cual) {
  $("entrada").classList.toggle("oculto", cual !== "entrada");
  $("panel").classList.toggle("oculto", cual !== "panel");
  clearInterval(temporizador);
  if (cual === "panel") { refrescar(); temporizador = setInterval(refrescar, 60e3); }
}

async function iniciar() {
  try {
    const e = await pedir("/auth/estado");
    if (e.autenticado) return mostrar("panel");
    $("alta").open = !e.hay_passkeys;
    mostrar("entrada");
  } catch {
    mostrar("entrada");
    $("msg-entrada").textContent = "No se puede contactar con el servidor.";
  }
}

document.addEventListener("DOMContentLoaded", () => {
  $("btn-entrar").addEventListener("click", entrar);
  $("btn-alta").addEventListener("click", darDeAlta);
  $("btn-salir").addEventListener("click", async () => { await pedir("/auth/salir", {}); mostrar("entrada"); });
  document.addEventListener("visibilitychange", () => { if (!document.hidden && !$("panel").classList.contains("oculto")) refrescar(); });
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js");
  iniciar();
});
