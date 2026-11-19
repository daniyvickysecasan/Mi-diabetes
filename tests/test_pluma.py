"""Recolector de la pluma: guardado idempotente, cebados, envío a Nightscout y retraso.

LibreView siempre simulado (FuenteFalsa; la web simulada está en test_libreview_web.py); Nightscout con el simulador de herramientas/.
"""
from datetime import timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from comun.origen import es_origen_propio
from herramientas.nightscout_simulado import crear_app as crear_ns
from servicios.recoleccion import pluma
from servicios.recoleccion.libreview import ErrorLibreView
from servicios.recoleccion.nightscout import ClienteNightscout, EscritorNightscout
from servicios.recoleccion.recolector import guardar_tratamientos
from tests.conftest import TOKEN

TOKEN_ESCRITURA = "token-careportal-prueba"
MADRID = ZoneInfo("Europe/Madrid")


class FuenteFalsa:
    def __init__(self, dosis):
        self.lista = list(dosis)
        self.llamadas = 0

    def dosis(self, desde, hasta):
        self.llamadas += 1
        return [d for d in self.lista if desde <= d.ts < hasta]


@pytest.fixture
def ns_escritura():
    app = crear_ns([], [], token=TOKEN, token_escritura=TOKEN_ESCRITURA)
    http = TestClient(app, base_url="http://nightscout.test")
    return app, EscritorNightscout("http://nightscout.test", TOKEN_ESCRITURA, cliente=http), \
        ClienteNightscout("http://nightscout.test", TOKEN, cliente=http)


def _d(ts, u, **kw):
    return pluma.DosisPluma(ts=ts, unidades=Decimal(str(u)), serie_pluma="SERIE-PRUEBA", **kw)


def test_guarda_dosis_como_fiasp_de_la_pluma(con, ahora):
    f = FuenteFalsa([_d(ahora - timedelta(hours=2), 4.5), _d(ahora - timedelta(hours=1), 2)])
    r = pluma.ciclo(con, f, solape_h=48, ahora=ahora)
    assert r["dosis"]["insertadas"] == 2
    filas = con.execute("SELECT * FROM dosis ORDER BY ts").fetchall()
    assert [f["unidades"] for f in filas] == [Decimal("4.50"), Decimal("2.00")]
    assert {f["origen"] for f in filas} == {"pluma_libreview"}
    assert {f["insulina"] for f in filas} == {"fiasp"}
    # El estado lo decide la conciliación (sin dosis en la app: solo_pluma), nunca este módulo.
    assert {f["estado_conciliacion"] for f in filas} <= {"pendiente", "solo_pluma"}
    assert all(f["dosis_par_id"] is None and f["comida_id"] is None for f in filas)


def test_releer_no_duplica(con, ahora):
    f = FuenteFalsa([_d(ahora - timedelta(hours=2), 4.5)])
    pluma.ciclo(con, f, 48, ahora=ahora)
    r = pluma.ciclo(con, f, 48, ahora=ahora + timedelta(minutes=5))
    assert r["dosis"]["insertadas"] == 0 and r["dosis"]["ya_estaban"] == 1
    assert con.execute("SELECT count(*) AS n FROM dosis").fetchone()["n"] == 1
    # La hora de llegada es la de la primera vez que se vio.
    assert con.execute("SELECT vista_en FROM llegadas_pluma").fetchone()["vista_en"] == ahora


def test_id_externo_estable_y_distinto():
    t = pluma.ahora_utc().replace(microsecond=0)
    a, b = _d(t, 3), _d(t.astimezone(MADRID), 3)       # misma hora en otra zona: misma dosis
    assert a.id_externo == b.id_externo
    assert _d(t, 3.5).id_externo != a.id_externo
    assert _d(t + timedelta(minutes=1), 3).id_externo != a.id_externo
    assert _d(t, 3, id_libreview="123").id_externo == "lv:123"


def test_cebado_se_guarda_con_nota_y_no_va_a_nightscout(con, ahora, ns_escritura):
    app, escritor, _ = ns_escritura
    f = FuenteFalsa([_d(ahora - timedelta(minutes=30), 2, cebado=True), _d(ahora - timedelta(minutes=29), 6)])
    r = pluma.ciclo(con, f, 48, escritor=escritor, ahora=ahora)
    assert r["dosis"]["insertadas"] == 2
    ceb = con.execute("SELECT d.notas, l.cebado FROM dosis d JOIN llegadas_pluma l ON l.dosis_id=d.id "
                      "WHERE d.unidades = 2").fetchone()
    assert ceb["notas"] == "cebado" and ceb["cebado"] is True
    assert r["nightscout"]["enviadas"] == 1
    assert [t["insulin"] for t in app.state.datos["tratamientos"]] == [6.0]


def test_invalidas_se_ignoran(con, ahora):
    f = FuenteFalsa([_d(ahora - timedelta(hours=1), 0), _d(ahora - timedelta(hours=1), -1)])
    r = pluma.ciclo(con, f, 48, ahora=ahora)
    assert r["dosis"]["invalidas"] == 2 and r["dosis"]["insertadas"] == 0


