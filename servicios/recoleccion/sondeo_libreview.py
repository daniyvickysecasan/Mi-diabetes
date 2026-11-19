"""Una vuelta del recolector de la pluma: atiende el SMS que pide la app y lee el informe de LibreView.

Cada vuelta mira el estado en la base (comun.estado_libreview):
  - sms_pedido: pide a LibreView que mande el SMS y pasa a esperando_codigo.
  - codigo_recibido: manda el código, lo borra de la base y, si entra, lee las dosis.
  - necesita_codigo / esperando_codigo: no hace nada (espera a Daniel; no reintenta el login).
  - el resto: si toca, lee el informe "Registro diario" y guarda las dosis.
"""
import logging
from datetime import datetime, timedelta, timezone

from comun import estado_libreview as est
from servicios.recoleccion import pluma
from servicios.recoleccion.libreview import ErrorLibreView, NecesitaCodigo, dosis_del_informe

log = logging.getLogger("pluma")


class FuenteInforme:
    """Adapta el informe de LibreView a la interfaz de pluma.importar (dosis + horas ambiguas)."""

    def __init__(self, cliente, dias: int = 14):
        self.cliente, self.dias = cliente, dias
        self._horas: list[dict] = []
        self.serie_pluma = None
        self.sincronizacion_pluma = None
        self.sin_unidades = 0

    def dosis(self, desde: datetime, hasta: datetime):
        inf = self.cliente.informe_registro_diario(dias=self.dias)
        self.serie_pluma, self.sincronizacion_pluma = inf.serie_pluma, inf.sincronizacion_pluma
        lectura = dosis_del_informe(inf.datos, self.cliente.zona, inf.serie_pluma)
        self.sin_unidades = lectura.sin_unidades
        self._horas = [h for h in lectura.horas_ambiguas if desde <= h["hora"] < hasta]
        return [d for d in lectura.dosis if desde <= d.ts < hasta]

    def horas_ambiguas(self) -> list[dict]:
        return self._horas


def _sincronizar(con, cliente, modo: str, dias: int, escritor, ahora: datetime) -> dict:
    fuente = FuenteInforme(cliente, dias)
    desde = ahora - timedelta(days=dias)
    r = pluma.importar(con, fuente, desde, ahora + timedelta(minutes=5), modo, escritor=escritor, ahora=ahora)
    est.poner(con, "ok", error=None, ultimo_ok=ahora, ultima_sincronizacion=fuente.sincronizacion_pluma)
    if fuente.sin_unidades:
        log.warning("%s horas con inyección pero sin unidades en el informe", fuente.sin_unidades)
    return r


def vuelta(con, cliente, escritor=None, sincronizar: bool = True, dias: int = 14, modo: str = "incremental",
           ahora: datetime | None = None) -> dict:
    """Una vuelta. `con` en autocommit. Nunca lanza por fallos de LibreView: los deja en el estado."""
    ahora = ahora or datetime.now(timezone.utc)
    e = est.leer(con)
    estado = e["estado"]
    try:
        if estado == "sms_pedido":
            destino = cliente.pedir_sms()
            est.poner(con, "esperando_codigo", destino_sms=destino, error=None)
            log.info("SMS de LibreView pedido")
            return {"accion": "sms_enviado"}
        if estado == "codigo_recibido":
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
            sincronizar = True
        elif estado in ("necesita_codigo", "esperando_codigo"):
            return {"accion": "esperando_a_daniel"}
        if not sincronizar:
            return {"accion": "nada"}
        return {"accion": "sincronizado", **_sincronizar(con, cliente, modo, dias, escritor, ahora)}
    except NecesitaCodigo:
        est.poner(con, "necesita_codigo", error=None)
        log.warning("LibreView pide el código del SMS: aviso en la app")
        return {"accion": "necesita_codigo"}
    except ErrorLibreView as x:
        est.poner(con, "error", error=str(x)[:300])
        log.error("LibreView: %s", x)
        return {"accion": "error", "error": str(x)}
