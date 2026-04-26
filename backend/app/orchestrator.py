from __future__ import annotations

from .model import CapacityFactorModel
from .providers import ProviderGate, collect_features_for_point
from .schemas import EvaluateRequest, EvaluateResponse


def calculate_payback(cf: float, power_kw: float, tariff: float) -> tuple[float, float, float, float | None, str]:
    capex = 75000 * power_kw
    opex_year = 1000 * power_kw
    revenue_year = cf * 8760 * power_kw * tariff
    delta = revenue_year - opex_year
    if delta <= 0:
        return capex, opex_year, revenue_year, None, "Проект не окупается при текущих параметрах."
    return capex, opex_year, revenue_year, capex / delta, "Окупаемость рассчитана."


def evaluate_point(
    *,
    request_id: str,
    payload: EvaluateRequest,
    model: CapacityFactorModel,
    gate: ProviderGate,
    settings,
) -> EvaluateResponse:
    features = collect_features_for_point(
        latitude=payload.lat,
        longitude=payload.lon,
        gate=gate,
        settings=settings,
    )
    cf = model.predict_cf(features)
    cf_category, cf_explanation = model.classify_cf(cf)
    capex, opex_year, revenue_year, pp, pp_note = calculate_payback(cf, payload.power_kw, payload.tariff_rub_kwh)

    return EvaluateResponse(
        request_id=request_id,
        lat=payload.lat,
        lon=payload.lon,
        cf=round(cf, 6),
        cf_percent=round(cf * 100, 2),
        cf_category=cf_category,
        cf_explanation=cf_explanation,
        capex_rub=round(capex, 2),
        opex_year_rub=round(opex_year, 2),
        revenue_year_rub=round(revenue_year, 2),
        payback_years=round(pp, 3) if pp is not None else None,
        payback_note=pp_note,
    )

