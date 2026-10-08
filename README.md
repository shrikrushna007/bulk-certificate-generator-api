# Bulk Certificate Generator API

Bulk Certificate Generator API is a FastAPI service that accepts one job containing multiple recipients, generates an individual PDF certificate for every valid recipient, records recipient-level outcomes, and exposes job progress and certificate downloads.

The design favors a self-contained local setup: SQLite stores job metadata and ReportLab writes PDFs to local file storage. Invalid recipients and rendering failures are isolated so they do not prevent other certificates in the same job from completing.

## Features

- Bulk job creation with HTTP 202 responses
- Request-level and recipient-level validation
- A predefined, landscape PDF certificate template
- Persistent job status, counters, timestamps, and recipient outcomes
- `PENDING`, `PROCESSING`, `COMPLETED`, `PARTIALLY_COMPLETED`, and `FAILED` job states
- Job-scoped certificate downloads with cross-job access protection
- UUID-based filenames and storage path confinement
- Consistent safe JSON errors without stack-trace disclosure
- Alembic migrations, Docker configuration, automated tests, Ruff, and GitHub Actions CI

## Technology stack

- Python 3.11+
- FastAPI and Pydantic v2
- SQLAlchemy 2.x and SQLite
- Alembic
- ReportLab
- pytest, FastAPI TestClient, httpx, and pypdf
- Ruff
- Docker and Docker Compose

## Architecture

```mermaid
flowchart LR
    Client --> API[FastAPI routes and schemas]
    API --> Jobs[Job service]
    Jobs --> DB[(SQLite)]
    API --> Background[FastAPI background task]
    Background --> Processor[Job processor]
    Processor --> Pool[Bounded render worker pool]
    Pool --> Renderer[ReportLab renderer]
    Processor --> DB
    Renderer --> Storage[(Local PDF storage)]
    API --> Storage
```

HTTP routes validate and translate requests but do not contain certificate-generation logic. `JobService` owns job persistence, `JobProcessor` coordinates an application-wide bounded render pool and batched status updates, `CertificateRenderer` owns the single PDF design, and `CertificateStorage` confines all artifact paths beneath the configured storage root. Only each job coordinator writes its results, avoiding concurrent SQLite writers within a job while the global pool prevents thread growth as jobs arrive.

## Project structure

```text
.
|-- app/
|   |-- api/                 # Routes and FastAPI dependencies
|   |-- core/                # Configuration and logging
|   |-- db/                  # SQLAlchemy base, models, and sessions
|   |-- schemas/             # Pydantic request and response models
|   |-- services/            # Validation, processing, rendering, and storage
|   `-- main.py              # Application factory and exception handlers
|-- migrations/              # Alembic environment and initial schema
|-- sample_requests/         # Copy-ready API payloads
|-- tests/                   # Isolated API and service integration tests
|-- .env.example
|-- compose.yaml
|-- Dockerfile
|-- pyproject.toml
`-- REQUIREMENTS_CHECKLIST.md
```

## Data model

`certificate_jobs` stores certificate-wide data, lifecycle status, counters, and timestamps. `certificate_records` stores one row per submitted recipient, including its stable input order, validation or generation outcome, safe error message, and relative PDF path. A foreign key with cascade deletion associates records with their job, and a unique constraint prevents duplicate recipient positions within one job.

PDF data is not stored in SQLite. Files use generated job and certificate UUIDs, so duplicate or filename-like recipient names cannot collide or control filesystem paths.

## API endpoints

| Method | Path | Purpose | Success |
|---|---|---|---|
| `GET` | `/health` | Liveness check | `200` |
| `POST` | `/api/v1/jobs` | Create a bulk certificate job | `202` |
| `GET` | `/api/v1/jobs/{job_id}` | Read progress and paginated recipient outcomes | `200` |
| `GET` | `/api/v1/jobs/{job_id}/certificates/{certificate_id}` | Download a completed PDF | `200` |

Interactive OpenAPI documentation is available at `http://localhost:8000/docs` while the application is running.

## Local development

### Prerequisites

- Python 3.11 or newer
- Git
- Docker Desktop or another Docker-compatible runtime only for the container workflow

### Install

On macOS or Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e ".[dev]"
```

On Windows PowerShell:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[dev]"
```

No environment variables are required for local development. Copy `.env.example` to `.env` only when overriding defaults:

```bash
cp .env.example .env
```

PowerShell equivalent:

```powershell
Copy-Item .env.example .env
```

