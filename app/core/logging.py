import json
import logging
from datetime import UTC, datetime

STRUCTURED_FIELDS = (
    "job_id",
    "certificate_id",
    "status",
    "total_count",
    "success_count",
    "failure_count",
    "duration_ms",
    "recovered_job_count",
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        event: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for field in STRUCTURED_FIELDS:
            if hasattr(record, field):
                event[field] = getattr(record, field)
        if record.exc_info:
            event["exception"] = self.formatException(record.exc_info)
        return json.dumps(event, ensure_ascii=False)


def configure_logging(level: str, log_format: str = "json") -> None:
    handler = logging.StreamHandler()
    if log_format == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
    logging.basicConfig(level=level, handlers=[handler], force=True)
