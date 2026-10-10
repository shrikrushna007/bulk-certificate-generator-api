# Requirements Traceability

This checklist maps assignment and reliability requirements to implementation and verification.

| Requirement | Implementation | Verification |
|---|---|---|
| Submit multiple recipients | JSON and streaming CSV job endpoints | `test_creates_and_completes_bulk_job`, `test_streaming_csv_import_creates_large_job_without_json_limit` |
| Validate job-level input | Pydantic schemas, JSON cap, CSV structure checks | Request validation and CSV header tests |
| Isolate invalid recipients | `validate_recipient` and failed recipient rows | Invalid-name, invalid-email, and unsupported-glyph tests |
| Generate professional PDFs | ReportLab renderer with wrapped/scaled text and explicit font coverage validation | PDF content, issue-date, long-name, and duplicate-name tests plus rendered visual review |
| Process outside request handling | FastAPI background task or Celery workers | Pending-state and distributed processor tests |
| Isolate one render failure | Per-recipient render, hash, and upload outcomes | 100-recipient fault-injection, local hash-failure, and distributed upload-failure tests |
| Persist status and progress | Normalized jobs and recipient records | Pending, processing, terminal, counter, recovery, all-invalid, and multiple-job tests |
| Bound large status responses | Summary mode and recipient pagination | Summary and pagination tests |
| Retrieve job-scoped PDFs | Job-and-certificate constrained query | Download, invalid ID, and cross-job tests |
| Handle missing artifacts | Safe 409/410 responses | Failed, missing, and checksum-race certificate tests |
| Prevent unsafe paths | UUID keys and confined local resolver | Storage traversal and filename tests |
| Make retries idempotent | Unique hashed key plus canonical request hash | JSON and CSV replay/conflict tests |
| Recover interrupted local jobs | Startup reconciliation | Local recovery test and restart exercise |
| Scale work across processes | PostgreSQL batch claims with `SKIP LOCKED` | Distributed batch claim integration test |
| Fence stale workers | Claim tokens, leases, attempt-specific object keys | Expired-claim recovery and stale-result rejection tests |
| Recover interrupted archives | Renewable archive leases, progress heartbeats, and periodic dispatch | Expired archive-claim recovery test |
| Detect file changes | Persisted SHA-256 plus verify/download checks | Integrity and tampering tests |
| Export all outcomes | Asynchronous ZIP, bounded concurrent artifact reads, short-lived database sessions, and complete CSV report | Archive content, progress, session-lifetime, and redundant-request regression tests |
| Use shared artifact storage | Local/S3 abstraction with managed transfers | S3 streamed-body cleanup test and live RustFS workflow |
| Provide operational context | Structured job lifecycle and failure logs | JSON logging test |
| Measure real operations | HTTP benchmark with CSV or JSON transport | `scripts/benchmark.py` and documented local results |
| Support reproducible setup | Two Compose profiles, migrations, examples | Compose configuration validation and README command review |
| Provide an evaluator-friendly interface | Built-in responsive operations console | Frontend route and static-asset integration test plus live HTTP workflow |
| Provide managed deployment infrastructure | AWS CDK stack for ALB, ECS, RDS, ElastiCache, S3, KMS, Secrets Manager, logs, and alarms | Successful CloudFormation deployment and live HTTPS workflow |

## Validation policy

Errors that make the overall job undefined return HTTP 422 without creating work. These include missing certificate details, an invalid date, an empty recipient collection, malformed JSON, or invalid CSV headers. Errors confined to one recipient create a failed recipient row while valid recipients continue.

## Scalability policy

The JSON cap is a request-safety control, not the service's capacity model. Large jobs use streaming CSV, with zero meaning no application-level CSV row cap. The distributed profile scales rendering by adding workers that claim bounded, non-overlapping PostgreSQL batches. Real capacity remains finite and must be established by load tests against the intended database, broker, object store, and worker resources.
