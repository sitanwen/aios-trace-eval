FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY web ./web
RUN pip install --no-cache-dir .

ENV AIOS_DB_PATH=/data/aios_eval.db
ENV AIOS_WEB_DIR=/app/web
EXPOSE 8080
CMD ["aios-trace-eval-server"]
