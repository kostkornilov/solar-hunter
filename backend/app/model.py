from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
from catboost import CatBoostRegressor


class CapacityFactorModel:
    def __init__(self, model_path: Path, feature_columns_path: Path, index_params_path: Path):
        self.model = CatBoostRegressor()
        self.model.load_model(str(model_path))
        self.feature_columns = joblib.load(str(feature_columns_path))
        with open(index_params_path, "r", encoding="utf-8") as f:
            self.index_params = json.load(f)

    def build_features(self, raw: dict[str, Any]) -> pd.DataFrame:
        df = pd.DataFrame([raw])
        if "latitude" in df.columns:
            df["lat_sin"] = np.sin(np.radians(df["latitude"]))
            df["lat_cos"] = np.cos(np.radians(df["latitude"]))
        if "longitude" in df.columns:
            df["lon_sin"] = np.sin(np.radians(df["longitude"]))
            df["lon_cos"] = np.cos(np.radians(df["longitude"]))

        for col in ("latitude", "longitude", "name"):
            if col in df.columns:
                df = df.drop(columns=[col])

        for col in self.feature_columns:
            if col not in df.columns:
                df[col] = np.nan

        df = df[self.feature_columns]
        df = df.replace([np.inf, -np.inf], np.nan).fillna(0)
        return df

    def predict_cf(self, raw_features: dict[str, Any]) -> float:
        x = self.build_features(raw_features)
        return float(self.model.predict(x)[0])

    def classify_cf(self, cf: float) -> tuple[str, str]:
        low = self.index_params["low_threshold"]
        high = self.index_params["high_threshold"]
        if cf < low:
            return "low", f"Низкий CF: ниже {low * 100:.1f}%."
        if cf < high:
            return "medium", f"Средний CF: от {low * 100:.1f}% до {high * 100:.1f}%."
        return "high", f"Высокий CF: выше {high * 100:.1f}%."

