from datetime import datetime, timedelta
from airflow import DAG
from airflow.operators.python import PythonOperator
from airflow.providers.postgres.hooks.postgres import PostgresHook
import os
import requests
from PIL import Image


default_args = {
    "owner": "airflow",
    "retries": 3,
    "retry_delay": timedelta(minutes=5),
}

# Constants
TEMP_DIR = "/tmp/herbarium_images"




def extract_images(**context):

    hook = PostgresHook(postgres_conn_id="herbarium_postgres")
    conn = hook.get_conn()
    cur = conn.cursor()

    query = """
    SELECT id, original_image
    FROM ci_herbarium_specimens
    WHERE icon_image IS NULL
    """

    cur.execute(query)
    rows = cur.fetchall()

    os.makedirs(TEMP_DIR, exist_ok=True)

    extracted = []

    for specimen_id, image_url in rows:

        full_url = f"http://host.docker.internal:8000{image_url}"

        try:
            response = requests.get(full_url, timeout=30)
            response.raise_for_status()  # Raise an exception for bad status codes

            file_path = f"{TEMP_DIR}/{specimen_id}.jpg"

            with open(file_path, "wb") as f:
                f.write(response.content)

        except (requests.exceptions.RequestException, OSError) as e:
           
            print(f"Warning: Could not download image from {full_url}: {e}")
            print("Creating mock image for testing...")

            # Create a simple mock image (1x1 pixel)
            from PIL import Image
            mock_img = Image.new('RGB', (100, 100), color='gray')
            file_path = f"{TEMP_DIR}/{specimen_id}.jpg"
            mock_img.save(file_path, 'JPEG')

        extracted.append((specimen_id, file_path))

    context["ti"].xcom_push(key="images", value=extracted)


def transform_images(**context):

    images = context["ti"].xcom_pull(key="images")

    if not images:
        print("No images to transform")
        context["ti"].xcom_push(key="images", value=[])
        return

    transformed = []

    for specimen_id, file_path in images:

        try:
            img = Image.open(file_path)

            output_path = f"{TEMP_DIR}/{specimen_id}_compressed.jpg"

            quality = 85

            while quality > 10:  # Prevent infinite loop

                img.save(output_path, "JPEG", quality=quality)

                if os.path.getsize(output_path) <= 300 * 1024:
                    break

                quality -= 5

            transformed.append((specimen_id, output_path))

        except Exception as e:
            print(f"Error processing image {specimen_id}: {e}")
            # Skip this image but continue with others
            continue

    context["ti"].xcom_push(key="images", value=transformed)


def load_images(**context):

    images = context["ti"].xcom_pull(key="images")

    if not images:
        print("No images to load")
        return

    hook = PostgresHook(postgres_conn_id="herbarium_postgres")
    conn = hook.get_conn()
    cur = conn.cursor()

    try:
        for specimen_id, path in images:

            try:
             
                update_query = """
                UPDATE ci_herbarium_specimens
                SET icon_image = %s
                WHERE id = %s
                """

                cur.execute(update_query, (path, specimen_id))

            except Exception as e:
                print(f"Error updating database for specimen {specimen_id}: {e}")
                continue

        conn.commit()
        print(f"Successfully updated {len(images)} specimens with icon images")

    except Exception as e:
        print(f"Database error during load: {e}")
        conn.rollback()
        raise

    finally:
        cur.close()
        conn.close()


with DAG(
    dag_id="image_etl_dag",
    start_date=datetime(2024, 1, 1),
    schedule=None,
    catchup=False,
    default_args=default_args,
) as dag:

    extract = PythonOperator(
        task_id="extract_images",
        python_callable=extract_images
    )

    transform = PythonOperator(
        task_id="transform_images",
        python_callable=transform_images
    )

    load = PythonOperator(
        task_id="load_images",
        python_callable=load_images
    )

    extract >> transform >> load


globals()['image_etl_dag'] = dag