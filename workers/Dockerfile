FROM python:3.12-alpine

# exiftool is required by the mapper worker
RUN apk add --no-cache exiftool tzdata

WORKDIR /app

COPY workers/requirements.txt .
# All deps (incl. pyicloud's: srp is pure-Python, cryptography/cffi/pydantic-core
# ship musllinux wheels) install from wheels — no build toolchain needed.
RUN pip install --no-cache-dir -r requirements.txt

# schemas.py and icloud_client.py are shared; copy from project root
COPY schemas.py .
COPY icloud_client.py .
COPY workers/ .

ENV PYTHONUNBUFFERED=1

CMD ["python", "run_worker.py"]
