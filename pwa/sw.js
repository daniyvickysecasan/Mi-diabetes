// Service worker: guarda solo la "carcasa" de la app para que abra sin conexión.
// Los datos de salud (/api, /auth) NUNCA se guardan en caché.
const CACHE = "mi-diabetes-v7";
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
