FROM cgr.dev/chainguard/python:latest-dev AS build

# Chainguard's python image defaults to a non-root user; WORKDIR /app is created
# owned by root, so pip can't write into /app/packages. Run the throwaway build
# stage as root. The distroless runtime stage below stays non-root.
USER root

WORKDIR /app

COPY backend/requirements.txt .
RUN pip install --no-cache-dir --target=/app/packages -r requirements.txt

FROM cgr.dev/chainguard/python:latest

# The backend writes to and cleans up the shared /staging bind mount that the
# root-running Alpine workers also write to. Run as root so it can create and
# rmtree those mixed-ownership dirs without EACCES.
USER root

WORKDIR /app

COPY --from=build /app/packages /app/packages

# schemas.py is shared; copy from project root
COPY schemas.py .
COPY backend/ .

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app/packages

EXPOSE 8000
# The Chainguard python base sets ENTRYPOINT ["python"]; reset it so the CMD
# below runs as-is instead of being appended (which yields `python python ...`).
ENTRYPOINT []
CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