| Variable | Required | Default | Description |
|---|---|---|---|
| `APP_ENV` | No | `development` | Runtime environment label |
| `DATABASE_URL` | No | `sqlite:///./data/certificates.db` | SQLAlchemy database URL |
| `STORAGE_DIR` | No | `./data/certificates` | Root directory for generated PDFs |
| `MAX_RECIPIENTS` | No | `500` | Maximum recipients accepted in one job |
| `RENDER_WORKERS` | No | `4` | Maximum PDFs rendered concurrently across the application |
| `PROCESSING_BATCH_SIZE` | No | `100` | Outcomes persisted in each database transaction |
| `LOG_LEVEL` | No | `INFO` | Python logging level |

Apply the database migration:

```bash
alembic upgrade head
```

Start one application worker:

```bash
uvicorn app.main:app --reload
```

For a production-style local command, omit `--reload`:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## Complete API workflow

Create a job with the included sample request:

```bash
curl -i -X POST http://localhost:8000/api/v1/jobs \
  -H "Content-Type: application/json" \
  --data @sample_requests/create_job.json
```

Example response:

```json
{
  "job_id": "03bd7f9c-0c5d-42e3-8f34-6d86256c4693",
  "status": "PENDING",
  "total_recipients": 2,
  "status_url": "/api/v1/jobs/03bd7f9c-0c5d-42e3-8f34-6d86256c4693"
}
```

Poll job status using the returned URL:

```bash
curl http://localhost:8000/api/v1/jobs/03bd7f9c-0c5d-42e3-8f34-6d86256c4693
```

For frequent progress polling, omit recipient outcomes to keep the response small:

```bash
curl "http://localhost:8000/api/v1/jobs/03bd7f9c-0c5d-42e3-8f34-6d86256c4693?include_recipients=false"
```

Recipient outcomes are ordered by their original input position and paginated with `recipient_offset` and `recipient_limit`. The default page contains 100 outcomes and the maximum page size is 500:

```bash
curl "http://localhost:8000/api/v1/jobs/03bd7f9c-0c5d-42e3-8f34-6d86256c4693?recipient_offset=100&recipient_limit=100"
```

A completed recipient includes its certificate ID and download URL:

```json
{
  "job_id": "03bd7f9c-0c5d-42e3-8f34-6d86256c4693",
  "status": "COMPLETED",
  "certificate_title": "Advanced Backend Engineering",
  "issuing_organization": "AEREO Learning",
  "issue_date": "2026-10-08",
  "total_recipients": 2,
  "successful_generations": 2,
  "failed_generations": 0,
  "pending_generations": 0,
  "progress_percentage": 100.0,
  "created_at": "2026-10-08T13:30:00Z",
  "started_at": "2026-10-08T13:30:00Z",
  "completed_at": "2026-10-08T13:30:01Z",
  "recipients": [
    {
      "certificate_id": "86f8f39b-a37a-4411-90df-cc59cd201b66",
      "recipient_index": 0,
      "recipient_name": "Aarav Sharma",
      "recipient_email": "aarav.sharma@example.com",
      "status": "COMPLETED",
      "error_message": null,
      "download_url": "/api/v1/jobs/03bd7f9c-0c5d-42e3-8f34-6d86256c4693/certificates/86f8f39b-a37a-4411-90df-cc59cd201b66"
    }
  ],
  "recipient_page": {
    "offset": 0,
    "limit": 100,
    "returned": 2,
    "total": 2
  }
}
```

Download a certificate:

```bash
curl -o certificate.pdf \
  http://localhost:8000/api/v1/jobs/03bd7f9c-0c5d-42e3-8f34-6d86256c4693/certificates/86f8f39b-a37a-4411-90df-cc59cd201b66
```

The identifiers above are illustrative. Use the values returned by the running API.

## Validation and partial failures

The request envelope must contain a valid certificate definition and a non-empty recipient list within `MAX_RECIPIENTS`. Unknown fields, invalid dates, malformed recipient collections, and missing certificate-wide fields return HTTP 422 without creating a job.

Recipient objects are evaluated independently. A missing, non-string, blank, control-character-containing, or over-200-character name fails that recipient. An optional email must be a plausible address no longer than 320 characters. Invalid recipients are visible in job status with a safe failure message, while valid recipients continue processing. A job is `PARTIALLY_COMPLETED` when at least one certificate succeeds and at least one fails.

## Error responses

Errors use a consistent envelope:

```json
{
  "error": {
    "code": "certificate_not_found",
    "message": "The requested certificate does not belong to this job."
  }
}
```

The download endpoint returns `404` for unknown or cross-job identifiers, `409` when generation failed or is incomplete, and `410` when the database references a completed artifact that is missing from storage.

