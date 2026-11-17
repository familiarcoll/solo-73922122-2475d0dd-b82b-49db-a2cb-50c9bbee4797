"""Small FHIR formatting helpers."""
from __future__ import annotations


def etag(version: int) -> str:
    """FHIR version-aware ETag, e.g. W/"2"."""
    return f'W/"{version}"'


def parse_etag(value: str | None) -> int | None:
    """Parse an If-Match header value into a resource version integer."""
    if value is None:
        return None
    raw = value.strip()
    if raw.startswith("W/"):
        raw = raw[2:].strip()
    if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
        raw = raw[1:-1]
    try:
        return int(raw)
    except ValueError:
        return None


def meta_version(resource: dict, version: int) -> dict:
    """Return the resource with FHIR meta.versionId/lastUpdated populated."""
    body = dict(resource)
    meta = dict(body.get("meta") or {})
    meta["versionId"] = str(version)
    body["meta"] = meta
    body.pop("id", None)
    return body
