import json
import os
import sys

import psycopg2


def _parse_conn(conn_str: str) -> str:
    # Airflow uses `postgresql+psycopg2://...`; psycopg2 expects `postgresql://...`.
    if conn_str.startswith("postgresql+psycopg2://"):
        return "postgresql://" + conn_str[len("postgresql+psycopg2://") :]
    return conn_str


def main() -> int:
    conn_str = os.getenv("AIRFLOW_CONN_POSTGRES_DEFAULT")
    if not conn_str:
        print("Missing AIRFLOW_CONN_POSTGRES_DEFAULT env var", file=sys.stderr)
        return 2

    conn_str = _parse_conn(conn_str)
    conn = psycopg2.connect(conn_str)

    q = """
        SELECT
            p.id AS specimen_id,
            p.seq_num_current AS parent_seq,
            d_latest.seq_num AS child_latest_seq,
            p.icon_image AS parent_icon_image,
            d_latest.icon_image AS child_icon_image,
            p.taxonomy_metadata AS parent_taxonomy_metadata,
            d_latest.taxonomy_metadata AS child_taxonomy_metadata
        FROM ci_herbarium_specimens p
        LEFT JOIN LATERAL (
            SELECT *
            FROM ci_specimen_dtl d
            WHERE d.specimen_id = p.id
            ORDER BY d.seq_num DESC
            LIMIT 1
        ) d_latest ON true
        WHERE
            (
                d_latest.seq_num IS NULL
                AND (
                    p.seq_num_current <> 0
                    OR p.icon_image IS NOT NULL
                    OR p.taxonomy_metadata IS NOT NULL
                )
            )
            OR (
                d_latest.seq_num IS NOT NULL
                AND (
                    p.seq_num_current <> d_latest.seq_num
                    OR p.icon_image IS DISTINCT FROM d_latest.icon_image
                    OR p.taxonomy_metadata IS DISTINCT FROM d_latest.taxonomy_metadata
                )
            )
        LIMIT 100
    """

    with conn.cursor() as cur:
        cur.execute(q)
        rows = cur.fetchall()

    if not rows:
        print("OK: ci_herbarium_specimens is in sync with the latest ci_specimen_dtl rows.")
        return 0

    print(f"Found {len(rows)} mismatches (showing up to 100):", file=sys.stderr)
    for (
        specimen_id,
        parent_seq,
        child_latest_seq,
        parent_icon_image,
        child_icon_image,
        parent_tax,
        child_tax,
    ) in rows:
        parent_tax_summary = json.dumps(parent_tax, ensure_ascii=False)[:200] if isinstance(parent_tax, dict) else str(parent_tax)[:200]
        child_tax_summary = json.dumps(child_tax, ensure_ascii=False)[:200] if isinstance(child_tax, dict) else str(child_tax)[:200]
        print(
            f"- specimen_id={specimen_id}\n"
            f"  parent_seq={parent_seq}, child_latest_seq={child_latest_seq}\n"
            f"  parent_icon={parent_icon_image}\n"
            f"  child_icon={child_icon_image}\n"
            f"  parent_tax={parent_tax_summary}\n"
            f"  child_tax={child_tax_summary}\n",
            file=sys.stderr,
        )

    return 1


if __name__ == "__main__":
    raise SystemExit(main())

