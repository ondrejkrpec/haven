"""Wishlist wire-shape validators; authentication and authorization remain separate."""

from datetime import datetime
import json
from pathlib import Path
import re

from jsonschema import Draft202012Validator, FormatChecker

SCHEMA = json.loads((Path(__file__).resolve().parents[1] / "schemas/wishlist-request-v1.schema.json").read_text())
FORMATS = FormatChecker()


@FORMATS.checks("date-time")
def timestamp(value):
    if not isinstance(value, str):
        return True  # The schema's type keyword rejects non-strings.
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})", value):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.utcoffset() is not None
    except ValueError:
        return False


REQUEST_VALIDATOR = Draft202012Validator(SCHEMA, format_checker=FORMATS)
COMMAND_VALIDATOR = Draft202012Validator(
    {"$schema": SCHEMA["$schema"], "$defs": SCHEMA["$defs"], "$ref": "#/$defs/command"},
    format_checker=FORMATS,
)
