import hashlib
from contextvars import ContextVar
request_id = ContextVar('request_id', default=None)
from datetime import datetime, timezone

from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse


class APIError(Exception):
    def __init__(self, status, message, **extra):
        self.status = status
        self.body = {"message": message, **extra}


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def iso(value=None):
    return (value or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def reply(value, status=200):
    return JSONResponse(jsonable_encoder(value, custom_encoder={datetime: iso}), status_code=status)
