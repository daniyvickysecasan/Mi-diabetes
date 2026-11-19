"""Una vuelta del recolector de la pluma: atiende el SMS que pide la app y las lecturas pedidas.

LibreView solo se lee cuando alguien lo pide (tabla solicitudes_lectura_pluma): el botón de la app o,
en la fase 3, el cálculo de una dosis. Cada vuelta (cada INTERVALO_PLUMA_S, unos segundos) mira la base:
  - sms_pedido: pide a LibreView que mande el SMS y pasa a esperando_codigo.
  - codigo_recibido: manda el código, lo borra de la base y, si entra, lee las dosis.
  - necesita_codigo / esperando_codigo: cierra las lecturas pedidas como sin_sesion (no hace esperar
    al cálculo) y espera a Daniel; no reintenta el login.
  - si hay lecturas pedidas: lee el informe "Registro diario", guarda, concilia y las cierra.

Las dosis ya guardadas no se tocan nunca. Si una lectura posterior las contradice (otro total en su
hora, otra inyección en la misma hora o ya no aparecen, p. ej. un cebado marcado después), solo se
anota en cambios_pluma_libreview para revisarlo.
"""
import logging
from datetime import datetime, timedelta, timezone

from psycopg.types.json import Jsonb

from comun import estado_libreview as est
from servicios.recoleccion import pluma
from servicios.recoleccion.libreview import YA_DENTRO, ErrorLibreView, Lectura, NecesitaCodigo, dosis_del_informe

log = logging.getLogger("pluma")


class FuenteInforme:
    """Adapta el informe de LibreView a la interfaz de pluma.importar (dosis + horas ambiguas)."""

    def __init__(self, cliente, dias: int = 14, ahora: datetime | None = None):
        self.cliente, self.dias, self.ahora = cliente, dias, ahora
        self.lectura: Lectura | None = None
        self._horas: list[dict] = []
        self.serie_pluma = None
        self.sincronizacion_pluma = None

    def dosis(self, desde: datetime, hasta: datetime):
        inf = self.cliente.informe_registro_diario(dias=self.dias)
        self.serie_pluma, self.sincronizacion_pluma = inf.serie_pluma, inf.sincronizacion_pluma
        self.lectura = dosis_del_informe(inf.datos, self.cliente.zona, inf.serie_pluma, ahora=self.ahora)
        self._horas = [h for h in self.lectura.horas_ambiguas if desde <= h["hora"] < hasta]
        return [d for d in self.lectura.dosis if desde <= d.ts < hasta]

    def horas_ambiguas(self) -> list[dict]:
        return self._horas


def anotar_cambios(con, lectura: Lectura, zona) -> int:
    """Compara las dosis del informe ya guardadas con la lectura nueva. No cambia ninguna dosis."""
    if not lectura.desde:
        return 0
    n = 0
    filas = con.execute(
        """SELECT d.id, d.ts, d.unidades FROM dosis d JOIN llegadas_pluma l ON l.dosis_id = d.id
           WHERE d.origen = %s AND l.bruto->>'fuente' = 'registro_diario' AND d.ts >= %s AND d.ts < %s""",
        (pluma.ORIGEN_DOSIS, lectura.desde, lectura.hasta)).fetchall()
    for f in filas:
        local = f["ts"].astimezone(zona).replace(minute=0, second=0, microsecond=0)
        hora = local.astimezone(timezone.utc)
        h = lectura.horas.get(hora)
        if h is None or f["ts"] not in h["inyecciones"]:
            motivo, detalle = "desaparecida", {}
        elif len(h["inyecciones"]) > 1:
            motivo, detalle = "otra_inyeccion", {"inyecciones": [t.isoformat() for t in h["inyecciones"]],
                                                 "unidades_hora": str(h["unidades"])}
        elif h["unidades"] != f["unidades"]:
            motivo, detalle = "total_distinto", {"unidades_hora": str(h["unidades"])}
        else:
            continue
        nueva = con.execute(
            """INSERT INTO cambios_pluma_libreview (dosis_id, motivo, detalle) VALUES (%s, %s, %s)
               ON CONFLICT (dosis_id, motivo) DO UPDATE SET detalle = EXCLUDED.detalle
               RETURNING (xmax = 0) AS nueva""", (f["id"], motivo, Jsonb(detalle))).fetchone()["nueva"]
        if nueva:
            n += 1
            log.warning("Dosis %s de la pluma contradicha por LibreView (%s): queda para revisar", f["id"], motivo)
    return n


