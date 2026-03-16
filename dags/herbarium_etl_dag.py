from datetime import datetime, timedelta
import json
import re
import logging
from airflow import DAG
from airflow.operators.python import PythonOperator
from airflow.providers.postgres.hooks.postgres import PostgresHook
from airflow.exceptions import AirflowException

# Constants
SOURCE_TABLE = "herbarium_tasks"
TARGET_TABLE = "ci_herbarium_specimens"
CONN_ID = "herbarium_postgres"

# Default arguments for the DAG
default_args = {
    "owner": "airflow",
    "retries": 3,  # Retry the DAG task 3 times in case of failure
    "retry_delay": timedelta(minutes=5),  # Delay between retries
}

# -------------------------------
# Barcode Normalization Function
# -------------------------------
def normalize_barcode(barcode):
    if not barcode or not barcode.startswith("LWG"):
        return None

    # extract digits after LWG
    digits = re.sub(r"\D", "", barcode[3:])

    # keep only the last 9 digits
    digits = digits[-9:]

    # fill remaining with zeros from left
    digits = digits.zfill(9)

    return f"LWG{digits}"

# -------------------------------
# ETL Process Function
# -------------------------------
def etl_process():
    # Initialize Postgres hook
    hook = PostgresHook(postgres_conn_id=CONN_ID)

    # Verify the connection
    try:
        conn = hook.get_conn()
        cur = conn.cursor()
        logging.info(f"Successfully connected to {CONN_ID}")
    except Exception as e:
        raise AirflowException(f"Connection to {CONN_ID} failed: {e}")

    try:
        # ---------- EXTRACT ----------
        logging.info(f"Extracting data from {SOURCE_TABLE} where status='COMPLETED'")
        cur.execute(f"SELECT * FROM {SOURCE_TABLE} WHERE status='COMPLETED'")
        rows = cur.fetchall()

        # Get column names from the query result
        columns = [desc[0] for desc in cur.description]

        # Log if no rows found
        if not rows:
            logging.info(f"No rows found with status 'COMPLETED' in {SOURCE_TABLE}.")
            return

        # Batch insert preparation
        insert_values = []

        for row in rows:
            record = dict(zip(columns, row))

            # Handle missing/incorrect columns with safe get()
            taxonomy_data = record.get("taxonomy_data")
            barcode = record.get("barcode")
            genus = record.get("genus")
            species = record.get("species")
            
            # ---------- TRANSFORM ----------
            # Handle taxonomy_data (TEXT → JSON)
            try:
                taxonomy_json = json.loads(taxonomy_data) if taxonomy_data else None
            except Exception as e:
                logging.error(f"Error parsing taxonomy_data for ID {record.get('id')}: {e}")
                taxonomy_json = None

            # Normalize barcode
            barcode = normalize_barcode(barcode)

            specimen_name = None
            if genus or species:
                specimen_name = f"{genus or ''} {species or ''}".strip()

            # Prepare the values for insert
            insert_values.append((
                record.get("id"),
                barcode,
                record.get("accession_number"),
                record.get("collection_number"),
                record.get("collection_date"),
                specimen_name,
                record.get("common_name"),
                record.get("family"),
                genus,
                species,
                json.dumps(taxonomy_json) if taxonomy_json else None,
                record.get("collector_name"),
                record.get("location"),
                record.get("latitude"),
                record.get("longitude"),
                record.get("altitude"),
                record.get("image_url"),
                record.get("notes"),
                record.get("status"),
            ))

        # ---------- LOAD ----------
        insert_query = f"""
        INSERT INTO {TARGET_TABLE} (
            id,
            barcode,
            accession_number,
            collection_no,
            collection_date,
            specimen_name,
            vernacular_name,
            family_name,
            genus_name,
            species_name,
            taxonomy_data,
            collector_name,
            locality_name,
            collection_latitude,
            collection_longitude,
            collection_altitude,
            original_image,
            specimen_details,
            specimen_status
        )
        VALUES (
            %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
        )
        ON CONFLICT (id) DO UPDATE SET
            barcode = EXCLUDED.barcode,
            specimen_name = EXCLUDED.specimen_name,
            taxonomy_data = EXCLUDED.taxonomy_data,
            collector_name = EXCLUDED.collector_name,
            locality_name = EXCLUDED.locality_name,
            collection_latitude = EXCLUDED.collection_latitude,
            collection_longitude = EXCLUDED.collection_longitude,
            collection_altitude = EXCLUDED.collection_altitude,
            original_image = EXCLUDED.original_image,
            specimen_details = EXCLUDED.specimen_details,
            specimen_status = EXCLUDED.specimen_status
        """

        # Execute the batch insert
        logging.info(f"Executing insert for {len(insert_values)} records.")
        cur.executemany(insert_query, insert_values)

        # Commit the transaction
        conn.commit()
        logging.info("ETL process completed successfully.")

    except Exception as e:
        logging.error(f"ETL process failed: {e}")
        conn.rollback()
        raise AirflowException(f"ETL process failed: {e}")

    finally:
        # Ensure the cursor and connection are closed
        cur.close()
        conn.close()

# -------------------------------
# DAG DEFINITION
# -------------------------------
with DAG(
    dag_id="herbarium_etl_dag",
    start_date=datetime(2024, 1, 1),
    schedule=None,
    catchup=False,
    default_args=default_args,
) as dag:

    # Task to run the ETL process
    run_etl = PythonOperator(
        task_id="run_etl",
        python_callable=etl_process,
    )

    run_etl

# Ensure DAG is available at module level for Airflow discovery
globals()['herbarium_etl_dag'] = dag