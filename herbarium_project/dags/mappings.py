import json
import uuid
from typing import Any, Dict, Mapping, Optional


# Stable namespace so specimen_id can be derived deterministically from business identifiers
# (e.g., barcode). This enables versioning when the same specimen is re-processed.
NAMESPACE_UUID = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")


# Map herbarium_tasks columns to the keys stored in ci_specimen_dtl.taxonomy_metadata.
TASK_COLUMN_TO_TAXONOMY_KEY: Dict[str, str] = {
    "family": "family_name",
    "genus": "genus_name",
    "species": "species_name",
    "common_name": "common_name",
    "collector_name": "collector_name",
    "collection_date": "collection_date",
    "location": "location",
    "barcode": "barcode",
    "determiner": "determiner",
    "habitat": "habitat",
    # Useful for debugging/traceability.
    "collection_number": "collection_number",
    "accession_number": "accession_number",
    "accession_date": "accession_date",
    "department": "department",
    "latitude": "latitude",
    "longitude": "longitude",
    "altitude": "altitude",
}


def compute_specimen_id(barcode: Optional[str], fallback_task_uuid: uuid.UUID) -> uuid.UUID:
    """
    Derive a stable specimen_id from barcode.
    If barcode is missing, fall back to the task UUID (idempotency-friendly for local dev).
    """
    if barcode:
        normalized = barcode.strip().lower()
        return uuid.uuid5(NAMESPACE_UUID, normalized)
    return fallback_task_uuid


def parse_json_text(value: Any) -> Optional[Dict[str, Any]]:
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def build_taxonomy_metadata(task_row: Mapping[str, Any], taxonomy_json: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Build the JSONB payload for ci_specimen_dtl.taxonomy_metadata.

    Priority:
    1) Explicit columns from herbarium_tasks (mapped via TASK_COLUMN_TO_TAXONOMY_KEY)
    2) Fallback to values present inside taxonomy_data JSON (if available)
    """
    payload: Dict[str, Any] = {}

    # 1) Column mappings
    for src_col, dst_key in TASK_COLUMN_TO_TAXONOMY_KEY.items():
        payload[dst_key] = task_row.get(src_col)

    # 2) Merge mapped keys from taxonomy_data JSON if present.
    if taxonomy_json:
        for src_col, dst_key in TASK_COLUMN_TO_TAXONOMY_KEY.items():
            # taxonomy_data sometimes uses different key casing/naming; try both.
            payload.setdefault(dst_key, taxonomy_json.get(src_col) or taxonomy_json.get(src_col.title()) or taxonomy_json.get(dst_key))

    # Keep the raw taxonomy_data for auditing (and to help future mapping improvements).
    payload["taxonomy_data_raw"] = taxonomy_json
    return payload


def _to_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    return str(value)


def build_parent_specimen_payload(task_row: Mapping[str, Any]) -> Dict[str, Any]:
    """
    Map herbarium_tasks columns into ci_herbarium_specimens fields.

    This keeps parent table columns populated for search/index use-cases.
    """
    genus = (task_row.get("genus") or "").strip()
    species = (task_row.get("species") or "").strip()
    specimen_name = " ".join(part for part in [genus, species] if part) or None

    source_status = (task_row.get("status") or "").strip().upper()
    status_map = {
        "COMPLETED": "COMPLETED",
        "MIGRATED": "MIGRATED",
        "DLQ_IMAGE_CORRUPTED": "DLQ_IMAGE",
        "PENDING_REVIEW": "PENDING",
        "RAW_UPLOAD": "RAW_UPLOAD",
        "ASSIGNED": "ASSIGNED",
    }
    specimen_status = status_map.get(source_status, source_status[:10] or None)

    return {
        "pk_hrb_specimen_id": task_row.get("barcode"),
        "specimen_name": specimen_name,
        "vernacular_name": task_row.get("common_name"),
        "family_name": task_row.get("family"),
        # Target schema uses 'genius_name'
        "genius_name": task_row.get("genus"),
        "species_name": task_row.get("species"),
        "accession_number": task_row.get("accession_number"),
        "collection_no": task_row.get("collection_number"),
        "collector_name": task_row.get("collector_name"),
        "determiner_name": task_row.get("determiner"),
        "collection_date": task_row.get("collection_date"),
        "locality_name": task_row.get("location"),
        "collection_latitude": _to_text(task_row.get("latitude")),
        "collection_longitude": _to_text(task_row.get("longitude")),
        "collection_altitude": _to_text(task_row.get("altitude")),
        "barcode": task_row.get("barcode"),
        "taxonomy_data": task_row.get("taxonomy_data"),
        "original_image": task_row.get("filename"),
        "specimen_status": specimen_status,
    }

