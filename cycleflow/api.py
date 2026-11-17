import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .contracts import DomainError
from .service import CycleService


class EventBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str
    external_id: str
    kind: str
    occurred_at: str
    payload: dict


class BatchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    events: list[dict]
    stop_after: int | None = None


class ReleaseBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    release_id: str
    product: str
    window_start: str
    window_end: str
    expected_fingerprint: str


def create_app(db_path=None):
    service = CycleService(db_path or os.environ.get("CYCLEFLOW_DB", "cycleflow.sqlite3"))
    app = FastAPI(title="CycleFlow", version="1.0.0")
    app.state.service = service

    @app.exception_handler(DomainError)
    async def domain_error(_request: Request, exc: DomainError):
        return JSONResponse(status_code=exc.status, content={"error": exc.code, "message": str(exc)})

    @app.get("/health")
    def health():
        return service.health()

    @app.post("/events")
    def ingest(body: EventBody):
        return service.ingest(body.model_dump())

    @app.get("/events/{event_id}")
    def event(event_id: int):
        return service.get_event(event_id)

    @app.get("/events/{event_id}/lineage")
    def lineage(event_id: int):
        return service.get_lineage(event_id)

    @app.post("/events/{event_id}/retry")
    def retry(event_id: int):
        return service.retry(event_id)

    @app.post("/events/retry-waiting")
    def retry_waiting(limit: int = 100):
        return service.retry_waiting(limit)

    @app.get("/quality/problems")
    def problems():
        return {"problems": service.problems()}

    @app.get("/quality/audit")
    def audit(product: str | None = None):
        return service.audit(product)

    @app.post("/batches/{batch_id}")
    def ingest_batch(batch_id: str, body: BatchBody):
        return service.ingest_batch(batch_id, body.events, body.stop_after)

    @app.get("/batches/{batch_id}")
    def batch(batch_id: str):
        return service.batches.status(batch_id)

    @app.get("/designs/{design_id}")
    def design(design_id: str):
        return service.get_design(design_id)

    @app.get("/lots/{lot_id}")
    def lot(lot_id: str):
        return service.get_lot(lot_id)

    @app.get("/jobs/{job_id}")
    def job(job_id: str):
        return service.get_job(job_id)

    @app.get("/jobs/{job_id}/timeline")
    def job_timeline(job_id: str):
        return service.job_timeline(job_id)

    @app.get("/designs/{design_id}/capacity")
    def capacity(design_id: str, quantity: int, at: str):
        return service.capacity(design_id, quantity, at)

    @app.get("/suppliers/exposure/{product}")
    def exposure(product: str, window_start: str, window_end: str):
        return service.supplier_exposure(product, window_start, window_end)

    @app.get("/tests/{test_id}")
    def test(test_id: str):
        return service.get_test(test_id)

    @app.get("/reports/{product}")
    def report(product: str, window_start: str, window_end: str):
        return service.report(product, window_start, window_end)

    @app.get("/reports/{product}/compare")
    def compare(product: str, first_start: str, first_end: str, second_start: str, second_end: str):
        return service.compare(product, first_start, first_end, second_start, second_end)

    @app.get("/reports/{product}/weekly")
    def weekly(product: str, window_start: str, window_end: str):
        return service.weekly(product, window_start, window_end)

    @app.get("/reports/{product}/bottlenecks")
    def bottlenecks(product: str, window_start: str, window_end: str):
        return service.bottlenecks(product, window_start, window_end)

    @app.get("/reports/{product}/cohorts")
    def cohorts(product: str, window_start: str, window_end: str):
        return service.cohorts(product, window_start, window_end)

    @app.post("/releases")
    def publish(body: ReleaseBody):
        return service.publish(body.release_id, body.product, body.window_start, body.window_end,
                               body.expected_fingerprint)

    @app.get("/releases/{release_id}")
    def release(release_id: str):
        return service.get_release(release_id)

    @app.get("/releases/{release_id}/freshness")
    def freshness(release_id: str):
        return service.shelf.freshness(release_id)

    return app


app = create_app()
