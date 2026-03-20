# dataProject
Data Project

## Herbarium ETL Feature: Deterministic Specimen UUID

The ETL pipeline uses deterministic UUID generation for `ci_herbarium_specimens.id` to keep reruns idempotent and version-friendly.

- `specimen_id` is computed in `herbarium_project/dags/mappings.py` via `compute_specimen_id(barcode, task_uuid)`.
- Primary rule: if `barcode` is present, use `uuid.uuid5(NAMESPACE_UUID, barcode.strip().lower())`.
- Fallback rule: if `barcode` is missing, use the source `herbarium_tasks.id` (task UUID).

### Why this matters

- Reprocessing the same barcode maps to the same specimen UUID.
- New versions are added in `ci_specimen_dtl` using incremented `seq_num`.
- Parent upserts in `ci_herbarium_specimens` remain stable across repeated DAG runs.
