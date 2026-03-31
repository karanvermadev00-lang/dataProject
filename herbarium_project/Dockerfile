FROM apache/airflow:2.9.3

USER root

RUN apt-get update && apt-get install -y \
    tesseract-ocr \
    libgl1 \
    libglib2.0-0 \
    && apt-get clean

USER airflow

RUN pip install --no-cache-dir \
    opencv-python-headless \
    numpy \
    pillow \
    pandas \
    psycopg2-binary \
    pytesseract