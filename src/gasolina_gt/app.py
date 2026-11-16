"""API de Gasolina GT sobre la capa Oro (instantánea en Parquet).

  GET /                   panel web (static/index.html)
  GET /health             estado del servicio y de cada tabla Oro
  GET /precios/actual     último precio semanal por combustible
  GET /precios            histórico semanal con filtros (combustible, desde, hasta, limit, offset)
  GET /prediccion         predicción de la semana siguiente (?combustible=super opcional)
  GET /modelo/metricas    exactitud del modelo y de la línea base
  GET /modelo/backtest    predicción vs. realidad en las semanas de prueba
  GET /fotos              lo que leyó el OCR en cada foto, contra el precio real
  GET /docs               Swagger (FastAPI)
"""

from __future__ import annotations

import logging
import math
import os
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

import pandas as pd
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from gasolina_gt import __version__
from gasolina_gt.oro import DATOS_DIR, FECHA_INSTANTANEA, LectorOro, cargar_json

log = logging.getLogger("gasolina.api")
STATIC = Path(__file__).parent / "static"
Combustible = Literal["super", "regular", "diesel"]


LIMIT_DEFAULT, LIMIT_MAX = 100, 1000


def _tz() -> timezone:
    return timezone(timedelta(hours=-6))  # Guatemala


def _limpio(v):
    """Valores de pandas/numpy -> JSON (NaN -> None, fechas -> ISO)."""
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    if isinstance(v, (pd.Timestamp, datetime)):
        return v.isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if hasattr(v, "item"):  # numpy
        return _limpio(v.item())
    if v is pd.NaT:
        return None
    return v


def _registros(df: pd.DataFrame) -> list[dict]:
    return [{k: _limpio(v) for k, v in fila.items()} for fila in df.to_dict(orient="records")]


def _fecha(valor: str | None, nombre: str) -> date | None:
    if valor is None:
        return None
    try:
        return date.fromisoformat(valor)
    except ValueError:
        raise HTTPException(400, f"'{nombre}' debe tener formato AAAA-MM-DD (recibí '{valor}').")


