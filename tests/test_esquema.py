"""Esquema: migraciones idempotentes y restricciones de seguridad."""
import psycopg
import pytest

from comun.migrar import migrar


def test_migrar_dos_veces_no_hace_nada(url_bd):
    assert migrar(url_bd) == []


def test_glucosa_es_hypertable(con):
    n = con.execute("SELECT count(*) AS n FROM timescaledb_information.hypertables WHERE hypertable_name='glucosa'"
                    ).fetchone()["n"]
    assert n == 1


def _version(con, version, estado, aprobada=False):
    con.execute(
        """INSERT INTO versiones_parametros (version, estado, origen, ratio_hc, sensibilidad, aprobada_por, aprobada_en)
           VALUES (%s, %s, 'manual', '[]', '[]', %s, %s)""",
        (version, estado, "daniel" if aprobada else None, "now()" if aprobada else None))


def test_parametros_no_se_activan_sin_aprobacion(con):
    with pytest.raises(psycopg.errors.CheckViolation):
        _version(con, 1, "activa")
    with pytest.raises(psycopg.errors.CheckViolation):
        _version(con, 2, "sombra")
    _version(con, 3, "propuesta")


def test_solo_una_version_activa(con):
    _version(con, 1, "activa", aprobada=True)
    with pytest.raises(psycopg.errors.UniqueViolation):
        _version(con, 2, "activa", aprobada=True)


def test_sugerencia_bloqueada_no_lleva_unidades(con):
    _version(con, 1, "activa", aprobada=True)
    with pytest.raises(psycopg.errors.CheckViolation):
        con.execute("""INSERT INTO sugerencias (version_parametros_id, calculo_version, incertidumbre, traza,
                       bloqueada, motivo_bloqueo, u_inmediata) VALUES (1, 't', '{}', '{}', true, 'hipo', 2)""")
    con.execute("""INSERT INTO sugerencias (version_parametros_id, calculo_version, incertidumbre, traza,
                   bloqueada, motivo_bloqueo) VALUES (1, 't', '{}', '{}', true, 'hipo prevista')""")


def test_dosis_no_se_duplica_por_origen_e_id(con):
    sql = "INSERT INTO dosis (ts, unidades, insulina, origen, id_externo) VALUES (now(), 3, 'fiasp', 'csv_libreview', 'x1')"
    con.execute(sql)
    with pytest.raises(psycopg.errors.UniqueViolation):
        con.execute(sql)


def test_dosis_valores_invalidos(con):
    with pytest.raises(psycopg.errors.CheckViolation):
        con.execute("INSERT INTO dosis (ts, unidades, insulina, origen) VALUES (now(), 0, 'fiasp', 'app')")
    with pytest.raises(psycopg.errors.CheckViolation):
        con.execute("INSERT INTO dosis (ts, unidades, insulina, origen) VALUES (now(), 2, 'humalog', 'app')")
