# Mi Diabetes — documento de traspaso

Estado del proyecto a 18-11-2026.

> **Actualización 18-11-2026:** las decisiones posteriores están en `Plan_Desarrollo_Mi_Diabetes.md`, que manda si hay discrepancia. Cambios clave: sin dosis máxima fija; sugerencias desde el principio con indicador de incertidumbre; objetivo 90 mg/dL; dosis vía script propio contra LibreView más confirmación en la app; registro tipo chat. Este documento resume todo lo decidido en la conversación inicial para continuar el trabajo en el proyecto "Mi Diabetes".

## 1. Objetivo

Sistema personal, local y modular para gestionar la diabetes:
- estimar nutrientes de las comidas por foto o descripción,
- recoger datos de Nightscout,
- calcular, predecir y sugerir bolos (cuánto, cuándo y si dividir),
- detectar anomalías y preguntar,
- analizar las digestiones,
- aprender de forma medible con el tiempo.

Es un sistema de **pruebas personales, no comercial**. No hay que aplicar requisitos regulatorios, pero las sugerencias de dosis siempre requieren confirmación del usuario.
Requisitos generales: todo local, sin suscripciones y con redundancia, comparando varias fuentes y avisando si discrepan.

## 2. Contexto del usuario (relevante para los cálculos)

- **Terapia:** plumas (múltiples dosis). Rápida: **Fiasp**. Basal: **Tresiba**, una vez al día.
- **Pluma inteligente:** sus datos van a **LibreView**. Probablemente es una NovoPen 6 o Echo Plus, pendiente de confirmar.
- **Sensor:** **FreeStyle Libre**. Llega a **Nightscout autoalojado** mediante **LibreLinkUp** (con unos minutos de retraso).
- **Historial en Nightscout:** más de un año de glucosa, **sin datos de insulina**.
- **Reloj:** **Garmin**.
- **Móvil:** **iPhone**.
- **Unidades:** mg/dL.
- **Digestivo:** gastritis atrófica crónica. No tiene diagnóstico de gastroparesia, pero nota digestiones lentas. **No quiere registrar síntomas**: todo se deduce del sensor.

## 3. Infraestructura

- **Servidor:** Windows, con GPU de **32 GB** y **192 GB de RAM**. Cifrado de disco con BitLocker.
- **Modelos:** Ollama nativo en Windows.
- **Servicios:** Docker Desktop con WSL2 para PostgreSQL + TimescaleDB, Nightscout y los módulos.
- **Acceso exterior:** **Cloudflare Tunnel + Cloudflare Access** (plan gratuito) en el dominio **intelligentcontrol.net**.
  - `app.intelligentcontrol.net`: la app, detrás de Access, más login propio con passkey.
  - `ntfy.intelligentcontrol.net`: notificaciones, que Access deja pasar sin identificación y que protege el token de ntfy.
  - Nota: Cloudflare descifra el tráfico en su red. Más adelante se podrían cifrar en la app los campos sensibles.

## 4. Arquitectura (9 módulos)

Ningún módulo se comunica directamente con otro: todos leen y escriben en la base de datos central. La API es la única entrada desde fuera.

1. **Almacenamiento seguro:** PostgreSQL + TimescaleDB y copias cifradas (restic). Guarda glucosa, dosis, comidas (con foto y estimaciones), sugerencias, decisiones y resultados.
2. **Recolección:** Nightscout cada 5 minutos con token de solo lectura, más Garmin (biblioteca no oficial de Garmin Connect) y las dosis (ver decisiones abiertas).
3. **Cálculos:**
   - Métricas de consenso: tiempo en rango (TIR, TBR, TAR), GMI y coeficiente de variación.
   - Insulina activa con la curva de Fiasp.
   - Basal de Tresiba tratada como casi constante (cuentan la hora y los cambios de dosis).
   - Reajuste semanal de ratio y sensibilidad al estilo autotune, siempre como propuesta.
4. **Comidas:** foto o descripción → 3 modelos de visión × 3 repeticiones, por dos vías: base de alimentos y estimación directa del modelo. Resultado por consenso (mediana), con alertas de discrepancia y revisión por el usuario. **Ya existe un prototipo v0.1** (sección 7).
5. **Predicción:** GluPredKit (incluye lector para Nightscout) más modelos fisiológicos y de aprendizaje automático. Al principio solo con glucosa; se añade la insulina cuando haya dosis registradas.
6. **Anomalías:** reglas y estadística (huecos de datos, caídas por presión sobre el sensor, subidas sin comida, dosis sin comida). Pregunta por ntfy y usa las respuestas como etiquetas.
7. **Digestivo:** compara la curva real después de cada comida con la esperada para estimar un "retraso de vaciado". Aprende qué comidas lo predicen y lo usa para sugerir cuándo inyectarse y si dividir la dosis.
8. **Razonamiento:**
   - Los números salen de los cálculos deterministas; un modelo de lenguaje local solo explica y avisa de riesgos.
   - Como son plumas, la parte de grasa y proteína (método de Varsovia, UGP) se sugiere como **segunda inyección con hora**, no como bolo extendido.
   - Límites fijos: ninguna dosis con hipoglucemia prevista y alerta por acumulación de insulina (la dosis máxima fija se eliminó).
9. **Interfaz móvil:** app web instalable (PWA) en el iPhone. Servidor FastAPI e interfaz ligera (Svelte o HTMX). Pantallas:
   - comida (foto o texto, tabla editable, alertas y dosis sugerida),
   - panel (glucosa, tendencia, predicción, insulina activa, TIR),
   - sugerencias con "hecho / modificado / ignorado",
   - respuestas a las preguntas de anomalías.

   Funciona sin conexión y envía los datos al recuperarla.

