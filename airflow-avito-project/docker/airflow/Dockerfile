FROM apache/airflow:2.10.5-python3.11

COPY requirements.txt /requirements.txt

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

USER airflow
RUN pip install --no-cache-dir -r /requirements.txt
