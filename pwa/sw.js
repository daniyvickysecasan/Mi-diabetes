// Service worker: guarda solo la "carcasa" de la app para que abra sin conexión.
// Los datos de salud (/api, /auth) NUNCA se guardan en caché.
const CACHE = "mi-diabetes-v16";
const CARCASA = ["/", "/index.html", "/estilo.css", "/app.js", "/manifest.webmanifest",
                 "/icono.svg", "/icono-180.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(CARCASA)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  if (url.pathname.startsWith("/api") || url.pathname.startsWith("/auth") || url.pathname === "/salud") return;
  // Red primero, caché si no hay conexión.
  e.respondWith(fetch(e.request, { cache: "no-cache" }).then((r) => {
    const copia = r.clone();
    caches.open(CACHE).then((c) => c.put(e.request, copia));
    return r;
  }).catch(() => caches.match(e.request).then((r) => r || caches.match("/index.html"))));
});

// Avisos Web Push (fase 4): el servicio de anomalías los manda cifrados; al pulsar se abre la app en Avisos.
self.addEventListener("push", (e) => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch { d = { texto: e.data ? e.data.text() : "" }; }
  e.waitUntil(self.registration.showNotification(d.titulo || "Mi Diabetes", {
    body: d.texto || "", tag: d.id ? `anomalia-${d.id}` : undefined, icon: "/icono-180.png",
    data: { url: d.url || "/#avisos" }, requireInteraction: !!d.urgente,
  }));
});

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || "/#avisos";
  e.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((cs) => {
    for (const c of cs) {
      if (new URL(c.url).origin === location.origin && "focus" in c) {
        c.postMessage({ ir: "avisos" });
        return c.focus();
      }
    }
    return self.clients.openWindow(url);
  }));
});
