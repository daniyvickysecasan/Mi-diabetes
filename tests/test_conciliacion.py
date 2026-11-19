"""Conciliación app ↔ pluma: ninguna inyección se cuenta dos veces y toda duda queda visible."""
from datetime import datetime, timedelta, timezone

import pytest

from comun.conciliacion import RevisionInvalida, calcular, conciliar, revisar
from comun.insulina import insulina_activa_bd

T0 = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
_n = iter(range(1, 10**6))


def m(minutos):
    return T0 + timedelta(minutes=minutos)


def fila(origen, minutos, u, revisada=False, estado="pendiente", notas=None, par=None, id=None):
    return {"id": id or next(_n), "ts": m(minutos), "unidades": u, "origen": origen,
            "revisada_en": T0 if revisada else None, "estado_conciliacion": estado, "dosis_par_id": par,
            "notas": notas}


# ------------------------------------------------------------------ función pura
def test_pareja_exacta():
    a, p = fila("app", 0, 4), fila("pluma_libreview", 3, 4)
    r = calcular([a, p])
    assert r[a["id"]].estado == r[p["id"]].estado == "emparejada"
    assert r[a["id"]].par == p["id"] and r[p["id"]].par == a["id"]
    assert not r[a["id"]].revisar


def test_unidades_distintas_es_discrepancia_y_manda_la_pluma():
    a, p = fila("app", 0, 4), fila("pluma_libreview", 10, 5)
    r = calcular([a, p])
    assert r[a["id"]].estado == "discrepancia" and r[a["id"]].revisar
    assert "Cuenta la pluma" in r[a["id"]].motivo


def test_fuera_de_ventana_no_empareja():
    a, p = fila("app", 0, 4), fila("pluma_libreview", 16, 4)
    r = calcular([a, p])
    assert r[a["id"]].estado == "solo_app" and r[a["id"]].revisar     # la pluma sincronizó después
    assert r[p["id"]].estado == "solo_pluma"


def test_app_pendiente_mientras_la_pluma_no_sincroniza():
    a, p = fila("app", 0, 4), fila("pluma_libreview", -120, 3)
    r = calcular([a, p])
    assert r[a["id"]].estado == "pendiente" and not r[a["id"]].revisar


def test_prefiere_unidades_iguales_y_luego_la_mas_cercana():
    a = fila("app", 0, 6)
    p1, p2, p3 = fila("pluma_libreview", 1, 2), fila("pluma_libreview", 12, 6), fila("pluma_libreview", 5, 6)
    r = calcular([a, p1, p2, p3])
    assert r[a["id"]].par == p3["id"] and r[a["id"]].estado == "emparejada"
    assert r[p1["id"]].estado == r[p2["id"]].estado == "solo_pluma"


def test_dos_dosis_seguidas_se_emparejan_una_a_una():
    a1, a2 = fila("app", 0, 4), fila("app", 8, 2)
    p1, p2 = fila("pluma_libreview", 1, 4), fila("pluma_libreview", 9, 2)
    r = calcular([a1, a2, p1, p2])
    assert (r[a1["id"]].par, r[a2["id"]].par) == (p1["id"], p2["id"])


def test_duplicado_api_csv():
    api, csv_ = fila("pluma_libreview", 0, 4), fila("csv_libreview", 1, 4)
    r = calcular([csv_, api])
    assert r[csv_["id"]].estado == "descartada" and r[csv_["id"]].par == api["id"]
    assert r[api["id"]].estado == "solo_pluma"


def test_csv_no_se_deduplica_con_otro_csv_ni_con_unidades_distintas():
    c1, c2, api = fila("csv_libreview", 0, 4), fila("csv_libreview", 1, 4), fila("pluma_libreview", 1, 4.5)
    r = calcular([c1, c2, api])
    assert all(r[f["id"]].estado != "descartada" for f in (c1, c2, api))


def test_cebado_marcado_en_libreview_no_cuenta():
    c, d = fila("pluma_libreview", 0, 2, notas="cebado"), fila("pluma_libreview", 1, 6)
    r = calcular([c, d])
    assert r[c["id"]].estado == "descartada" and r[c["id"]].par is None
    assert r[d["id"]].estado == "solo_pluma"


def test_posible_cebado_sin_marcar_se_revisa_y_cuenta():
    c, d = fila("pluma_libreview", 0, 1), fila("pluma_libreview", 2, 6)
    r = calcular([c, d])
    assert r[c["id"]].estado == "solo_pluma" and r[c["id"]].revisar and "Cebado" in r[c["id"]].motivo
    assert not r[d["id"]].revisar


def test_filas_revisadas_no_se_tocan():
    a = fila("app", 0, 4, revisada=True, estado="solo_app")
    p = fila("pluma_libreview", 2, 4)
    r = calcular([a, p])
    assert a["id"] not in r and r[p["id"]].estado == "solo_pluma"


def test_api_tardia_frente_a_csv_ya_revisado():
    c = fila("csv_libreview", 0, 4, revisada=True, estado="solo_pluma")
    api = fila("pluma_libreview", 0, 4)
    r = calcular([c, api])
    assert r[api["id"]].estado == "descartada" and r[api["id"]].par == c["id"]


def test_hora_ambigua_del_csv_se_revisa():
    p = fila("csv_libreview", 0, 4, notas="hora ambigua (cambio de hora de otoño): se toma la primera")
    r = calcular([p])
    assert r[p["id"]].revisar and "hora ambigua" in r[p["id"]].motivo