## 5. Definición de "aprender"

El sistema aprende cuando cambia un parámetro o un modelo y ese cambio reduce, en datos posteriores no usados para ajustarlo, una métrica de error fijada de antemano, sin aumentar las hipoglucemias. Guardar datos sin comprobar mejoras no cuenta como aprender.

| Módulo | Qué cambia | Referencia | Métrica |
|---|---|---|---|
| Comidas | Pesos de los modelos, tabla de platos propios, reentrenamiento futuro | Hidratos de comidas pesadas o con etiqueta | Error medio en gramos |
| Parámetros | Ratio, sensibilidad y basal por franja | Glucosa 3–4 h después | Distancia al objetivo, TIR posprandial |
| Predicción | Modelos de glucosa | Glucosa real a 30, 60 y 120 min | Error de predicción con más peso en hipoglucemias |
| Digestivo | Modelo de retraso de vaciado | Retraso observado en la curva | Error en minutos del pico |
| Anomalías | Umbrales y clasificador | Respuestas del usuario | Falsas alarmas frente a detecciones |
| Razonamiento | Reglas y casos de ejemplo | Resultado de seguir o ignorar una sugerencia | % de comidas en rango e hipoglucemias tras dosis |

Cómo se adoptan los cambios:
- **Modo sombra:** cada versión nueva funciona en paralelo unas semanas antes de sustituir a la actual.
- **Versiones** registradas en MLflow (local), con posibilidad de volver atrás.
- **Aprobación:** el usuario aprueba todo cambio que afecte a la dosis.
- **Informe semanal** en el móvil.
- Los periodos con ejercicio, enfermedad o fallos del sensor se etiquetan y se excluyen o se tratan aparte.

## 6. Decisiones abiertas

1. **Cómo llegan las dosis al sistema (crítico).** Las dosis de la pluma están en LibreView y **no llegan a Nightscout**: nightscout-librelink-up solo sube la glucosa, y nightscout-connect todavía no importa tratamientos. El usuario creía que sí llegaban; el historial sin insulina lo confirma. Propuesta pendiente de aceptar:
   - Confirmación de cada dosis en la app nueva, más CSV semanal de LibreView para rellenar olvidos.
   - Prueba en paralelo de la API no oficial de LibreView.
   - Un CSV del historial completo de LibreView para rellenar dosis pasadas (comprobar qué incluye).
2. Confirmar el modelo de pluma.
3. Probar el prototipo del módulo de comidas con Ollama y los modelos reales.

## 7. Estado del código

`hidratos-por-foto.zip` (prototipo del módulo de comidas, v0.1):
- `config.py`: modelos (ya ajustados a 32 GB: `qwen3-vl:32b`, `gemma3:27b`, `minicpm-v`), umbrales de alerta, parámetros de insulina y `RESTAR_FIBRA`.
- `preparar_bd.py`: convierte la base USDA FoodData Central (SR Legacy, CSV) en SQLite local.
- `alimentos_propios.csv`: alimentos propios por 100 g, con hidratos como en la etiqueta europea, sin fibra. Las filas actuales son solo ejemplos.
- `nutricion.py`: núcleo y línea de comandos (`--foto`, `--texto`, `--glucosa`, `--activa`), con consenso, alertas, cálculo de bolo con UGP y registro en `registro.jsonl`.
- `app.py`: interfaz Gradio provisional, con tabla editable, recálculo y guardado.

Estado: lógica probada con modelos simulados; **pendiente de probar con Ollama real**. El bolo de grasa y proteína todavía está pensado como "extendido": hay que adaptarlo a una **segunda inyección** para plumas.

## 8. Plan por fases (construcción incremental)

1. Base de datos + recolección desde Nightscout + esqueleto de FastAPI y de la PWA con el panel de glucosa + túnel de Cloudflare.
2. Vía de las dosis (app + CSV de LibreView) y cálculos (insulina activa, métricas).
3. Integrar el módulo de comidas en la app y adaptar la segunda inyección.
4. Anomalías con notificaciones ntfy.
5. Predicción (GluPredKit) y Garmin.
6. Digestivo.
7. Razonamiento y aprendizaje (ajuste semanal en modo sombra). Las sugerencias de dosis básicas se activan ya en la fase 3, con indicador de incertidumbre.

## 9. Referencias clave

- Estimación de hidratos con modelos de visión: error de unos 3–5 g en alimentos simples y de unos 14 g en platos compuestos (ChatGPT-4o, J Diabetes Sci Technol). En un estudio de 2026 con más de 26.000 consultas, los modelos variaban mucho entre repeticiones de la misma foto y su "confianza" no predecía el acierto.
- Darle al modelo el peso real reduce mucho el error (benchmark con Nutrition5k).
- Datasets: Nutrition5k, NutriBench, MM-Food-100K y NutriConv.
- Bases de alimentos: USDA FoodData Central, BEDCA y Open Food Facts.
- Método de Varsovia: 1 UGP = 100 kcal de grasa o proteína, equivalente a unos 10 g de hidratos.
- Vaciado gástrico: es un factor clave de la glucosa posprandial en la diabetes tipo 1; en un estudio, alrededor del 50% de las personas con tipo 1 tenía vaciado retrasado sin correlación con los síntomas.
- Herramientas: GluPredKit, OpenAPS autotune, nightscout-librelink-up, MLflow y ntfy.

## 10. Cómo trabajar en el proyecto

- Construcción incremental, módulo a módulo.
- Código en Python y despliegue en Docker sobre Windows.
- Ningún número de dosis puede salir de un modelo de lenguaje: solo de los cálculos deterministas.
- Siguiente paso propuesto: cerrar la decisión 1 (dosis) y empezar la fase 1.
