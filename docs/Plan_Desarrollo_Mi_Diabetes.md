# Mi Diabetes — plan de desarrollo por fases

Borrador del 18-11-2026, pendiente de aprobación. Si este plan y el traspaso discrepan, manda este plan. Parte de la sección 8 de `Traspaso_Mi_Diabetes.md` e incorpora las decisiones tomadas en el chat del proyecto.

## 0. Decisiones incorporadas

- **Dosis:** confirmación en la app al inyectarse; script propio contra la API no oficial de LibreView cada pocos minutos; CSV de LibreView como respaldo semanal; importación única del historial. Si discrepan, **la pluma manda en hora y unidades**; la app manda en el vínculo con la comida y el tipo de insulina.
- **Pluma inteligente solo para Fiasp.** Todo lo que llega de LibreView es Fiasp. La Tresiba se configura en el móvil y la dosis diaria **se da por puesta** salvo aviso.
- **Sin dosis máxima fija.** Límites que quedan: ninguna dosis con hipoglucemia prevista y alerta por acumulación de insulina.
- **Hidratos sin dosis** (hipos, picoteo): botón rápido "hidratos de corrección" con gramos.
- **Parámetros:** hoy no hay ratio ni sensibilidad definidos ("a ojo"). Las sugerencias se muestran **desde el principio con un indicador de incertidumbre**. Objetivo de glucosa: **90 mg/dL** (110 como margen hasta validar la predicción).
- **Registro tipo chat (nuevo):** texto libre rápido ("me estoy tomando unas cervezas con los amigos"). Un modelo local lo convierte en un evento estructurado e incompleto (tipo, hora, estimación aproximada con incertidumbre, etiquetas como alcohol o ejercicio). Nunca produce números de dosis. **Sin aviso por alcohol** (Daniel lo rechazó el 19-11-2026): el alcohol queda solo como etiqueta.
- **Pluma → Nightscout (18-11):** nada de un Android aparte para leer la pluma por NFC. Se usa el script propio contra LibreView; la confirmación en la app sigue siendo la vía en tiempo real y el respaldo.
- **Repositorio:** monorepo privado `mi-diabetes` en GitHub (ver sección 9).

## Leyenda de dónde se ejecuta

- **Nube:** hilos de Claude que escriben código, tests con datos simulados y abren PR. No tocan tus datos reales.
- **PC Windows:** Docker Desktop + WSL2 (PostgreSQL/TimescaleDB, Nightscout, módulos), Ollama nativo con la GPU de 32 GB, Cloudflare Tunnel. Se trabaja en tu equipo cuando hace falta GPU, datos reales o credenciales.

---

## Fase 1 — Base, recolección y panel

**Objetivo.** Al terminar tendrás en el iPhone una PWA, accesible en `app.intelligentcontrol.net`, que muestra tu glucosa actual, la tendencia y el TIR, leyendo de tu propia base de datos con todo tu historial de Nightscout importado.

**Tareas (en orden; ∥ = puede ir en paralelo como hilo):**
1. Crear el monorepo con la estructura y el `CLAUDE.md`. Migrar el prototipo de comidas a `servicios/comidas/` sin cambiarlo.
2. ∥ Esquema de base de datos (glucosa, dosis, comidas, eventos de chat, sugerencias, decisiones, versiones de parámetros) y migraciones. Docker-compose con PostgreSQL + TimescaleDB.
3. ∥ Recolector de Nightscout cada 5 min con token de solo lectura, idempotente, con importación del histórico completo y detección de huecos.
4. ∥ Esqueleto FastAPI (autenticación con passkey, endpoint de glucosa) y PWA mínima con el panel (glucosa, tendencia, TIR de 24 h y 14 días).
5. Copias cifradas con restic y prueba de restauración.
6. Cloudflare Tunnel + Access para `app.` y `ntfy.`.

**Dónde.** 1–4: nube (con Nightscout simulado). 5–6 y despliegue: PC Windows.

**Pruebas y aceptación.**
- El histórico importado coincide con Nightscout: mismo número de lecturas y ninguna diferencia de valor (±0 mg/dL).
- Lectura nueva visible en el panel en menos de 10 min desde que aparece en Nightscout, durante 7 días seguidos.
- Reimportar no duplica nada.
- Restaurar la copia en una base vacía da los mismos recuentos.
- Desde fuera de casa, sin pasar Access no se ve nada; con Access + passkey sí.

