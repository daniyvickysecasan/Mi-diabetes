"""Aviso "Lee la pluma": función pura y lectura desde la base. Solo informa, no toca ninguna dosis."""
from datetime import datetime, timedelta, timezone

from comun import aviso_pluma as AP
from comun.conciliacion import conciliar

AHORA = datetime(2026, 11, 19, 14, 0, tzinfo=timezone.utc)


def h(horas):
    return AHORA - timedelta(hours=horas)


def test_sin_aviso_si_la_pluma_es_reciente():
    assert AP.evaluar(AHORA, h(3), []) is None


def test_aviso_si_hace_mucho_de_la_ultima_dosis_de_la_pluma():
    a = AP.evaluar(AHORA, h(13), [])
    assert a["motivo"] == "sin_lecturas" and "13 h" in a["texto"] and "LibreLink" in a["texto"]
    assert a["ultima_pluma"] == h(13).isoformat()


def test_el_umbral_es_inclusivo_y_configurable():
    assert AP.evaluar(AHORA, h(12), [])["motivo"] == "sin_lecturas"
    assert AP.evaluar(AHORA, h(11.9), []) is None
    assert AP.evaluar(AHORA, h(7), [], umbral_horas=6)["motivo"] == "sin_lecturas"


def test_aviso_si_nunca_ha_llegado_nada_de_la_pluma():
    a = AP.evaluar(AHORA, None, [])
    assert a["motivo"] == "sin_lecturas" and a["ultima_pluma"] is None


def test_dosis_apuntada_sin_llegar_de_la_pluma_avisa_aunque_la_pluma_sea_reciente():
    a = AP.evaluar(AHORA, h(2), [h(1)])
    assert a["motivo"] == "dosis_sin_leer" and a["pendientes"] == 1
    assert a["texto"].startswith("Tienes 1 dosis apuntada hace 1 h")


def test_dosis_recien_apuntada_no_avisa_dentro_del_margen():
    assert AP.evaluar(AHORA, h(2), [AHORA - timedelta(minutes=30)]) is None
    assert AP.evaluar(AHORA, h(2), [AHORA - timedelta(minutes=30)], margen_min=20)["pendientes"] == 1


def test_varias_pendientes_cuenta_solo_las_pasadas_del_margen():
    a = AP.evaluar(AHORA, None, [h(3), h(2), AHORA - timedelta(minutes=5)])
    assert a["pendientes"] == 2 and "Tienes 2 dosis apuntadas hace 3 h" in a["texto"]


def _dosis(con, ts, u, origen, id_ext, notas=None):
    con.execute("""INSERT INTO dosis (ts, unidades, insulina, origen, id_externo, notas)
                   VALUES (%s, %s, 'fiasp', %s, %s, %s)""", (ts, u, origen, id_ext, notas))


def test_desde_la_base(con):
    assert AP.evaluar_bd(con, AHORA)["motivo"] == "sin_lecturas"
    _dosis(con, h(20), 4, "csv_libreview", "c1")
    assert "20 h" in AP.evaluar_bd(con, AHORA)["texto"]
    _dosis(con, h(3), 5, "pluma_libreview", "p1")
    assert AP.evaluar_bd(con, AHORA) is None
    # una dosis de la app posterior a lo último de la pluma queda pendiente → avisa
    _dosis(con, h(1), 6, "app", "a1")
    conciliar(con, ahora=AHORA)
    assert con.execute("SELECT estado_conciliacion FROM dosis WHERE id_externo = 'a1'").fetchone()[
        "estado_conciliacion"] == "pendiente"
    a = AP.evaluar_bd(con, AHORA)
    assert a["motivo"] == "dosis_sin_leer" and a["pendientes"] == 1
    # llega de la pluma → se empareja y el aviso desaparece
    _dosis(con, h(1) + timedelta(minutes=1), 6, "pluma_libreview", "p2")
    conciliar(con, ahora=AHORA)
    assert AP.evaluar_bd(con, AHORA) is None


def test_un_cebado_descartado_tambien_prueba_que_se_leyo(con):
    _dosis(con, h(1), 2, "pluma_libreview", "p1", notas="cebado")
    conciliar(con, ahora=AHORA)
    assert AP.evaluar_bd(con, AHORA) is None


def test_dosis_futuras_no_cuentan_como_lectura(con):
    _dosis(con, AHORA + timedelta(hours=1), 3, "pluma_libreview", "p1")
    assert AP.evaluar_bd(con, AHORA)["ultima_pluma"] is None


def test_con_libreview_cada_pocas_horas_cuenta_hasta_la_ultima_lectura():
    dosis = AHORA - timedelta(hours=2)
    # LibreView se leyó hace 3 h (antes de la dosis): aún no ha podido verla, no se avisa.
    assert AP.evaluar(AHORA, h(3), [dosis], ultima_lectura=AHORA - timedelta(hours=3)) is None
    # Se leyó hace 1 h (1 h después de la dosis) y no estaba: falta leer la pluma.
    a = AP.evaluar(AHORA, h(3), [dosis], ultima_lectura=AHORA - timedelta(hours=1))
    assert a["motivo"] == "dosis_sin_leer" and "2 h" in a["texto"]
    # Leída solo 20 min después de la dosis: dentro del margen, sin aviso todavía.
    assert AP.evaluar(AHORA, h(3), [dosis], ultima_lectura=dosis + timedelta(minutes=20)) is None


def test_aviso_en_la_base_usa_la_ultima_lectura_de_libreview(con):
    from datetime import datetime, timezone
    ahora = datetime.now(timezone.utc)
    con.execute("INSERT INTO dosis (ts, unidades, insulina, origen, id_externo) VALUES (%s, 4, 'fiasp', 'app', 'a-1')",
                (ahora - timedelta(hours=2),))
    con.execute("UPDATE estado_libreview SET estado='ok', ultimo_ok=%s", (ahora - timedelta(hours=3),))
    assert (AP.evaluar_bd(con, ahora) or {}).get("motivo") != "dosis_sin_leer"
    con.execute("UPDATE estado_libreview SET ultimo_ok=%s", (ahora - timedelta(minutes=5),))
    assert AP.evaluar_bd(con, ahora)["motivo"] == "dosis_sin_leer"


def test_sin_lecturas_tambien_cuenta_hasta_la_ultima_lectura():
    # Última dosis de la pluma hace 14 h, pero LibreView se miró hace 10 h (4 h después): aún no avisa.
    assert AP.evaluar(AHORA, h(14), [], ultima_lectura=h(10)) is None
    assert AP.evaluar(AHORA, h(14), [], ultima_lectura=h(1))["motivo"] == "sin_lecturas"
