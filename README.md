# Mi Diabetes

Sistema personal y local para gestionar la diabetes: comidas por foto, recolección de
Nightscout, predicción y sugerencia de dosis. Uso personal, no comercial.

- Reglas fijas: [`CLAUDE.md`](CLAUDE.md)
- Plan por fases (manda): [`docs/Plan_Desarrollo_Mi_Diabetes.md`](docs/Plan_Desarrollo_Mi_Diabetes.md)
- Traspaso y arquitectura: [`docs/Traspaso_Mi_Diabetes.md`](docs/Traspaso_Mi_Diabetes.md)
- Despliegue en Windows: [`despliegue/README_despliegue_windows.md`](despliegue/README_despliegue_windows.md)

## Estructura

```
CLAUDE.md            reglas fijas del proyecto
api/                 FastAPI: passkey, glucosa, panel (sirve la PWA)
pwa/                 interfaz instalable para el iPhone (sin dependencias)
comun/               ajustes, conexión, migraciones SQL, consultas compartidas
servicios/
  recoleccion/       Nightscout → base (cada 5 min, histórico, huecos)
  comidas/           prototipo v0.1 migrado sin cambios
  almacenamiento/ calculos/ prediccion/ anomalias/ digestivo/ razonamiento/   (fases siguientes)
herramientas/        Nightscout simulado y datos sintéticos
despliegue/          docker-compose, .env.example, túnel, restic
docs/                plan y traspaso
tests/               pytest contra TimescaleDB real
```

## Estado: fase 1

| Pieza | Estado |
|---|---|
| Esquema (glucosa, dosis, comidas, chat, hidratos de corrección, Tresiba, sugerencias, decisiones, parámetros) | ✔ |
| Recolector de Nightscout (incremental, histórico, huecos, verificación, filtro de origen propio) | ✔ |
| API con passkey y panel PWA (glucosa, tendencia, TIR 24 h y 14 días) | ✔ |
| Copias restic y túnel de Cloudflare | plantillas listas; se ejecutan en el PC |

## Desarrollo

```bash
pip install -r requirements-pruebas.txt
docker run -d --name tsdb -e POSTGRES_PASSWORD=prueba -p 5433:5432 timescale/timescaledb:2.17.2-pg16
export TEST_DATABASE_URL=postgresql://postgres:prueba@localhost:5433/postgres
pytest
```
