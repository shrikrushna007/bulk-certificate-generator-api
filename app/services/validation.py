from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


@dataclass(frozen=True)
class ValidatedRecipient:
    name: str | None
    email: str | None
    error: str | None


def validate_recipient(name: Any, email: Any) -> ValidatedRecipient:
    if not isinstance(name, str):
        return ValidatedRecipient(None, _safe_email(email), "Recipient name must be a string.")

    if CONTROL_CHARACTERS.search(name):
        return ValidatedRecipient(
            None,
            _safe_email(email),
            "Recipient name contains unsupported characters.",
        )
    normalized_name = " ".join(unicodedata.normalize("NFKC", name).split())
    if not normalized_name:
        return ValidatedRecipient(None, _safe_email(email), "Recipient name must not be blank.")
    if len(normalized_name) > 200:
        return ValidatedRecipient(
            None,
            _safe_email(email),
            "Recipient name must not exceed 200 characters.",
        )

    if email is None:
        return ValidatedRecipient(normalized_name, None, None)
    if not isinstance(email, str):
        return ValidatedRecipient(normalized_name, None, "Recipient email must be a string.")

    normalized_email = email.strip().lower()
    if (
        not normalized_email
        or len(normalized_email) > 320
        or not EMAIL_PATTERN.fullmatch(normalized_email)
    ):
        return ValidatedRecipient(normalized_name, None, "Recipient email is not valid.")
    return ValidatedRecipient(normalized_name, normalized_email, None)


def _safe_email(value: Any) -> str | None:
    if isinstance(value, str):
        stripped = value.strip().lower()
        return stripped[:320] or None
    return None
