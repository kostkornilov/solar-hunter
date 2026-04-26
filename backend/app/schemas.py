from __future__ import annotations

from pydantic import BaseModel, Field


class EvaluateRequest(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    power_kw: float = Field(alias="P", ge=0.1, le=586.0)
    tariff_rub_kwh: float = Field(default=8.5, alias="tariff", gt=0)

    model_config = {
        "populate_by_name": True,
        "json_schema_extra": {
            "examples": [
                {
                    "lat": 55.7558,
                    "lon": 37.6176,
                    "P": 120.0,
                    "tariff": 8.5,
                }
            ]
        },
    }


class EvaluateResponse(BaseModel):
    request_id: str
    lat: float
    lon: float
    cf: float
    cf_percent: float
    cf_category: str
    cf_explanation: str
    capex_rub: float
    opex_year_rub: float
    revenue_year_rub: float
    payback_years: float | None
    payback_note: str

