from __future__ import annotations

import logging
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool

from .config import Settings
from .logging_config import RequestLoggerAdapter, setup_logging
from .model import CapacityFactorModel
from .orchestrator import evaluate_point
from .providers import ProviderGate
from .schemas import EvaluateRequest, EvaluateResponse

settings = Settings()
setup_logging(settings.log_level)
logger = logging.getLogger("solarhunter")

app = FastAPI(title=settings.app_name, version=settings.app_version)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

model = CapacityFactorModel(
    model_path=settings.model_path,
    feature_columns_path=settings.feature_columns_path,
    index_params_path=settings.index_params_path,
)
gate = ProviderGate(settings)


@app.middleware("http")
async def with_request_id(request: Request, call_next):
    request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["x-request-id"] = request_id
    return response


@app.get("/health")
def health():
    return {"status": "ok", "service": settings.app_name, "version": settings.app_version}


@app.post("/v1/evaluate", response_model=EvaluateResponse)
async def evaluate(payload: EvaluateRequest, request: Request):
    request_id = request.state.request_id
    req_logger = RequestLoggerAdapter(logger, {"request_id": request_id})
    req_logger.info("evaluate request lat=%s lon=%s", payload.lat, payload.lon)
    try:
        result = await run_in_threadpool(
            evaluate_point,
            request_id=request_id,
            payload=payload,
            model=model,
            gate=gate,
            settings=settings,
        )
        req_logger.info("evaluate finished cf=%s payback=%s", result.cf, result.payback_years)
        return result
    except Exception as exc:  # noqa: BLE001
        req_logger.exception("evaluate failed: %s", exc)
        raise HTTPException(status_code=500, detail="Evaluation failed") from exc