def crear_app() -> FastAPI:
    oro = LectorOro()
    limit_default, limit_max = LIMIT_DEFAULT, LIMIT_MAX
    entorno = os.environ.get("ENTORNO", "local")

    app = FastAPI(
        title="Gasolina GT · precios de combustible en Guatemala",
        version=__version__,
        description=(
            "Precios semanales de súper, regular y diésel leídos de fotos de bombas con OCR, "
            "comparados con la referencia del MEM, y la predicción de si cada uno sube, baja o "
            "se mantiene la semana siguiente. Sirve una instantánea de la capa Oro del pipeline "
            f"medallón original en Databricks (exportada el {FECHA_INSTANTANEA})."
        ),
    )
    app.state.oro = oro
    log.info("API %s | entorno %s | Oro: %s", __version__, entorno, oro.carpeta)

    # ---------------------------------------------------------------- registro de consultas
    @app.middleware("http")
    async def registrar(request: Request, call_next):
        t0 = time.perf_counter()
        resp = await call_next(request)
        if request.url.path.startswith(("/health", "/precios", "/prediccion", "/modelo")):
            log.info("%s %s %s %.1f ms", request.url.path, request.url.query or "-",
                     resp.status_code, (time.perf_counter() - t0) * 1000)
        return resp

    if (DATOS_DIR / "img").is_dir():
        app.mount("/media", StaticFiles(directory=DATOS_DIR / "img"), name="media")

    # ---------------------------------------------------------------- endpoints
    @app.get("/", include_in_schema=False)
    def inicio():
        return FileResponse(STATIC / "index.html", media_type="text/html")

    @app.get("/health", summary="Estado del servicio")
    def health():
        tablas = oro.estado()
        fuentes = {t["fuente"] for t in tablas.values()}
        return {
            "status": "ok",
            "version": __version__,
            "fuente_oro": fuentes.pop() if len(fuentes) == 1 else "mixta",
            "tablas": tablas,
            "instantanea": FECHA_INSTANTANEA,
            "entorno": entorno,
            "hora_servidor": datetime.now(_tz()).isoformat(timespec="seconds"),
        }

    @app.get("/precios/actual", summary="Último precio semanal por combustible")
    def precios_actual():
        df = oro.tabla("precio_semanal")
        if df.empty:
            raise HTTPException(404, "Todavía no hay precios en la capa Oro.")
        # Si una semana tiene fuente "fotos" y "mem", gana "fotos" (lo que leímos nosotros).
        df = df.assign(_pref=(df["fuente"] != "fotos").astype(int)) if "fuente" in df else df
        orden = ["combustible", "semana_inicio"] + (["_pref"] if "_pref" in df else [])
        asc = [True, False] + ([True] if "_pref" in df else [])
        ultimos = df.sort_values(orden, ascending=asc).groupby("combustible").head(1)
        cols = [c for c in ("combustible", "semana_inicio", "semana_fin", "precio_promedio",
                            "precio_min", "precio_max", "fuente") if c in ultimos]
        return {
            "semana_inicio": _limpio(ultimos["semana_inicio"].max()),
            "precios": _registros(ultimos[cols].sort_values("combustible")),
        }

    @app.get("/precios", summary="Histórico semanal con filtros")
    def precios(
        combustible: Combustible | None = Query(None, description="super, regular o diesel"),
        desde: str | None = Query(None, description="AAAA-MM-DD (semana_inicio >= desde)"),
        hasta: str | None = Query(None, description="AAAA-MM-DD (semana_inicio <= hasta)"),
        limit: int = Query(limit_default, ge=1, le=limit_max),
        offset: int = Query(0, ge=0),
    ):
        d, h = _fecha(desde, "desde"), _fecha(hasta, "hasta")
        if d and h and d > h:
            raise HTTPException(400, "'desde' no puede ser posterior a 'hasta'.")
        df = oro.tabla("precio_semanal")
        var = oro.tabla("variacion_semanal")
        if not var.empty:
            extra = [c for c in ("dif_q", "dif_pct", "direccion") if c in var]
            df = df.merge(var[["semana_inicio", "combustible", *extra]],
                          on=["semana_inicio", "combustible"], how="left")
        if combustible:
            df = df[df["combustible"] == combustible]
        if d:
            df = df[df["semana_inicio"] >= d]
        if h:
            df = df[df["semana_inicio"] <= h]
        df = df.sort_values(["semana_inicio", "combustible"])
        return {
            "total": len(df),
            "limit": limit,
            "offset": offset,
            "datos": _registros(df.iloc[offset: offset + limit]),
        }

    @app.get("/prediccion", summary="Predicción de la semana siguiente")
    def prediccion(combustible: Combustible | None = Query(None)):
        df = oro.tabla("predicciones")
        if df.empty:
            raise HTTPException(404, "Todavía no hay predicciones en la capa Oro.")
        if "fecha_generacion" in df:
            df = df.sort_values("fecha_generacion")
        ult = df.sort_values("semana_objetivo").groupby("combustible").tail(1)

        def fmt(f: dict) -> dict:
            return {
                "semana": _limpio(f.get("semana_objetivo")),
                "combustible": f.get("combustible"),
                "prediccion": f.get("prediccion"),
                "probabilidad": _limpio(f.get("probabilidad")),
                "prediccion_baseline": f.get("prediccion_baseline"),
                "modelo": f.get("modelo"),
                "version_modelo": _limpio(f.get("version_modelo")),
            }

        filas = [fmt(f) for f in ult.sort_values("combustible").to_dict(orient="records")]
        if combustible:
            una = [f for f in filas if f["combustible"] == combustible]
            if not una:
                raise HTTPException(404, f"No hay predicción para '{combustible}'.")
            return una[0]
        return {"semana": max(f["semana"] for f in filas), "predicciones": filas}

    @app.get("/modelo/metricas", summary="Exactitud del modelo frente a la línea base")
    def modelo_metricas():
        df = oro.tabla("metricas_modelo")
        if df.empty:
            raise HTTPException(404, "Todavía no hay métricas del modelo en la capa Oro.")
        if "es_champion" in df and df["es_champion"].astype(bool).any():
            df = df[df["es_champion"].astype(bool)]
        fila = df.sort_values("fecha_entrenamiento").iloc[-1].to_dict()
        return {k: _limpio(v) for k, v in fila.items()}

    @app.get("/modelo/backtest", summary="Predicción contra la realidad en las semanas de prueba")
    def modelo_backtest():
        filas = cargar_json("backtest.json")
        if not filas:
            raise HTTPException(404, "No hay resultados de la prueba temporal.")
        n = len(filas)
        return {
            "semanas": sorted({f["semana"] for f in filas}),
            "aciertos_modelo": sum(f["real"] == f["modelo"] for f in filas),
            "aciertos_linea_base": sum(f["real"] == f["base"] for f in filas),
            "total": n,
            "detalle": filas,
        }

    @app.get("/fotos", summary="Lectura del OCR en cada foto frente al precio real")
    def fotos(detalle: bool = Query(False, description="Incluir todas las detecciones del OCR")):
        datos = cargar_json("fotos.json")
        if not datos:
            raise HTTPException(404, "No hay resultados del OCR.")
        if not detalle:
            datos = [{k: v for k, v in f.items() if k != "crudas"} for f in datos]
        evaluados = [(f["leidos"].get(c), v) for f in datos for c, v in f["reales"].items()]
        correctos = sum(1 for leido, real in evaluados
                        if leido is not None and abs(leido - real) < 0.005)
        return {"total_fotos": len(datos), "precios_etiquetados": len(evaluados),
                "precios_correctos": correctos, "fotos": datos}

    return app


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
app = crear_app()
