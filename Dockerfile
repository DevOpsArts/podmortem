FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY pyproject.toml .

RUN pip install --no-cache-dir .

ENV PODMORTEM_DB_PATH=/data/podmortem.db

VOLUME ["/data"]

ENTRYPOINT ["podmortem"]
CMD ["watch"]