# ------------------------------------------------------------------ contra la base
def _ins(con, minutos, u, origen="app", id_ext=None, notas=None, insulina="fiasp"):
    return con.execute(
        """INSERT INTO dosis (ts, unidades, insulina, origen, id_externo, notas) VALUES (%s,%s,%s,%s,%s,%s)
           RETURNING id""", (m(minutos), u, insulina, origen, id_ext or f"x{next(_n)}", notas)).fetchone()["id"]


def _iob_ids(con, minutos=60):
    return sorted(d["id"] for d in insulina_activa_bd(con, m(minutos))["dosis"])


def test_bd_app_luego_pluma_luego_csv_cuenta_una_vez(con):
    a = _ins(con, 0, 4)
    conciliar(con, m(1))
    assert _iob_ids(con) == [a]                          # antes de la pluma, cuenta la app
    p = _ins(con, 2, 4, "pluma_libreview")
    conciliar(con, m(5))
    assert _iob_ids(con) == [p]                          # emparejadas: cuenta la pluma
    c = _ins(con, 2, 4, "csv_libreview")
    r = conciliar(con, m(6))
    assert _iob_ids(con) == [p]                          # el CSV repite: descartado
    assert r["estados"]["descartada"] == 1
    estado = {f["id"]: f for f in con.execute("SELECT * FROM dosis").fetchall()}
    assert estado[c]["dosis_par_id"] == p and estado[a]["dosis_par_id"] == p
    assert conciliar(con, m(7))["cambiadas"] == 0        # idempotente


def test_bd_comida_la_pone_la_app(con):
    cid = con.execute("INSERT INTO comidas (ts, origen) VALUES (%s, 'manual') RETURNING id", (T0,)).fetchone()["id"]
    a = _ins(con, 0, 4)
    con.execute("UPDATE dosis SET comida_id = %s WHERE id = %s", (cid, a))
    p = _ins(con, 4, 4, "pluma_libreview")
    conciliar(con, m(5))
    f = con.execute("SELECT * FROM dosis_efectivas").fetchall()
    assert [(x["id"], x["comida_id"], x["dosis_app_id"]) for x in f] == [(p, cid, a)]


def test_bd_revision_separar_descartar_reabrir(con):
    a = _ins(con, 0, 4)
    p = _ins(con, 5, 6, "pluma_libreview")
    conciliar(con, m(6))
    assert _iob_ids(con) == [p]
    revisar(con, a, "separar", m(7))                     # eran dos inyecciones
    assert _iob_ids(con) == sorted([a, p])
    revisar(con, a, "reabrir", m(8))
    assert _iob_ids(con) == [p]
    revisar(con, p, "descartar", m(9))                   # la de la pluma no era una inyección
    assert _iob_ids(con) == [a]
    with pytest.raises(RevisionInvalida):
        revisar(con, a, "separar", m(10))


def test_bd_aceptar_congela(con):
    a = _ins(con, 0, 4)
    p = _ins(con, 5, 5, "pluma_libreview")
    conciliar(con, m(6))
    revisar(con, a, "aceptar", m(7))
    filas = con.execute("SELECT * FROM dosis ORDER BY id").fetchall()
    assert all(f["revision"] == "aceptada" and not f["requiere_revision"] for f in filas)
    _ins(con, 1, 4, "pluma_libreview")                   # una pluma "mejor" llega después
    conciliar(con, m(8))
    assert con.execute("SELECT dosis_par_id FROM dosis WHERE id=%s", (a,)).fetchone()["dosis_par_id"] == p


def test_bd_no_acepta_pendiente(con):
    a = _ins(con, 0, 4)
    conciliar(con, m(1))
    with pytest.raises(RevisionInvalida):
        revisar(con, a, "aceptar", m(2))


def test_bd_restricciones_impiden_estados_incoherentes(con):
    import psycopg
    a = _ins(con, 0, 4)
    with pytest.raises(psycopg.errors.CheckViolation):
        con.execute("UPDATE dosis SET estado_conciliacion = 'emparejada' WHERE id = %s", (a,))


def test_bd_aleatorio_ninguna_inyeccion_dos_veces(con):
    """Muchas combinaciones: la insulina efectiva nunca supera lo que dice la pluma más las solo_app."""
    import random
    rnd = random.Random(11)
    reales = []
    for i in range(40):
        t = i * 37
        u = rnd.choice([1, 2, 3.5, 4, 6, 8])
        reales.append((t, u))
        if rnd.random() < 0.8:
            _ins(con, t + rnd.randint(-10, 10), u if rnd.random() < 0.85 else u + 1)
        _ins(con, t, u, "pluma_libreview")
        if rnd.random() < 0.5:
            _ins(con, t, u, "csv_libreview")
    conciliar(con, m(2000))
    efectivas = con.execute("SELECT origen, count(*) AS n, sum(unidades) AS u FROM dosis_efectivas GROUP BY 1"
                            ).fetchall()
    por = {f["origen"]: f for f in efectivas}
    assert por["pluma_libreview"]["n"] == 40 and float(por["pluma_libreview"]["u"]) == sum(u for _, u in reales)
    assert "csv_libreview" not in por and "app" not in por
    assert con.execute("SELECT count(*) AS n FROM dosis WHERE estado_conciliacion='pendiente'").fetchone()["n"] == 0
