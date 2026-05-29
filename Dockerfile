FROM python:3.12-slim

RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip --root-user-action=ignore \
 && pip install --no-cache-dir --root-user-action=ignore -r requirements.txt

COPY benchmark.py .
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

VOLUME ["/data"]

ENTRYPOINT ["/app/entrypoint.sh"]