def test_escribe_en_nightscout_con_origen_propio_y_el_recolector_lo_ignora(con, ahora, ns_escritura):
    app, escritor, lector = ns_escritura
    f = FuenteFalsa([_d(ahora - timedelta(hours=3), 5)])
    r = pluma.ciclo(con, f, 48, escritor=escritor, ahora=ahora)
    assert r["nightscout"] == {"enviadas": 1, "errores": 0}
    t = app.state.datos["tratamientos"][0]
    assert t["enteredBy"] == "mi-diabetes/libreview" and es_origen_propio(t)
    assert t["created_at"] == (ahora - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    env = con.execute("SELECT * FROM envios_nightscout").fetchone()
    assert env["estado"] == "enviado" and env["nightscout_id"] == t["_id"]
    # El recolector de Nightscout lee ese tratamiento y lo descarta: ni copia ni dosis nueva.
    leidos = lector.tratamientos(ahora - timedelta(days=1), ahora + timedelta(minutes=1))
    g = guardar_tratamientos(con, leidos)
    assert g["propios_ignorados"] == 1 and g["insertados"] == 0
    assert con.execute("SELECT count(*) AS n FROM dosis").fetchone()["n"] == 1


def test_no_reenvia_lo_ya_enviado(con, ahora, ns_escritura):
    app, escritor, _ = ns_escritura
    f = FuenteFalsa([_d(ahora - timedelta(hours=3), 5)])
    pluma.ciclo(con, f, 48, escritor=escritor, ahora=ahora)
    r = pluma.ciclo(con, f, 48, escritor=escritor, ahora=ahora + timedelta(minutes=5))
    assert r["nightscout"]["enviadas"] == 0
    assert len(app.state.datos["tratamientos"]) == 1


def test_fallo_de_nightscout_reintenta_en_el_siguiente_ciclo(con, ahora):
    app = crear_ns([], [], token=TOKEN, token_escritura=TOKEN_ESCRITURA)
    http = TestClient(app, base_url="http://nightscout.test")
    malo = EscritorNightscout("http://nightscout.test", "token-equivocado", cliente=http)
    f = FuenteFalsa([_d(ahora - timedelta(hours=1), 3)])
    r = pluma.ciclo(con, f, 48, escritor=malo, ahora=ahora)
    assert r["nightscout"]["errores"] == 1
    e = con.execute("SELECT estado, error FROM envios_nightscout").fetchone()
    assert e["estado"] == "error" and "careportal" in e["error"] and "token-equivocado" not in e["error"]
    bueno = EscritorNightscout("http://nightscout.test", TOKEN_ESCRITURA, cliente=http)
    r = pluma.ciclo(con, f, 48, escritor=bueno, ahora=ahora + timedelta(minutes=5))
    assert r["nightscout"]["enviadas"] == 1


def test_reenvio_tras_corte_no_duplica_en_nightscout(con, ahora, ns_escritura):
    """Si Nightscout guardó la dosis pero la respuesta se perdió, el reenvío hace upsert."""
    app, escritor, _ = ns_escritura
    f = FuenteFalsa([_d(ahora - timedelta(hours=1), 3)])
    pluma.ciclo(con, f, 48, escritor=escritor, ahora=ahora)
    con.execute("UPDATE envios_nightscout SET estado='error'")
    pluma.enviar_pendientes(con, escritor)
    assert len(app.state.datos["tratamientos"]) == 1


def test_sin_token_de_escritura_solo_guarda(con, ahora):
    r = pluma.ciclo(con, FuenteFalsa([_d(ahora - timedelta(hours=1), 3)]), 48, escritor=None, ahora=ahora)
    assert r["nightscout"].get("desactivado") is True
    assert con.execute("SELECT estado FROM envios_nightscout").fetchone()["estado"] == "pendiente"


def test_fallo_de_libreview_queda_registrado(con, ahora):
    class Rota:
        def dosis(self, desde, hasta):
            raise ErrorLibreView("caído")
    with pytest.raises(ErrorLibreView):
        pluma.ciclo(con, Rota(), 48, ahora=ahora)
    e = con.execute("SELECT fuente, error FROM ejecuciones_recoleccion").fetchone()
    assert e["fuente"] == "libreview" and "caído" in e["error"]


def test_retraso_y_comparacion_con_la_app(con, ahora):
    # Dosis inyectada hace 60 min, vista ahora: retraso 60. En la app se confirmó a los 2 min.
    # La conciliación real (comun.conciliacion) es la que las empareja.
    t = ahora - timedelta(minutes=60)
    con.execute("""INSERT INTO dosis (ts, unidades, insulina, origen, creado_en)
                   VALUES (%s, 4, 'fiasp', 'app', %s)""", (t, t + timedelta(minutes=2)))
    r = pluma.ciclo(con, FuenteFalsa([_d(t, 4)]), 48, ahora=ahora)
    assert r["conciliacion"] is not None
    r = pluma.informe_retraso(con, 7, ahora=ahora)
    assert r["n"] == 1 and r["mediana_min"] == 60.0 and r["con_app"] == 1
    assert r["mediana_app_min"] == 2.0 and r["app_antes"] == 1


def test_historico_no_cuenta_en_el_retraso(con, ahora):
    f = FuenteFalsa([_d(ahora - timedelta(days=20), 4)])
    pluma.importar(con, f, ahora - timedelta(days=30), ahora, "historico", ahora=ahora)
    assert pluma.informe_retraso(con, 60, ahora=ahora)["n"] == 0


# ------------------------------------------------------------------ cliente de LibreView
def test_serie_y_cebado_descartado_por_la_conciliacion(con, ahora):
    f = FuenteFalsa([_d(ahora - timedelta(minutes=30), 2, cebado=True), _d(ahora - timedelta(minutes=29), 6)])
    pluma.ciclo(con, f, 48, ahora=ahora)
    filas = {r["unidades"]: r for r in con.execute("SELECT unidades, numero_serie, estado_conciliacion FROM dosis")}
    assert {r["numero_serie"] for r in filas.values()} == {"SERIE-PRUEBA"}
    assert filas[Decimal("2.00")]["estado_conciliacion"] == "descartada"
    assert filas[Decimal("6.00")]["estado_conciliacion"] != "descartada"
