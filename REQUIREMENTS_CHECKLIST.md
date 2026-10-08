# Requirements Traceability

This checklist maps the assignment requirements to implementation components and automated tests.

| Requirement | Implementation | Verification |
|---|---|---|
| Accept one request containing many recipients | `POST /api/v1/jobs` and `JobCreateRequest` | `test_creates_and_completes_bulk_job` |
| Validate job-level input | Pydantic request schemas and the configured recipient limit | `test_request_level_validation_returns_consistent_422`, `test_recipient_limit_is_enforced`, `test_extra_fields_are_rejected` |
| Isolate invalid recipient data | `validate_recipient` and failed `CertificateRecord` rows | `test_invalid_recipient_isolated_from_valid_recipient`, `test_missing_and_wrong_type_names_are_individual_failures`, `test_all_invalid_recipients_produce_failed_job` |
| Generate one certificate per valid recipient | `CertificateRenderer` predefined ReportLab template | `test_generated_pdf_is_retrievable_and_contains_expected_text` |
| Handle long and duplicate recipient names | Wrapped/scaled text and identifier-based filenames | `test_long_recipient_name_generates_valid_pdf`, `test_duplicate_names_generate_distinct_certificates` |
| Process outside the request handler | FastAPI `BackgroundTasks` and `JobProcessor` | Creation response asserts `PENDING`; status tests assert terminal transitions |
| Continue after an individual rendering failure | Per-future exception boundary in the bounded render pool | `test_rendering_failure_does_not_stop_other_recipients` |
| Persist job status and recipient results | SQLAlchemy job and certificate record models | Status, failure isolation, and multi-job integration tests |
| Report consistent counts and progress | Persisted success/failure counts plus derived pending/progress values | `test_creates_and_completes_bulk_job`, `test_unprocessed_job_exposes_pending_status_and_consistent_counts` |
| Keep large status responses bounded | Optional summary-only response and ordered recipient pagination | `test_status_supports_lightweight_summary`, `test_status_paginates_recipient_outcomes` |
| Retrieve a generated certificate | Job-scoped PDF download endpoint | `test_generated_pdf_is_retrievable_and_contains_expected_text` |
| Reject invalid job and certificate IDs | Safe 404 API errors | `test_unknown_job_returns_404`, `test_unknown_certificate_returns_404` |
| Prevent cross-job certificate access | Query constrained by both job ID and certificate ID | `test_cross_job_certificate_access_is_rejected` |
| Handle missing or failed artifacts | 409 for failed generation and 410 for missing files | `test_failed_certificate_cannot_be_downloaded`, `test_missing_generated_file_returns_410` |
| Avoid unsafe filenames and path traversal | UUID filenames and confined path resolution | `test_generated_files_use_confined_identifier_based_paths`, `test_storage_rejects_path_traversal` |
| Support independent jobs | UUID primary keys and job-scoped records | `test_multiple_jobs_are_independent` |
| Provide setup, execution, test, and API documentation | `README.md`, `.env.example`, sample request | Manual command review and runtime smoke test |

## Requirement interpretation

The API separates validation into two levels. Errors that make the overall job undefined, such as a missing certificate definition, malformed recipient collection, empty recipient list, or oversized request, return HTTP 422 and do not create a job. Errors confined to a recipient, such as a missing or invalid name or email, create a failed recipient record while the remaining recipients continue.

Background generation is an assignment-appropriate in-process mechanism. A bounded worker pool renders independent PDFs concurrently, while one coordinator persists batch outcomes to avoid concurrent SQLite writers within a job. It is not a durable or distributed queue and is deliberately documented as such. Generated PDF bytes remain on disk; the database stores metadata and relative paths rather than binary objects.
