"""Importador del CSV exportado de LibreView: historial de dosis de la pluma y respaldo semanal.

Solo importa la insulina rápida (Fiasp): la pluma inteligente es solo de Fiasp. La insulina lenta
que aparezca (Tresiba) se cuenta en el resumen y no se importa: la Tresiba se configura en la app.
La glucosa del CSV tampoco se importa (la fuente de glucosa es Nightscout).

Idempotente: cada dosis lleva un id determinista (serie + sello de tiempo del CSV + unidades), así
que importar el mismo CSV, o exportaciones solapadas, no duplica nada. Tras importar se concilia
con la app y con la API de LibreView (comun.conciliacion).

El CSV real NUNCA se copia al repositorio. Uso en el PC (ver despliegue/README_despliegue_windows.md):

    python -m servicios.recoleccion.csv_libreview RUTA.csv --simular   # solo enseña lo que haría
    python -m servicios.recoleccion.csv_libreview RUTA.csv             # importa y concilia
"""
import argparse
import csv
import hashlib
import io
import json
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ORIGEN = "csv_libreview"


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


# Nombres de columna conocidos (inglés y español), normalizados sin acentos ni signos.
ALIAS = {
    "dispositivo": ["device", "dispositivo"],
    "serie": ["serial number", "numero de serie"],
    "ts": ["device timestamp", "sello de tiempo del dispositivo", "marca de tiempo del dispositivo",
           "fecha y hora del dispositivo", "timestamp", "fecha y hora"],
    "tipo_registro": ["record type", "tipo de registro"],
    "rapida": ["rapid acting insulin units", "insulina de accion rapida unidades", "insulina rapida unidades",
               "insulina de accion rapida u"],
    "lenta": ["long acting insulin units", "long acting insulin value units", "insulina de accion prolongada unidades",
              "insulina de accion lenta unidades", "insulina lenta unidades"],
    "notas": ["notes", "notas"],
}

FORMATOS = {
    "dmy": ["%d-%m-%Y %H:%M", "%d-%m-%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S", "%d.%m.%Y %H:%M"],
    "mdy": ["%m-%d-%Y %H:%M", "%m-%d-%Y %I:%M %p", "%m-%d-%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%m/%d/%Y %I:%M %p"],
    "iso": ["%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d %H:%M"],
}


class CsvNoReconocido(ValueError):
    pass


@dataclass
class Lectura:
    columnas: list[str]
    mapeo: dict[str, str]
    orden_fecha: str
    filas: int = 0
    dosis: list[dict] = field(default_factory=list)
    lentas: int = 0
    errores: list[str] = field(default_factory=list)
    avisos: list[str] = field(default_factory=list)


def _numero(s: str) -> float | None:
    s = (s or "").strip().replace(",", ".")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _decodificar(datos: bytes) -> str:
    for cod in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            texto = datos.decode(cod)
            if "\x00" not in texto:
                return texto
        except UnicodeDecodeError:
            continue
    raise CsvNoReconocido("No se reconoce la codificación del archivo")


def _buscar_cabecera(texto: str) -> tuple[list[list[str]], int, str]:
    """LibreView pone una línea de metadatos antes de la cabecera: se busca la fila con la hora."""
    lineas = texto.splitlines()
    for sep in (",", ";", "\t"):
        filas = list(csv.reader(io.StringIO("\n".join(lineas)), delimiter=sep))
        for i, f in enumerate(filas[:10]):
            nombres = {_norm(c) for c in f}
            if any(a in nombres for a in ALIAS["ts"]):
                return filas, i, sep
    raise CsvNoReconocido("No encuentro la columna de fecha y hora del dispositivo en las 10 primeras líneas")


def _mapear(cabecera: list[str]) -> dict[str, str]:
    normal = {_norm(c): c for c in cabecera}
    mapeo = {}
    for clave, alias in ALIAS.items():
        for a in alias:
            if a in normal:
                mapeo[clave] = normal[a]
                break
    return mapeo


