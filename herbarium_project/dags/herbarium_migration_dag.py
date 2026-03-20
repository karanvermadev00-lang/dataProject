import datetime as dt
import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Set, Tuple

from airflow import DAG
from airflow.operators.python import PythonOperator
from airflow.providers.postgres.hooks.postgres import PostgresHook

from mappings import (
    build_parent_specimen_payload,
    build_taxonomy_metadata,
    compute_specimen_id,
    parse_json_text,
)

try:
    # Airflow adds the plugins folder to sys.path in the container.
    from image_utils import create_optimized_icon, resolve_icon_output_path
except ImportError:  # pragma: no cover
    from plugins.image_utils import create_optimized_icon, resolve_icon_output_path


POSTGRES_CONN_ID = "postgres_default"


def _resolve_original_image_path(original_dir: str, image_url: Optional[str], filename: Optional[str]) -> str:
    # Prefer `filename` for stable mapping.
    if filename:
        candidate = os.path.join(original_dir, os.path.basename(filename))
        if os.path.exists(candidate):
            return candidate

    # Fallback: parse basename from URL.
    if image_url:
        base = os.path.basename(image_url.split("?")[0])
        candidate = os.path.join(original_dir, base)
        if os.path.exists(candidate):
            return candidate

    # Last resort: assume filename under original dir even if missing.
    if filename:
        return os.path.join(original_dir, os.path.basename(filename))
    raise FileNotFoundError("Cannot resolve original image path (missing filename and/or image_url).")


def _iter_completed_batches(
    hook: PostgresHook,
    batch_size: int,
    only_missing_in_dtl: bool,
    statuses: List[str],
) -> Any:
    """
    LIMIT-style chunking (1,000+) with stable keyset pagination.

    Keyset is used to avoid pagination inconsistencies while this DAG inserts into `ci_specimen_dtl`.
    """
    conn = hook.get_conn()

    last_created_at = dt.datetime(1970, 1, 1)
    last_id = uuid.UUID(int=0)

    while True:
        with conn.cursor() as cur:
            missing_clause = ""
            if only_missing_in_dtl:
                missing_clause = "AND NOT EXISTS (SELECT 1 FROM ci_specimen_dtl d WHERE d.source_task_id = t.id)"

            cur.execute(
                """
                SELECT
                    t.id,
                    t.barcode,
                    t.filename,
                    t.image_url,
                    t.status,
                    t.created_at,
                    t.genus,
                    t.species,
                    t.family,
                    t.common_name,
                    t.collector_name,
                    t.collection_date,
                    t.location,
                    t.taxonomy_data,
                    t.metadata,
                    t.notes,
                    t.collection_number,
                    t.accession_number,
                    t.accession_date,
                    t.department,
                    t.latitude,
                    t.longitude,
                    t.altitude,
                    t.determiner,
                    t.habitat,
                    t.validated_at
                FROM herbarium_tasks t
                WHERE t.status = ANY(%s::text[])
                  {missing_clause}
                  AND (t.created_at > %s OR (t.created_at = %s AND t.id > %s))
                ORDER BY t.created_at ASC, t.id ASC
                LIMIT %s
                """.replace("{missing_clause}", missing_clause),
                (statuses, last_created_at, last_created_at, last_id, batch_size),
            )
            rows = cur.fetchall()
            if not rows:
                break

            colnames = [desc[0] for desc in cur.description]
            batch: List[Dict[str, Any]] = []
            for r in rows:
                batch.append({colnames[i]: r[i] for i in range(len(colnames))})

            last_created_at = batch[-1]["created_at"]
            last_id = batch[-1]["id"]
            yield batch


