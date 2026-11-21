# anomalias

Módulo 6 (fase 4). Detecta episodios con reglas deterministas, los guarda en `anomalias` y avisa
por el ntfy de casa. Daniel responde en la app (Más → Avisos y preguntas) y la respuesta queda como
etiqueta. Se comunica con el resto solo por la base. **Nada de esto entra en un cálculo de dosis.**

| Tipo | Cuándo | Aviso |
|---|---|---|
| `hueco` | Sin lecturas más de 30 min | normal |
| `presion` | Bajada ≥ 30 mg/dL a ≥ 2 mg/dL/min y rebote del 70 % en ≤ 60 min, sin hidratos | normal, a posteriori |
| `subida_sin_comida` | +60 mg/dL en ≤ 2 h hasta ≥ 160, sin comida ni hidratos desde 3 h antes | normal |
| `dosis_sin_comida` | Fiasp sin comida de −3 h a +1 h ni sugerencia; se pregunta a la hora | normal |
| `acumulacion` | (insulina activa − hidratos sin absorber / ratio) × sensibilidad > glucosa − 70 | **urgente**, también de noche; repite cada 60 min |
| `olvido_tresiba` | 4 h con ≥ 90 % de lecturas > 180, sin bajar, sin comida, hidratos ni Fiasp desde 2 h antes | normal, 1 al día; solo pregunta |

Las no urgentes esperan de 23:00 a 08:00 y son como mucho 6 al día. Umbrales en `reglas.py`
(`Umbrales`), cambiables con `ANOMALIA_<NOMBRE>` en el `.env`. Aprobados por Daniel el 20-11-2026.

```
python -m servicios.anomalias servicio        # contenedor `anomalias`
python -m servicios.anomalias ciclo
python -m servicios.anomalias probar-aviso
python -m servicios.anomalias evaluar --desde 2026-11-21 --en-vivo   # criterio: ≥ 80 % y ≤ 1 falsa/día, ≥ 20 casos
python -m servicios.anomalias evaluar --desde 2026-05-01             # reglas sobre el histórico
python -m servicios.anomalias umbrales
```

- `reglas.py`: reglas puras y textos. `detector.py`: lee la base y guarda. `avisos.py`: ntfy, silencio y
  límite diario. `evaluar.py`: compara con `episodios_etiquetados` (se rellenan en la app).
- Tests: `tests/test_reglas_anomalias.py` (puros) y `tests/test_anomalias.py` (base real y
  `herramientas/ntfy_simulado.py`).
- Despliegue de ntfy: `despliegue/ntfy/README.md`.
