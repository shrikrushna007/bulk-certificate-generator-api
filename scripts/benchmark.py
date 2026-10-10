from __future__ import annotations

import argparse
import csv
import io
import json
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import date

import httpx


@dataclass(frozen=True)
class BenchmarkResult:
    job_id: str
    recipients: int
    acceptance_seconds: float
    completion_seconds: float
    successful: int
    failed: int
    certificates_per_second: float


def run_case(
    client: httpx.Client,
    base_url: str,
    recipient_count: int,
    max_wait_seconds: float,
    transport: str,
) -> BenchmarkResult:
    started = time.perf_counter()
    idempotency_headers = {
        "Idempotency-Key": f"benchmark-{transport}-{recipient_count}-{uuid.uuid4()}"
    }
    if transport == "csv":
        with tempfile.TemporaryFile(mode="w+b") as upload:
            text_upload = io.TextIOWrapper(upload, encoding="utf-8", newline="")
            writer = csv.writer(text_upload)
            writer.writerow(["name", "email"])
            for index in range(1, recipient_count + 1):
                writer.writerow([f"Benchmark Recipient {index:09d}", ""])
            text_upload.flush()
            upload.seek(0)
            response = client.post(
                f"{base_url}/api/v1/jobs/import",
                data={
                    "title": "Certificate Generation Benchmark",
                    "issuing_organization": "Local Performance Test",
                    "issue_date": date.today().isoformat(),
                },
                files={"recipients": ("recipients.csv", upload, "text/csv")},
                headers=idempotency_headers,
            )
            text_upload.detach()
    else:
        payload = {
            "certificate": {
                "title": "Certificate Generation Benchmark",
                "issuing_organization": "Local Performance Test",
                "issue_date": date.today().isoformat(),
            },
            "recipients": [
                {"name": f"Benchmark Recipient {index:09d}"}
                for index in range(1, recipient_count + 1)
            ],
        }
        response = client.post(
            f"{base_url}/api/v1/jobs",
            json=payload,
            headers=idempotency_headers,
        )
    acceptance_seconds = time.perf_counter() - started
    response.raise_for_status()
    status_url = response.json()["status_url"]

    while True:
        status_response = client.get(
            f"{base_url}{status_url}",
            params={"include_recipients": "false"},
        )
        status_response.raise_for_status()
        job = status_response.json()
        if job["status"] not in {"PENDING", "PROCESSING"}:
            break
        if time.perf_counter() - started > max_wait_seconds:
            raise TimeoutError(
                f"Job {response.json()['job_id']} did not finish within "
                f"{max_wait_seconds:g} seconds."
            )
        time.sleep(0.1)

    completion_seconds = time.perf_counter() - started
    throughput = job["successful_generations"] / completion_seconds
    return BenchmarkResult(
        job_id=response.json()["job_id"],
        recipients=recipient_count,
        acceptance_seconds=round(acceptance_seconds, 3),
        completion_seconds=round(completion_seconds, 3),
        successful=job["successful_generations"],
        failed=job["failed_generations"],
        certificates_per_second=round(throughput, 2),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark the running API with real HTTP, database, and PDF operations."
    )
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--sizes", nargs="+", type=int, default=[10, 100, 1000])
    parser.add_argument("--max-wait", type=float, default=600.0)
    parser.add_argument("--transport", choices=["csv", "json"], default="csv")
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if any(size < 1 for size in args.sizes):
        raise SystemExit("Every benchmark size must be a positive integer.")
    if args.max_wait <= 0:
        raise SystemExit("--max-wait must be greater than zero.")

    base_url = args.base_url.rstrip("/")
    results: list[BenchmarkResult] = []
    with httpx.Client(timeout=60) as client:
        health = client.get(f"{base_url}/health")
        health.raise_for_status()
        for size in args.sizes:
            results.append(run_case(client, base_url, size, args.max_wait, args.transport))

    if args.as_json:
        print(json.dumps([asdict(result) for result in results], indent=2))
        return

    print("| Job ID | Recipients | Acceptance (s) | Completion (s) | Success | Failed | PDFs/s |")
    print("|---|---:|---:|---:|---:|---:|---:|")
    for result in results:
        print(
            f"| `{result.job_id}` | {result.recipients:,} | "
            f"{result.acceptance_seconds:.3f} | "
            f"{result.completion_seconds:.3f} | {result.successful:,} | "
            f"{result.failed:,} | {result.certificates_per_second:.2f} |"
        )


if __name__ == "__main__":
    main()
