# Mi Diabetes: reglas fijas del proyecto

Sistema personal, local y modular para gestionar la diabetes de Daniel (plumas Fiasp + Tresiba,
FreeStyle Libre → LibreLinkUp → Nightscout autoalojado, pluma inteligente → LibreView, Garmin,
iPhone, mg/dL). Uso personal, no comercial. Responde y documenta en español.

Antes de cambiar nada, lee `docs/Plan_Desarrollo_Mi_Diabetes.md` (manda) y
`docs/Traspaso_Mi_Diabetes.md`.

## Reglas que no se rompen nunca

1. **Ningún número de dosis sale de un modelo de lenguaje.** Las unidades de insulina, horas de
   inyección y correcciones solo salen de cálculos deterministas, trazables y con tests. Un modelo
   de lenguaje puede explicar, resumir, extraer eventos con incertidumbre o avisar de riesgos, pero
   su salida nunca entra en un cálculo de dosis.
2. **La pluma manda en hora y unidades.** Si la app y la pluma (LibreView) discrepan, se usan la
   hora y las unidades de la pluma; la app manda en el vínculo con la comida y en el tipo de
   insulina. Las discrepancias se marcan para revisar, no se resuelven en silencio.
3. **Los módulos solo se comunican por la base de datos.** Ningún servicio llama a otro por red ni
   importa su código. Lo compartido (esquema, ajustes, funciones puras) vive en `comun/`. La API es
   la única entrada desde fuera.
4. **Nada de secretos ni datos de salud en el repositorio.** Credenciales solo en variables de
   entorno (`despliegue/.env`, que no se versiona; la plantilla es `despliegue/.env.example`).
   Lecturas, dosis, comidas, fotos, registros (`*.jsonl`) y copias de seguridad nunca se suben.
   Los tests usan datos sintéticos (`herramientas/sinteticos.py`).
5. **Todo cambio que afecte a la dosis lleva tests y la aprobación explícita de Daniel** antes de
   fusionarse. Eso incluye: insulina activa, sugerencias, conciliación de dosis, parámetros
   (ratio, sensibilidad, objetivo), freno por hipoglucemia, Tresiba y el filtro de origen propio
   del recolector.

## Límites de seguridad vigentes

- Ninguna sugerencia de dosis con hipoglucemia prevista (o glucosa baja o bajando).
- Alerta por acumulación de insulina.
- No hay dosis máxima fija (eliminada el 18-11-2026).
- Objetivo 90 mg/dL; la corrección usa 110 como margen hasta validar la predicción (fase 5).
- Un cambio de parámetros nunca se activa sin aprobación: la tabla `versiones_parametros`
  exige `aprobada_en` para el estado `activa` y solo admite una activa.

## Convenciones técnicas

- Python 3.12, FastAPI, psycopg 3 (SQL explícito, sin ORM), PostgreSQL 16 + TimescaleDB.
- Horas en la base siempre `timestamptz` (UTC); la interfaz muestra `Europe/Madrid`.
- Migraciones: archivos SQL numerados en `comun/migraciones/`, aplicados con
  `python -m comun.migrar`. Nunca se edita una migración ya fusionada: se añade otra.
- Lo que escribamos en Nightscout lleva `enteredBy` empezando por `mi-diabetes`
  (`comun.origen.ORIGEN_PROPIO`) y el recolector lo ignora al leer, para no duplicar.
- Tests: `pytest` contra una TimescaleDB real (`TEST_DATABASE_URL`). Nightscout siempre simulado
  (`herramientas/nightscout_simulado.py`).
- `servicios/comidas/` es el prototipo v0.1 migrado sin cambios. Sus parámetros de insulina en
  `config.py` son de ejemplo y su "bolo extendido" se sustituye por la segunda inyección con hora
  en la fase 3.
- Todo local y gratuito; nada de servicios de pago ni dependencias que llamen a terceros.

## Si cambia el diseño

Indica qué apartado de `docs/Traspaso_Mi_Diabetes.md` o del plan hay que actualizar y con qué texto.
Si una decisión afecta a la seguridad o falta un dato, pregunta a Daniel antes de suponer.
