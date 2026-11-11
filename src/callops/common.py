"""Shared identifiers, digests and safe application errors."""

import hashlib
import re
from datetime import datetime, timezone
from uuid import UUID


class Conflict(ValueError):
    """Safe error codes only; never include filenames, SQL or content."""


def utcnow():
    return datetime.now(timezone.utc)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def identifier(value):
    try:
        return UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise Conflict('INVALID_ID') from None


def valid_hash(value):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
        raise Conflict('INVALID_HASH')
    return value
