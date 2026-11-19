
FROM docker.io/library/python:3.12-alpine

RUN apk add --no-cache tzdata
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY app /app
WORKDIR /app

ENV FACTU_DATA_DIR=/data \
    FACTU_INGRESS_ONLY=1 \
    FACTU_PORT=8099 \
    PYTHONUNBUFFERED=1

CMD ["python3", "app.py"]