def _orden_fechas(valores: list[str], idioma_es: bool, forzado: str | None) -> str:
    if forzado:
        return forzado
    muestra = [v.strip() for v in valores if v.strip()]
    if muestra and all(re.match(r"\d{4}[-/]", v) for v in muestra[:50]):
        return "iso"
    primero = segundo = False
    for v in muestra:
        m = re.match(r"(\d{1,2})[-/.](\d{1,2})[-/.]\d{4}", v)
        if m:
            primero |= int(m.group(1)) > 12
            segundo |= int(m.group(2)) > 12
    if primero and not segundo:
        return "dmy"
    if segundo and not primero:
        return "mdy"
    if primero and segundo:
        raise CsvNoReconocido("Fechas incoherentes: hay días en las dos primeras posiciones")
    return "dmy" if idioma_es else "mdy"


def _fecha(v: str, orden: str) -> datetime:
    v = re.sub(r"\s+", " ", v.strip().upper().replace("A. M.", "AM").replace("P. M.", "PM"))
    for fmt in FORMATOS[orden]:
        try:
            return datetime.strptime(v, fmt)
        except ValueError:
            continue
    raise ValueError(f"fecha no reconocida: {v!r}")


def a_utc(local: datetime, zona: ZoneInfo) -> tuple[datetime, str | None]:
    """Hora local sin zona → UTC. Avisa de las horas ambiguas o inexistentes del cambio de hora."""
    a = local.replace(tzinfo=zona, fold=0)
    b = local.replace(tzinfo=zona, fold=1)
    aviso = None
    if a.utcoffset() != b.utcoffset():
        ida = a.astimezone(timezone.utc).astimezone(zona).replace(tzinfo=None)
        aviso = ("hora inexistente (cambio de hora de primavera)" if ida != local
                 else "hora ambigua (cambio de hora de otoño): se toma la primera")
    return a.astimezone(timezone.utc), aviso


def id_estable(serie: str, sello: str, unidades: float) -> str:
    h = hashlib.sha256(f"{serie.strip()}|{sello.strip()}|{unidades:.2f}".encode()).hexdigest()[:16]
    return f"csv:{h}"


def leer(datos: bytes, zona: str = "Europe/Madrid", orden_fecha: str | None = None) -> Lectura:
    """Interpreta el CSV sin tocar la base (función pura)."""
    texto = _decodificar(datos)
    filas, i_cab, _sep = _buscar_cabecera(texto)
    cabecera = filas[i_cab]
    mapeo = _mapear(cabecera)
    if "rapida" not in mapeo:
        raise CsvNoReconocido("No encuentro la columna de insulina rápida (unidades). Columnas: " + ", ".join(cabecera))
    idx = {k: cabecera.index(c) for k, c in mapeo.items()}
    cuerpo = [f for f in filas[i_cab + 1:] if any(c.strip() for c in f)]
    col = lambda f, k: f[idx[k]] if k in idx and idx[k] < len(f) else ""
    idioma_es = any(a in _norm(" ".join(cabecera)) for a in ("dispositivo", "insulina"))
    orden = _orden_fechas([col(f, "ts") for f in cuerpo], idioma_es, orden_fecha)
    lec = Lectura(columnas=cabecera, mapeo=mapeo, orden_fecha=orden, filas=len(cuerpo))
    z = ZoneInfo(zona)
    for n, f in enumerate(cuerpo, start=i_cab + 2):
        u_rapida, u_lenta = _numero(col(f, "rapida")), _numero(col(f, "lenta"))
        if u_lenta:
            lec.lentas += 1
        if u_rapida is None:
            if col(f, "rapida").strip():
                lec.errores.append(f"línea {n}: unidades no numéricas {col(f, 'rapida')!r}")
            continue
        if u_rapida <= 0:
            continue
        try:
            local = _fecha(col(f, "ts"), orden)
        except ValueError as e:
            lec.errores.append(f"línea {n}: {e}")
            continue
        ts, aviso = a_utc(local, z)
        if re.search(r"\b(cebado|priming|prime|purga)\b", _norm(col(f, "notas"))):
            aviso = "cebado (marcado en LibreView)" + (f"; {aviso}" if aviso else "")
        serie = col(f, "serie")
        d = {"ts": ts, "unidades": round(u_rapida, 2), "id_externo": id_estable(serie, col(f, "ts"), u_rapida),
             "numero_serie": serie or None, "dispositivo": col(f, "dispositivo") or None, "linea": n,
             "notas": aviso}
        if aviso:
            lec.avisos.append(f"línea {n}: {aviso}")
        lec.dosis.append(d)
    return lec


