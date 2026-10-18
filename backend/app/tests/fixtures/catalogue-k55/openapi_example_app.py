from __future__ import annotations

from fastapi import FastAPI

app = FastAPI(title="Lynewright OpenAPI integration example", version="1.0.0")
_polls: dict[str, int] = {}


@app.get("/health", operation_id="health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/jobs", operation_id="startJob")
def start_job(payload: dict[str, str]) -> dict[str, str]:
    job_id = payload.get("value", "")
    if not job_id:
        job_id = "default-job"
    _polls[job_id] = 0
    return {"job_id": job_id, "status": "accepted"}


@app.get("/jobs/{job_id}", operation_id="jobStatus")
def job_status(job_id: str) -> dict[str, str]:
    count = _polls.get(job_id, 0) + 1
    _polls[job_id] = count
    return {"job_id": job_id, "status": "pending" if count == 1 else "done"}