## Tests and linting

```bash
ruff check .
pytest -q
```

To include coverage:

```bash
pytest --cov=app --cov-report=term-missing
```

Tests create an isolated temporary SQLite database and storage directory for each test. The suite exercises real HTTP endpoints, SQLAlchemy persistence, actual ReportLab output, and PDF text extraction. Renderer failures are injected only where failure isolation itself is under test.

## Performance characteristics

The processor fetches pending recipients in bounded batches, renders each batch through one application-wide configurable thread pool, and writes all outcomes from that batch in one database transaction. Job creation also inserts recipient rows in bulk. SQLite uses WAL mode, normal synchronization, and a busy timeout to allow progress reads while generation is active.

A Docker Desktop benchmark on the development machine generated 5,000 one-page certificates with four render workers and a batch size of 100:

| Measurement | Sequential implementation | Batched concurrent implementation |
|---|---:|---:|
| Request acceptance | 1.22 seconds | 0.80 seconds |
| Generation duration | 73.6 seconds | 18.9 seconds |
| Successful PDFs | 5,000 | 5,000 |
| Failed PDFs | 0 | 0 |

This is a local comparison rather than a throughput guarantee. Results depend on CPU allocation, storage performance, template complexity, and recipient data. The first, middle, and final PDFs from the optimized run were downloaded and text-validated.

## Docker

Build and start the API:

```bash
docker compose up --build
```

The container applies migrations before starting Uvicorn, runs as a non-root user, and persists the SQLite database and generated PDFs in the `certificate_data` volume.

Compose forwards the processing settings from `.env`. For a deliberate 5,000-recipient local test, use values such as:

```dotenv
MAX_RECIPIENTS=5000
RENDER_WORKERS=4
PROCESSING_BATCH_SIZE=100
```

Increase `RENDER_WORKERS` only after measuring the host. More threads can reduce performance when CPU or storage is already saturated.

Stop the service:

```bash
docker compose down
```

Add `-v` only when intentionally deleting the persistent Docker volume and all generated data.

## Design decisions and trade-offs

- **In-process background execution:** FastAPI background tasks keep the create request responsive without adding Redis or a worker service. Work is not durable: terminating the process can interrupt a job, and automatic recovery is not implemented. Run one worker for predictable local processing.
- **Bounded parallel rendering:** Independent PDFs are rendered concurrently, while a single coordinator persists results. Each future captures its own failure, so one broken recipient does not cancel the batch.
- **Batched commits:** Outcomes are committed once per configurable batch instead of once per certificate. This substantially reduces SQLite transaction overhead; a process failure can leave up to one batch requiring reconciliation.
- **SQLite and local storage:** They make evaluation simple and credential-free. WAL mode improves concurrent status reads, but SQLite still serializes writes and local files are not shared across hosts, so this deployment is intended for one application instance.
- **Paginated status:** Lightweight summary polling avoids loading or serializing thousands of recipient records. Outcome pages remain available when detailed inspection is needed.
- **Relative paths instead of PDF blobs:** The database remains small and queryable while the storage layer enforces a single trusted root.
- **Derived progress:** Pending count and percentage derive from persisted totals, successes, and failures, reducing the number of independently mutable counters.

## Known limitations

- Background tasks are not durable and do not resume automatically after process termination.
- The local SQLite and filesystem design is not intended for horizontally scaled deployment.
- Concurrency improves throughput within one process but does not provide distributed execution or cross-host coordination.
- There is no authentication or authorization; deploy behind an appropriate access-control layer when certificates contain private data.
- Generated files have no automatic retention or cleanup policy.
- The predefined template uses ReportLab core fonts. Complex writing systems require a licensed Unicode font to be bundled and registered.
- Bulk ZIP download, cancellation, retry, and webhooks are outside the required scope.

## Future improvements

- Add startup reconciliation and a durable queue for restart-safe processing.
- Move artifacts to object storage and use short-lived signed download URLs.
- Add authentication, ownership checks, and audit events.
- Add retention policies, job cancellation, explicit retries, and idempotency keys.
- Bundle a broad Unicode font and add multilingual rendering tests.
- Add metrics and tracing for queue latency, per-batch render duration, and failure rates.

## Implementation insights

The central reliability concern is not rendering a PDF; it is preserving accurate job state when validation or rendering fails midway through a bulk request. Separating request-level validation from recipient-level outcomes makes partial success explicit. Bounded rendering concurrency improves throughput while a single database writer preserves counter consistency. Identifier-based storage also removes recipient input from the filesystem trust boundary.