def _iter_completed_batches_offset(hook: PostgresHook, batch_size: int) -> Any:
    """
    Explicit LIMIT/OFFSET chunking for Task A.

    Task A only depends on `herbarium_tasks.status` (not on dtl existence), so OFFSET is stable.
    """
    conn = hook.get_conn()
    parent_statuses = os.getenv(
        "HERBARIUM_PARENT_SOURCE_STATUSES",
        "COMPLETED,MIGRATED,DLQ_IMAGE_CORRUPTED",
    )
    statuses = [s.strip() for s in parent_statuses.split(",") if s.strip()]
    offset = 0

    while True:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    t.id,
                    t.barcode,
                    t.filename,
                    t.image_url,
                    t.status,
                    t.created_at,
                    t.genus,
                    t.species,
                    t.family,
                    t.common_name,
                    t.collector_name,
                    t.collection_date,
                    t.location,
                    t.taxonomy_data,
                    t.metadata,
                    t.notes,
                    t.collection_number,
                    t.accession_number,
                    t.accession_date,
                    t.department,
                    t.latitude,
                    t.longitude,
                    t.altitude,
                    t.determiner,
                    t.habitat,
                    t.validated_at
                FROM herbarium_tasks t
                WHERE t.status = ANY(%s::text[])
                ORDER BY t.created_at ASC, t.id ASC
                LIMIT %s OFFSET %s
                """,
                (statuses, batch_size, offset),
            )
            rows = cur.fetchall()
            if not rows:
                break

            colnames = [desc[0] for desc in cur.description]
            batch: List[Dict[str, Any]] = []
            for r in rows:
                batch.append({colnames[i]: r[i] for i in range(len(colnames))})

        yield batch
        offset += batch_size


def migration_parent_task(**context) -> None:
    """
    Task A:
    Ensure ci_herbarium_specimens has one parent row per specimen_id.
    """
    hook = PostgresHook(postgres_conn_id=POSTGRES_CONN_ID)

    batch_size = int(os.getenv("HERBARIUM_BATCH_SIZE", "1000"))

    parent_upsert_sql = """
        INSERT INTO ci_herbarium_specimens (
            id,
            pk_hrb_specimen_id,
            specimen_name,
            vernacular_name,
            family_name,
            genius_name,
            species_name,
            accession_number,
            collection_no,
            collector_name,
            determiner_name,
            collection_date,
            locality_name,
            collection_latitude,
            collection_longitude,
            collection_altitude,
            barcode,
            taxonomy_data,
            original_image,
            specimen_status,
            updated_at
        )
        VALUES %s
        ON CONFLICT (id) DO UPDATE
        SET
            pk_hrb_specimen_id = COALESCE(EXCLUDED.pk_hrb_specimen_id, ci_herbarium_specimens.pk_hrb_specimen_id),
            specimen_name = COALESCE(EXCLUDED.specimen_name, ci_herbarium_specimens.specimen_name),
            vernacular_name = COALESCE(EXCLUDED.vernacular_name, ci_herbarium_specimens.vernacular_name),
            family_name = COALESCE(EXCLUDED.family_name, ci_herbarium_specimens.family_name),
            genius_name = COALESCE(EXCLUDED.genius_name, ci_herbarium_specimens.genius_name),
            species_name = COALESCE(EXCLUDED.species_name, ci_herbarium_specimens.species_name),
            accession_number = COALESCE(EXCLUDED.accession_number, ci_herbarium_specimens.accession_number),
            collection_no = COALESCE(EXCLUDED.collection_no, ci_herbarium_specimens.collection_no),
            collector_name = COALESCE(EXCLUDED.collector_name, ci_herbarium_specimens.collector_name),
            determiner_name = COALESCE(EXCLUDED.determiner_name, ci_herbarium_specimens.determiner_name),
            collection_date = COALESCE(EXCLUDED.collection_date, ci_herbarium_specimens.collection_date),
            locality_name = COALESCE(EXCLUDED.locality_name, ci_herbarium_specimens.locality_name),
            collection_latitude = COALESCE(EXCLUDED.collection_latitude, ci_herbarium_specimens.collection_latitude),
            collection_longitude = COALESCE(EXCLUDED.collection_longitude, ci_herbarium_specimens.collection_longitude),
            collection_altitude = COALESCE(EXCLUDED.collection_altitude, ci_herbarium_specimens.collection_altitude),
            barcode = COALESCE(EXCLUDED.barcode, ci_herbarium_specimens.barcode),
            taxonomy_data = COALESCE(EXCLUDED.taxonomy_data, ci_herbarium_specimens.taxonomy_data),
            original_image = COALESCE(EXCLUDED.original_image, ci_herbarium_specimens.original_image),
            specimen_status = COALESCE(EXCLUDED.specimen_status, ci_herbarium_specimens.specimen_status),
            updated_at = NOW()
    """

    from psycopg2.extras import execute_values

    conn = hook.get_conn()
    processed_tasks = 0

    for batch in _iter_completed_batches_offset(hook, batch_size=batch_size):
        values: List[Tuple[Any, ...]] = []
        for task in batch:
            specimen_id = compute_specimen_id(task.get("barcode"), task["id"])
            parent_payload = build_parent_specimen_payload(task)
            values.append(
                (
                    specimen_id,
                    parent_payload["pk_hrb_specimen_id"],
                    parent_payload["specimen_name"],
                    parent_payload["vernacular_name"],
                    parent_payload["family_name"],
                    parent_payload["genius_name"],
                    parent_payload["species_name"],
                    parent_payload["accession_number"],
                    parent_payload["collection_no"],
                    parent_payload["collector_name"],
                    parent_payload["determiner_name"],
                    parent_payload["collection_date"],
                    parent_payload["locality_name"],
                    parent_payload["collection_latitude"],
                    parent_payload["collection_longitude"],
                    parent_payload["collection_altitude"],
                    parent_payload["barcode"],
                    parent_payload["taxonomy_data"],
                    parent_payload["original_image"],
                    parent_payload["specimen_status"],
                    dt.datetime.utcnow(),
                )
            )
        with conn.cursor() as cur:
            execute_values(
                cur,
                parent_upsert_sql,
                values,
                template="(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            )
        conn.commit()
        processed_tasks += len(batch)

    print(f"[migration_parent_task] saw {processed_tasks} COMPLETED tasks")


def image_processing_task(**context) -> None:
    """
    Task B:
    Concurrent icon generation with dead-letter handling.

    This task focuses on filesystem work and dead-letter logging, not DB detail inserts.
    """
    hook = PostgresHook(postgres_conn_id=POSTGRES_CONN_ID)

    batch_size = int(os.getenv("HERBARIUM_BATCH_SIZE", "1000"))
    workers = int(os.getenv("HERBARIUM_IMAGE_WORKERS", "8"))

    original_dir = os.getenv("HERBARIUM_IMAGES_ORIGINAL_DIR")
    icon_dir = os.getenv("HERBARIUM_IMAGES_ICON_DIR")
    if not original_dir or not icon_dir:  # pragma: no cover
        raise RuntimeError("Missing HERBARIUM_IMAGES_ORIGINAL_DIR / HERBARIUM_IMAGES_ICON_DIR env vars")
    os.makedirs(icon_dir, exist_ok=True)

    conn = hook.get_conn()

    from psycopg2.extras import execute_values

    dlq_insert_sql = """
        INSERT INTO ci_specimen_image_dlq (source_task_id, specimen_id, error_text)
        VALUES %s
        ON CONFLICT (source_task_id) DO UPDATE
        SET specimen_id = EXCLUDED.specimen_id,
            error_text = EXCLUDED.error_text,
            created_at = NOW()
    """

    processed = 0
    corrupted = 0

    task_source_statuses = os.getenv(
        "HERBARIUM_TASK_SOURCE_STATUSES",
        os.getenv(
            "HERBARIUM_PARENT_SOURCE_STATUSES",
            "COMPLETED,MIGRATED,DLQ_IMAGE_CORRUPTED",
        ),
    )
    statuses = [s.strip() for s in task_source_statuses.split(",") if s.strip()]

    for batch in _iter_completed_batches(
        hook,
        batch_size=batch_size,
        only_missing_in_dtl=True,
        statuses=statuses,
    ):
        # Thread-safe: create atomic icon files (handled in image_utils) and never share PIL objects.
        failures: List[Tuple[uuid.UUID, uuid.UUID, str]] = []
        success_tasks: List[uuid.UUID] = []

        def worker(task: Dict[str, Any]) -> Tuple[uuid.UUID, Optional[str], Optional[uuid.UUID]]:
            t_id = task["id"]
            try:
                filename = task.get("filename")
                image_url = task.get("image_url")

                icon_output_path = resolve_icon_output_path(icon_dir, filename or str(t_id))
                if os.path.exists(icon_output_path):
                    return t_id, None, None

                original_path = _resolve_original_image_path(original_dir, image_url, filename)
                create_optimized_icon(original_path, icon_output_path, size=(200, 200))
                return t_id, None, None
            except Exception as e:  # pragma: no cover (error paths)
                specimen_id = compute_specimen_id(task.get("barcode"), t_id)
                return t_id, str(e), specimen_id

        with ThreadPoolExecutor(max_workers=workers) as pool:
            future_map = {pool.submit(worker, task): task for task in batch}
            for fut in as_completed(future_map):
                t_id, err_text, specimen_id = fut.result()
                if err_text:
                    failures.append((t_id, specimen_id, err_text))
                else:
                    success_tasks.append(t_id)

        processed += len(batch)
        if failures:
            corrupted += len(failures)

            dlq_values = [(t_id, sid, err) for (t_id, sid, err) in failures]
            with conn.cursor() as cur:
                execute_values(cur, dlq_insert_sql, dlq_values, template="(%s,%s,%s)")
            conn.commit()

        # If icons are now generated successfully, clear any stale DLQ rows.
        if success_tasks:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    DELETE FROM ci_specimen_image_dlq
                    WHERE source_task_id = ANY(%s::uuid[])
                    """,
                    ([str(t_id) for t_id in success_tasks],),
                )
            conn.commit()

    print(f"[image_processing_task] processed {processed} tasks; corrupted={corrupted}")


