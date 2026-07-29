FROM python:3.12-slim

# Unprivileged runtime user; /data is chowned to this uid on the host.
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin appuser

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY schema/ ./schema/
COPY tools/ ./tools/
COPY examples/ ./examples/

ENV AC_DATA_DIR=/data
EXPOSE 8098

USER 10001
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8098"]
