"""Lectura de la capa Oro desde una instantánea local en Parquet.

En el proyecto original la API leía las tablas Oro de Databricks (SQL warehouse) que
generaba un Job semanal (Bronce -> Plata -> Oro). Esta versión de portafolio sirve una
instantánea exportada de esas mismas tablas, así que funciona sin credenciales ni servicios
externos. La carpeta se puede cambiar con la variable ORO_DIR.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

TABLAS_ORO = (
    "precio_semanal",
    "variacion_semanal",
    "features_modelo",
    "predicciones",
    "metricas_modelo",
)
ORO_DIR = Path(os.environ.get("ORO_DIR") or Path(__file__).resolve().parents[2] / "data" / "oro")
FECHA_INSTANTANEA = os.environ.get("FECHA_INSTANTANEA", "2026-11-15")


@dataclass
class LectorOro:
    carpeta: Path = ORO_DIR
    _cache: dict[str, pd.DataFrame] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def tabla(self, nombre: str) -> pd.DataFrame:
        if nombre not in TABLAS_ORO:
            raise KeyError(nombre)
        with self._lock:
            if nombre not in self._cache:
                ruta = self.carpeta / f"{nombre}.parquet"
                self._cache[nombre] = (_normalizar(pd.read_parquet(ruta)) if ruta.is_file()
                                       else pd.DataFrame())
            return self._cache[nombre]

    def estado(self) -> dict:
        out = {}
        for t in TABLAS_ORO:
            df = self.tabla(t)
            ultima = None
            for col in ("semana_inicio", "semana_objetivo", "fecha_entrenamiento"):
                if col in df.columns and not df.empty:
                    ultima = str(df[col].max())[:10]
                    break
            out[t] = {"fuente": "instantanea", "filas": len(df), "ultima": ultima,
                      **({} if len(df) else {"error": "Tabla no encontrada"})}
        return out


def _normalizar(df: pd.DataFrame) -> pd.DataFrame:
    """Fechas como date y números como float (la fuente "databricks" devuelve texto)."""
    df = df.copy()
    for col in df.columns:
        # pandas 3 usa el dtype "str" (no object) para texto: tratar ambos igual.
        texto = pd.api.types.is_object_dtype(df[col]) or pd.api.types.is_string_dtype(df[col])
        if col.startswith(("semana_", "fecha")) and col != "fecha_generacion":
            df[col] = pd.to_datetime(df[col], errors="coerce").dt.date
        elif col == "fecha_generacion":
            df[col] = pd.to_datetime(df[col], errors="coerce")
        elif texto and set(df[col].dropna().astype(str).str.lower()) <= {
            "true", "false"} and df[col].notna().any():
            df[col] = df[col].astype(str).str.lower().map({"true": True, "false": False})
        elif texto and col not in ("version_modelo", "mlflow_run_id"):
            conv = pd.to_numeric(df[col].astype(object), errors="coerce")
            if conv.notna().sum() == df[col].notna().sum() and df[col].notna().any():
                df[col] = conv
    return df