**Riesgos.** Exposición de datos de salud (mitigado con Access, passkey y BitLocker). Retraso de LibreLinkUp. Sin dosis aún: el panel no muestra insulina.

**Qué necesito de ti.** Conectar GitHub. Token de solo lectura de Nightscout y su URL interna. Acceso a la cuenta de Cloudflare para el túnel. Confirmar que Docker Desktop y WSL2 están instalados.

---

## Fase 2 — Dosis y cálculos

**Objetivo.** Registras cada dosis de Fiasp en la app (o te la rellena el CSV de LibreView), configuras la Tresiba, y el panel muestra insulina activa y métricas de consenso (TIR, TBR, TAR, GMI, CV).

**Tareas.**
1. Pantalla de registro de dosis (Fiasp) y de configuración de Tresiba (dosis y hora, cambios con fecha).
2. ∥ Botón de hidratos de corrección.
3. ∥ Conciliación continua (cada vez que llegan dosis del script o del CSV): emparejar por hora ±15 min y unidades con lo confirmado en la app, marcar discrepancias para revisar. Importador del CSV para el historial y como respaldo semanal.
4. ∥ Cálculo de insulina activa con la curva de Fiasp y métricas de consenso, con tests contra valores de referencia.
5. ∥ Script propio contra la API no oficial de LibreView (ingeniería inversa; no hay proyecto mantenido que lo haga): descarga las dosis cada pocos minutos, las guarda en la base y las escribe también en Nightscout como tratamientos. Se mide el retraso real y se compara con lo confirmado en la app.
6. Funcionamiento sin conexión de la PWA con cola de envío.

**Dónde.** Todo el código en la nube con datos simulados. Importar tu CSV real y probar la API de LibreView: PC Windows.

**Pruebas y aceptación.**
- Las métricas coinciden con las de Nightscout o LibreView en el mismo periodo (diferencia de TIR < 1 punto, GMI < 0,1 %).
- Insulina activa: error < 0,05 U frente a la curva de referencia en casos de prueba.
- Conciliación: el 100 % de las dosis del CSV quedan emparejadas o marcadas; ninguna se duplica.
- Una dosis registrada sin conexión llega a la base al recuperarla.

**Riesgos (seguridad en dosis).** Bucle de duplicados: el recolector de Nightscout no debe volver a importar los tratamientos que escribe nuestro script (se marcan con un origen propio y se ignoran al leer). Duplicar una dosis (app + CSV) inflaría la insulina activa y haría sugerir de menos; olvidar una la haría sugerir de más. La conciliación debe ser conservadora y visible. La API no oficial puede romperse en cualquier momento. Que el CSV no incluya la pluma o no tenga hora exacta.

**Qué necesito de ti.** Un CSV completo exportado de LibreView para ver sus columnas. Confirmar el modelo de pluma (NovoPen 6 o Echo Plus). Dosis y hora actuales de Tresiba.

---

## Fase 3 — Comidas, chat y primera sugerencia de dosis

**Objetivo.** Haces una foto o escribes en el chat, ves la tabla de nutrientes con alertas, y la app sugiere la dosis de Fiasp con su incertidumbre y, si hay grasa y proteína, una segunda inyección con hora. Marcas "hecho / modificado / ignorado".

**Tareas.**
1. Probar el prototipo con Ollama real y los tres modelos (tiempos, memoria de GPU, calidad).
2. ∥ Integrar el módulo de comidas en la API y la pantalla de comida de la PWA.
3. ∥ Sustituir el bolo extendido por la **segunda inyección con hora** (método de Varsovia).
4. ∥ Registro tipo chat: texto libre → evento estructurado con incertidumbre (sin aviso por alcohol, decisión del 19-11-2026).
5. Cálculo determinista de la sugerencia (hidratos + corrección − insulina activa) con indicador de incertidumbre (según dispersión de hidratos y confianza de los parámetros) y freno por hipoglucemia basado en glucosa actual y tendencia. Hasta que la predicción de la fase 5 esté validada, la corrección se calcula con objetivo 110 como margen de seguridad; después se baja a 90.
6. Registro de la decisión y del resultado a 3–4 h.

**Dónde.** Pruebas con Ollama real: PC Windows (GPU). Código e interfaz: nube, con modelos simulados.

