FROM python:3.12-alpine

# exiftool is required by the mapper worker
RUN apk add --no-cache exiftool tzdata

WORKDIR /app

COPY workers/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# schemas.py is shared; copy from project root
COPY schemas.py .
COPY workers/ .

ENV PYTHONUNBUFFERED=1

CMD ["python", "run_worker.py"]
