"""Importador del CSV de LibreView (formato sintético; se ajustará con el CSV real en el PC)."""
from datetime import datetime, timezone

import pytest

from herramientas.sinteticos import csv_libreview
from servicios.recoleccion.csv_libreview import CsvNoReconocido, _main, a_utc, importar, leer
from zoneinfo import ZoneInfo

L = lambda *a: datetime(*a)          # hora local de Madrid, sin zona
U = lambda *a: datetime(*a, tzinfo=timezone.utc)


def test_lee_dosis_y_pasa_a_utc():
    datos = csv_libreview([(L(2026, 11, 13, 14, 5), 6), (L(2026, 7, 14, 21, 30), 3.5)],
                          glucosa=[(L(2026, 11, 13, 14, 0), 120)], lentas=[(L(2026, 11, 13, 8, 0), 34)])
    lec = leer(datos)
    assert lec.orden_fecha == "dmy"
    assert [(d["ts"], d["unidades"]) for d in lec.dosis] == [
        (U(2026, 11, 13, 13, 5), 6.0),            # invierno: UTC+1
        (U(2026, 7, 14, 19, 30), 3.5)]            # verano: UTC+2
    assert lec.lentas == 1 and lec.errores == []


def test_punto_y_coma_y_coma_decimal():
    lec = leer(csv_libreview([(L(2026, 11, 13, 14, 5), 4.5)], sep=";"))
    assert lec.dosis[0]["unidades"] == 4.5


def test_id_estable_entre_exportaciones():
    a = leer(csv_libreview([(L(2026, 11, 13, 14, 5), 6)]))
    b = leer(csv_libreview([(L(2026, 11, 12, 9, 0), 2), (L(2026, 11, 13, 14, 5), 6)]))
    assert a.dosis[0]["id_externo"] == b.dosis[1]["id_externo"]


def test_cebado_marcado():
    lec = leer(csv_libreview([(L(2026, 11, 13, 14, 4), 2, "Cebado"), (L(2026, 11, 13, 14, 5), 6)]))
    assert lec.dosis[0]["notas"].startswith("cebado")


def test_cambio_de_hora():
    z = ZoneInfo("Europe/Madrid")
    _, aviso = a_utc(L(2026, 10, 25, 2, 30), z)
    assert "ambigua" in aviso
    _, aviso = a_utc(L(2026, 3, 29, 2, 30), z)
    assert "inexistente" in aviso
    assert a_utc(L(2026, 11, 13, 14, 0), z)[1] is None


def test_csv_ajeno():
    with pytest.raises(CsvNoReconocido):
        leer(b"a,b,c\n1,2,3\n")


def test_importar_idempotente_y_todas_marcadas(con):
    dosis = [(L(2026, 11, 13, h, 0), u) for h, u in ((8, 4), (14, 6), (21, 5))]
    datos = csv_libreview(dosis + [(L(2026, 11, 13, 13, 58), 2, "cebado")])
    # la app tiene la de las 14:00 (a las 14:03) y la API de LibreView la de las 21:00
    con.execute("""INSERT INTO dosis (ts, unidades, insulina, origen, id_externo)
                   VALUES (%s, 6, 'fiasp', 'app', 'c1'), (%s, 5, 'fiasp', 'pluma_libreview', 'lv1')""",
                (U(2026, 11, 13, 13, 3), U(2026, 11, 13, 20, 0)))
    r = importar(con, leer(datos))
    assert r["insertadas"] == 4 and r["sin_marcar"] == 0
    assert r["estados_dosis_csv"] == {"solo_pluma": 1, "emparejada": 1, "descartada": 2}
    r2 = importar(con, leer(datos))
    assert r2["insertadas"] == 0 and r2["ya_estaban"] == 4
    n = con.execute("SELECT count(*) AS n, sum(unidades) AS u FROM dosis_efectivas").fetchone()
    assert n["n"] == 3 and float(n["u"]) == 15


def test_cli_simular_no_escribe(tmp_path, capsys):
    f = tmp_path / "lv.csv"
    f.write_bytes(csv_libreview([(L(2026, 11, 13, 14, 5), 6)]))
    assert _main([str(f), "--simular"]) == 0
    salida = capsys.readouterr().out
    assert "dosis de rápida 1" in salida and "no se ha escrito nada" in salida


def test_cabecera_real_de_libreview():
    """Formato real: metadatos en la 1.ª línea, coma decimal entre comillas, dispositivo vacío en la pluma."""
    import csv, io
    from herramientas.sinteticos import CABECERA_LIBREVIEW_ES_REAL as H
    buf = io.StringIO()
    buf.write("Datos de glucosa,Generado el,19-11-2026 13:25 UTC,Generado por,Persona sintética\r\n")
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(H)
    vacia = [""] * len(H)
    g = list(vacia); g[0] = "FreeStyle LibreLink"; g[1] = "S1"; g[2] = "24-07-2022 00:01"; g[3] = "0"; g[4] = "120"
    p = list(vacia); p[1] = "PLUMA-1"; p[2] = "18-11-2026 14:44"; p[3] = "4"; p[7] = "6,0"
    l = list(vacia); l[2] = "18-11-2026 08:00"; l[3] = "4"; l[12] = "34,0"
    for f in (g, p, l):
        w.writerow(f)
    lec = leer(buf.getvalue().encode("utf-8-sig"))
    assert lec.mapeo["rapida"] == "Insulina de acción rápida (unidades)"
    assert lec.mapeo["lenta"] == "Insulina de acción larga (unidades)"
    assert [(d["ts"], d["unidades"], d["numero_serie"]) for d in lec.dosis] == [(U(2026, 11, 18, 13, 44), 6.0, "PLUMA-1")]
    assert lec.lentas == 1
