FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt ./requirements.txt
COPY synapse/requirements.txt ./synapse/requirements.txt
RUN pip install --no-cache-dir -r synapse/requirements.txt \
    && useradd --uid 10001 --create-home synapse \
    && mkdir -p /data && chown synapse:synapse /data
COPY monosuite_cli.py ./monosuite_cli.py
COPY synapse ./synapse
USER synapse
ENTRYPOINT ["python", "-m", "synapse"]
CMD ["serve", "--db", "/data/synapse.sqlite", "--host", "0.0.0.0"]