**Pruebas y aceptación.**
- Con 20 comidas pesadas o con etiqueta: error medio de hidratos ≤ 15 g en platos compuestos y ≤ 5 g en simples.
- Tiempo de respuesta de la foto ≤ 90 s con los tres modelos.
- Ninguna sugerencia contiene un número producido por un modelo de lenguaje (test automático que lo comprueba).
- Con glucosa baja o bajando, la sugerencia se bloquea siempre (casos de prueba).
- El chat convierte bien al menos 9 de cada 10 frases de prueba.

**Riesgos (seguridad en dosis).** Parámetros iniciales inventados: el indicador de incertidumbre debe ser muy visible al principio. Objetivo 90 con parámetros inciertos aumenta el riesgo de hipo. Errores grandes de los modelos de visión en platos compuestos. Alcohol y segunda inyección: riesgo de hipo tardía.

**Qué necesito de ti.** Parámetros de partida, aunque sean aproximados (lo que haces "a ojo" para una comida típica). Pesar o etiquetar unas 20 comidas. Rellenar `alimentos_propios.csv` con lo que comes a menudo.

**Estado a 20-11-2026 (PR 15–21).**
- Hecho y desplegado: motor determinista (PR 15), pantalla de sugerencia con decisión y resultado a 4 h (PR 16), hora en curso de la pluma y confirmación «no hay Fiasp sin apuntar desde la última sincronización» antes de cada cálculo (PR 17), comida por foto o texto con revisión de un toque (PR 18) y una ronda por modelo con segunda solo si discrepan, contexto de 8192 tokens (PR 19).
- Fusionado, pendiente de desplegar (Docker Desktop no arranca tras la actualización de Windows del 20-11): chat (PR 20), descripción que guía a la foto, base USDA en un paso, fotos en la copia restic y app en 4 pestañas con la corrección sin comer en Inicio (PR 21).
- Parámetros: versión 1 activa (aprobada el 19-11-2026, confianza baja).
- Medido: la foto tarda unos 10 min porque los modelos están en un disco duro mecánico (unos 3 min de carga cada uno); la generación en la GPU tarda segundos. Se moverán a un SSD nuevo.
- Pendiente para aceptar la fase: 20 comidas pesadas o con etiqueta (error de hidratos), ≤ 90 s por foto (tras el SSD) y la prueba de 20 frases del chat (`herramientas/frases_chat.py`).

---

## Fase 4 — Anomalías y notificaciones

**Objetivo.** El sistema detecta huecos de datos, caídas por presión, subidas sin comida y dosis sin comida, y te pregunta por ntfy. Tus respuestas quedan como etiquetas.

**Tareas.** 1. Servidor ntfy en Docker. 2. ∥ Reglas de detección con umbrales configurables. 3. ∥ Pantalla de respuestas en la PWA. 4. Alerta de acumulación de insulina.
5. Aviso de posible olvido de Tresiba: subida sostenida sin comida ni dosis registrada.

**Dónde.** Código en la nube; ntfy y pruebas reales en el PC.

**Aceptación.** Detecta ≥ 80 % con ≤ 1 falsa alarma diaria de media, sobre al menos 20 casos reales. Desde el 21-11-2026 (decisión de Daniel) se mide en vivo durante unas dos semanas: las respuestas a los avisos dan aciertos y falsas alarmas, y Daniel etiqueta a mano solo lo que se escapa (`python -m servicios.anomalias evaluar --desde 2026-11-21 --en-vivo`). Etiquetar el histórico de meses atrás no es realista.

**Riesgos.** Fatiga de notificaciones. Dependencia de la fase 2 (sin dosis, "dosis sin comida" no funciona).

**Qué necesito de ti.** Instalar la app ntfy en el iPhone. Etiquetar unos cuantos episodios pasados.