def _sincronizar(con, cliente, modo: str, dias: int, escritor, ahora: datetime) -> dict:
    fuente = FuenteInforme(cliente, dias, ahora)
    desde = ahora - timedelta(days=dias)
    r = pluma.importar(con, fuente, desde, ahora + timedelta(minutes=5), modo, escritor=escritor, ahora=ahora)
    r["cambios"] = anotar_cambios(con, fuente.lectura, cliente.zona) if fuente.lectura else 0
    est.poner(con, "ok", error=None, ultimo_ok=ahora, ultima_sincronizacion=fuente.sincronizacion_pluma)
    est.cerrar_solicitudes(con, "ok")
    if fuente.lectura and fuente.lectura.sin_unidades:
        log.warning("%s horas con inyección pero sin unidades en el informe", fuente.lectura.sin_unidades)
    for a in (fuente.lectura.avisos_hora if fuente.lectura else []):
        log.warning("Cambio de hora: %s", a)
    return r


def vuelta(con, cliente, escritor=None, sincronizar: bool | None = None, dias: int = 14,
           modo: str = "incremental", ahora: datetime | None = None) -> dict:
    """Una vuelta. `con` en autocommit. Nunca lanza por fallos de LibreView: los deja en el estado.
    `sincronizar`: None = solo si hay lecturas pedidas; True = leer ya (línea de comandos)."""
    ahora = ahora or datetime.now(timezone.utc)
    e = est.leer(con)
    estado = e["estado"]
    if sincronizar is None:
        sincronizar = est.hay_solicitudes(con)
    try:
        if estado == "sms_pedido":
            try:
                destino = cliente.pedir_sms()
            except ErrorLibreView as x:      # se deja el botón para reintentar, con el motivo
                est.poner(con, "necesita_codigo", error=str(x)[:300])
                log.error("LibreView no mandó el SMS: %s", x)
                return {"accion": "sms_fallido", "error": str(x)}
            if destino == YA_DENTRO:
                est.poner(con, "ok", error=None, ultimo_ok=ahora)
                log.info("LibreView deja entrar sin SMS")
                sincronizar = True
            else:
                est.poner(con, "esperando_codigo", destino_sms=destino, error=None)
                log.info("SMS de LibreView pedido")
                return {"accion": "sms_enviado"}
        elif estado == "codigo_recibido":
            codigo = e["codigo"]
            est.poner(con, "codigo_recibido", codigo=None)          # el código no se queda en la base
            try:
                cliente.enviar_codigo(codigo)
            except ErrorLibreView as x:
                est.poner(con, "necesita_codigo", error=str(x)[:300])
                log.warning("Código de LibreView rechazado: %s", x)
                return {"accion": "codigo_rechazado"}
            est.poner(con, "ok", error=None, ultimo_ok=ahora)
            log.info("LibreView: dentro con el código del SMS")
            sincronizar = True                                      # recién entrado: se leen las dosis
        elif estado in ("necesita_codigo", "esperando_codigo"):
            est.cerrar_solicitudes(con, "sin_sesion", "LibreView pide el código del SMS")
            return {"accion": "esperando_a_daniel"}
        if not sincronizar:
            return {"accion": "nada"}
        return {"accion": "sincronizado", **_sincronizar(con, cliente, modo, dias, escritor, ahora)}
    except NecesitaCodigo:
        est.poner(con, "necesita_codigo", error=None)
        est.cerrar_solicitudes(con, "sin_sesion", "LibreView pide el código del SMS")
        log.warning("LibreView pide el código del SMS: aviso en la app")
        return {"accion": "necesita_codigo"}
    except ErrorLibreView as x:
        est.poner(con, "error", error=str(x)[:300])
        est.cerrar_solicitudes(con, "error", str(x))
        log.error("LibreView: %s", x)
        return {"accion": "error", "error": str(x)}