def importar(con, lec: Lectura, ahora: datetime | None = None) -> dict:
    """Inserta las dosis leídas y concilia. Devuelve un resumen con el estado de cada dosis del CSV."""
    from comun.conciliacion import conciliar
    ahora = ahora or datetime.now(timezone.utc)
    ins = 0
    with con.transaction():
        for d in lec.dosis:
            r = con.execute(
                """INSERT INTO dosis (ts, unidades, insulina, origen, id_externo, numero_serie, notas,
                                      requiere_revision)
                   VALUES (%s, %s, 'fiasp', %s, %s, %s, %s, %s)
                   ON CONFLICT (origen, id_externo) DO NOTHING RETURNING id""",
                (d["ts"], d["unidades"], ORIGEN, d["id_externo"], d["numero_serie"], d["notas"],
                 d["notas"] is not None and not d["notas"].startswith("cebado"))).fetchone()
            ins += r is not None
    conc = conciliar(con, ahora)
    ids = [d["id_externo"] for d in lec.dosis]
    estados = {f["estado_conciliacion"]: f["n"] for f in con.execute(
        """SELECT estado_conciliacion, count(*) AS n FROM dosis WHERE origen = %s AND id_externo = ANY(%s)
           GROUP BY 1""", (ORIGEN, ids)).fetchall()}
    sin_marcar = sum(n for e, n in estados.items() if e == "pendiente")
    return {"filas_csv": lec.filas, "dosis_rapida": len(lec.dosis), "insertadas": ins,
            "ya_estaban": len(lec.dosis) - ins, "lentas_ignoradas": lec.lentas, "errores": lec.errores,
            "avisos": lec.avisos, "estados_dosis_csv": estados, "sin_marcar": sin_marcar,
            "conciliacion": conc}


def _main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Importa las dosis de Fiasp de un CSV de LibreView.")
    p.add_argument("csv", type=Path)
    p.add_argument("--simular", action="store_true", help="no escribe nada: muestra columnas y recuentos")
    p.add_argument("--orden-fecha", choices=["dmy", "mdy", "iso"], help="si la detección automática duda")
    a = p.parse_args(argv)
    try:
        lec = leer(a.csv.read_bytes(), orden_fecha=a.orden_fecha)
    except CsvNoReconocido as e:
        print(f"CSV no reconocido: {e}", file=sys.stderr)
        return 2
    print("Columnas del CSV:", " | ".join(lec.columnas))
    print("Columnas usadas:", json.dumps(lec.mapeo, ensure_ascii=False))
    print(f"Orden de fecha: {lec.orden_fecha} · filas {lec.filas} · dosis de rápida {len(lec.dosis)} · "
          f"filas con lenta (no se importan) {lec.lentas} · errores {len(lec.errores)}")
    if lec.dosis:
        z = ZoneInfo("Europe/Madrid")
        ts = [d["ts"] for d in lec.dosis]
        print(f"Desde {min(ts).astimezone(z):%d-%m-%Y %H:%M} hasta {max(ts).astimezone(z):%d-%m-%Y %H:%M} (hora de Madrid)")
        tam = {}
        for d in lec.dosis:
            tam[d["unidades"]] = tam.get(d["unidades"], 0) + 1
        print("Unidades más frecuentes:", ", ".join(f"{u:g} U ×{n}" for u, n in sorted(tam.items(), key=lambda x: -x[1])[:8]))
    for e in (lec.errores + lec.avisos)[:20]:
        print("  ", e)
    if a.simular:
        print("Simulación: no se ha escrito nada.")
        return 0
    from comun.ajustes import Ajustes
    from comun.bd import conectar
    with conectar(Ajustes.desde_entorno().database_url) as con:
        r = importar(con, lec)
    print(json.dumps({k: v for k, v in r.items() if k not in ("errores", "avisos")}, ensure_ascii=False, indent=2))
    if r["sin_marcar"]:
        print("ATENCIÓN: hay dosis del CSV sin conciliar.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_main())