**Estado a 20-11-2026 (PR de la fase 4, pendiente de fusionar y desplegar).**
- Hecho en código y tests: servicio `anomalias` con seis reglas (hueco, presión, subida sin comida, Fiasp sin comida, acumulación de insulina y posible olvido de Tresiba), ntfy autoalojado en Docker, pantalla «Avisos y preguntas» con respuestas como etiquetas, etiquetado de episodios pasados y `python -m servicios.anomalias evaluar` para el criterio de aceptación.
- Umbrales aprobados por Daniel el 20-11-2026: acumulación si (insulina activa − hidratos sin absorber / ratio) × sensibilidad > glucosa − 70, con absorción lineal de 3 h (comida) y 1 h (hidratos de corrección), urgente y repetida como mucho cada 60 min; olvido de Tresiba tras 4 h con ≥ 90 % de lecturas > 180, sin bajar y sin comida, hidratos ni Fiasp desde 2 h antes, una vez al día. Las demás son preguntas no urgentes, en silencio de 23:00 a 08:00 y como mucho 6 al día.
- Las anomalías no entran en ningún cálculo de dosis; el freno de la sugerencia no cambia.
- Desplegado el 20-11-2026 y ntfy funcionando en el iPhone el 21-11-2026. PR 25: los episodios ya terminados (más de 90 min) se guardan en la app pero no se notifican.
- Primeras 16 h: 7 avisos respondidos, 1 falsa alarma. Sobre el histórico desde mayo, la regla de presión salta 1,7 veces al día: hay que revisarla.
- Pendiente: dos semanas de medición en vivo (ver Aceptación).

---

## Fase 5 — Predicción y Garmin

**Objetivo.** El panel muestra la glucosa prevista a 30, 60 y 120 min, y la actividad del Garmin entra en los datos.

**Tareas.** 1. ∥ GluPredKit con tu histórico (solo glucosa) y luego con insulina. 2. ∥ Recolector de Garmin (biblioteca no oficial). 3. MLflow local para versiones. 4. Sustituir el freno simple de la fase 3 por la predicción, en modo sombra primero.

**Dónde.** Entrenamiento: PC Windows (tus datos no salen de casa). Código: nube.

**Aceptación.** Error de predicción a 30 min menor que el de "la glucosa sigue igual" en datos no usados para entrenar, sin empeorar en hipos.

**Riesgos.** La biblioteca de Garmin puede romperse. Pocos meses de datos con insulina al principio.

**Qué necesito de ti.** Credenciales de Garmin Connect (se guardan solo en tu PC).

---

## Fase 6 — Digestivo

**Objetivo.** Para cada comida se estima el retraso de vaciado y se aprende qué comidas lo predicen, para sugerir cuándo inyectarse y si dividir la dosis.

**Tareas.** 1. Curva esperada vs real por comida. 2. Modelo de retraso. 3. Sugerencia de hora y división, en modo sombra.

**Dónde.** PC Windows.

**Aceptación.** Error medio del pico ≤ 30 min en comidas posteriores no usadas para ajustar.

**Riesgos.** Retrasar la inyección con un vaciado mal estimado puede producir picos altos o hipos tardías.

**Qué necesito de ti.** Nada nuevo; solo seguir registrando comidas.

---

## Fase 7 — Razonamiento y aprendizaje

**Objetivo.** Ajuste semanal de ratio y sensibilidad tipo autotune como propuesta, explicaciones del modelo local, informe semanal y adopción de versiones por modo sombra.

**Tareas.** 1. Ajuste semanal por franja. 2. Explicación y avisos con el modelo local. 3. Informe semanal. 4. Flujo de aprobación de cambios de parámetros.

**Dónde.** PC Windows.

**Aceptación.** La métrica de la sección 5 del traspaso mejora en datos posteriores sin aumentar el TBR.

**Riesgos.** Que un cambio de parámetros se aplique sin aprobación. Debe ser imposible por diseño.

**Qué necesito de ti.** Aprobar o rechazar cada propuesta semanal.

---

## 9. Estructura del repositorio

```
mi-diabetes/
├── CLAUDE.md            reglas fijas del proyecto
├── api/                 FastAPI
├── pwa/                 interfaz (Svelte o HTMX)
├── comun/               esquema de BD y modelos de datos
├── servicios/
│   ├── almacenamiento/
│   ├── recoleccion/
│   ├── calculos/
│   ├── comidas/         prototipo v0.1 migrado
│   ├── prediccion/
│   ├── anomalias/
│   ├── digestivo/
│   └── razonamiento/
├── despliegue/          docker-compose, túnel, restic
└── docs/                traspaso, decisiones, este plan
```

Reglas de `CLAUDE.md`: ningún número de dosis sale de un modelo de lenguaje; la pluma manda en hora y unidades; ningún módulo habla con otro salvo por la base de datos; nada de secretos ni datos de salud en el repositorio; todo cambio que afecte a la dosis lleva tests y tu aprobación.