def detail_and_sync_task(**context) -> None:
    """
    Task C:
    Insert into ci_specimen_dtl, update ci_herbarium_specimens using a single CTE UPDATE per batch,
    and keep herbarium_tasks source rows unchanged.
    """
    hook = PostgresHook(postgres_conn_id=POSTGRES_CONN_ID)

    batch_size = int(os.getenv("HERBARIUM_BATCH_SIZE", "1000"))
    original_dir = os.getenv("HERBARIUM_IMAGES_ORIGINAL_DIR")
    icon_dir = os.getenv("HERBARIUM_IMAGES_ICON_DIR")
    if not original_dir or not icon_dir:  # pragma: no cover
        raise RuntimeError("Missing HERBARIUM_IMAGES_ORIGINAL_DIR / HERBARIUM_IMAGES_ICON_DIR env vars")
    os.makedirs(icon_dir, exist_ok=True)

    conn = hook.get_conn()

    from psycopg2.extras import execute_values

    insert_dtl_sql = """
        INSERT INTO ci_specimen_dtl (specimen_id, seq_num, source_task_id, icon_image, taxonomy_metadata)
        VALUES %s
        ON CONFLICT (specimen_id, source_task_id) DO NOTHING
    """
    insert_dtl_template = "(%s,%s,%s,%s,%s::jsonb)"

    sync_parent_sql = """
        WITH latest AS (
            SELECT
                specimen_id,
                seq_num,
                icon_image,
                taxonomy_metadata,
                source_task_id,
                ROW_NUMBER() OVER (PARTITION BY specimen_id ORDER BY seq_num DESC) AS rn
            FROM ci_specimen_dtl
            WHERE specimen_id = ANY(%s)
        )
        UPDATE ci_herbarium_specimens p
        SET
            seq_num_current = latest.seq_num,
            icon_image = latest.icon_image,
            taxonomy_metadata = latest.taxonomy_metadata,
            latest_source_task_id = latest.source_task_id,
            updated_at = NOW()
        FROM latest
        WHERE p.id = latest.specimen_id
          AND latest.rn = 1
    """

    dlq_insert_sql = """
        INSERT INTO ci_specimen_image_dlq (source_task_id, specimen_id, error_text)
        VALUES %s
        ON CONFLICT (source_task_id) DO UPDATE
        SET specimen_id = EXCLUDED.specimen_id,
            error_text = EXCLUDED.error_text,
            created_at = NOW()
    """

    processed_tasks = 0
    migrated_tasks = 0

    task_source_statuses = os.getenv(
        "HERBARIUM_TASK_SOURCE_STATUSES",
        os.getenv(
            "HERBARIUM_PARENT_SOURCE_STATUSES",
            "COMPLETED,MIGRATED,DLQ_IMAGE_CORRUPTED",
        ),
    )
    statuses = [s.strip() for s in task_source_statuses.split(",") if s.strip()]

    for batch in _iter_completed_batches(
        hook,
        batch_size=batch_size,
        only_missing_in_dtl=True,
        statuses=statuses,
    ):
        specimen_ids_in_batch: List[uuid.UUID] = [
            compute_specimen_id(task.get("barcode"), task["id"]) for task in batch
        ]
        specimen_ids_unique: List[uuid.UUID] = sorted(set(specimen_ids_in_batch))
        success_specimen_ids: Set[uuid.UUID] = set()

        # Determine starting seq_num per specimen_id based on existing child rows.
        max_seq_by_specimen: Dict[uuid.UUID, int] = {sid: 0 for sid in specimen_ids_unique}
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT specimen_id, COALESCE(MAX(seq_num), 0) AS max_seq
                FROM ci_specimen_dtl
                WHERE specimen_id = ANY(%s)
                GROUP BY specimen_id
                """,
                (specimen_ids_unique,),
            )
            for sid, max_seq in cur.fetchall():
                max_seq_by_specimen[sid] = int(max_seq)

        next_seq_by_specimen: Dict[uuid.UUID, int] = {
            sid: max_seq_by_specimen[sid] + 1 for sid in specimen_ids_unique
        }

        dtl_values: List[Tuple[Any, ...]] = []
        dlq_values: List[Tuple[uuid.UUID, uuid.UUID, str]] = []

        for task in batch:
            t_id = task["id"]
            specimen_id = compute_specimen_id(task.get("barcode"), t_id)
            filename = task.get("filename")
            image_url = task.get("image_url")

            icon_output_path = resolve_icon_output_path(icon_dir, filename or str(t_id))
            if not os.path.exists(icon_output_path):
                # Stage B might have been skipped; attempt on-demand.
                try:
                    original_path = _resolve_original_image_path(original_dir, image_url, filename)
                    create_optimized_icon(original_path, icon_output_path, size=(200, 200))
                except Exception as e:  # pragma: no cover
                    dlq_values.append((t_id, specimen_id, str(e)))
                    continue

            icon_rel_path = os.path.join("icons", os.path.basename(icon_output_path))

            taxonomy_json = parse_json_text(task.get("taxonomy_data"))
            taxonomy_metadata = build_taxonomy_metadata(task, taxonomy_json)

            seq_num = next_seq_by_specimen[specimen_id]
            next_seq_by_specimen[specimen_id] = seq_num + 1

            dtl_values.append(
                (
                    specimen_id,
                    seq_num,
                    t_id,
                    icon_rel_path,
                    json.dumps(taxonomy_metadata, default=str),
                )
            )
            success_specimen_ids.add(specimen_id)

        # Insert child records for successful tasks.
        with conn.cursor() as cur:
            if dtl_values:
                execute_values(cur, insert_dtl_sql, dtl_values, template=insert_dtl_template)
            # Sync parent ONLY for specimens where we inserted children.
            if success_specimen_ids:
                cur.execute(sync_parent_sql, (list(success_specimen_ids),))
            # Dead-letter failures.
            if dlq_values:
                execute_values(
                    cur,
                    dlq_insert_sql,
                    [(t_id, sid, err) for (t_id, sid, err) in dlq_values],
                    template="(%s,%s,%s)",
                )

        conn.commit()

        processed_tasks += len(batch)
        migrated_tasks += len(dtl_values)

    print(f"[detail_and_sync_task] processed={processed_tasks}; migrated={migrated_tasks}")


default_args = {
    "owner": "airflow",
    "retries": 1,
    "retry_delay": dt.timedelta(minutes=5),
}

with DAG(
    dag_id="herbarium_migration_dag",
    description="Mass-migrate COMPLETED herbarium_tasks into ci_* with versioned details + icon optimization",
    schedule=None,
    start_date=dt.datetime(2026, 1, 1),
    catchup=False,
    max_active_runs=1,
    default_args=default_args,
    tags=["etl", "herbarium"],
) as dag:
    migrate_parent = PythonOperator(
        task_id="migration_parent_insert",
        python_callable=migration_parent_task,
    )
    process_icons = PythonOperator(
        task_id="image_processing_icon",
        python_callable=image_processing_task,
    )
    detail_and_sync = PythonOperator(
        task_id="detail_and_sync",
        python_callable=detail_and_sync_task,
    )

    migrate_parent >> process_icons >> detail_and_sync

