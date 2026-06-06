# Worker image for V_2rubby.
# The master clones the repo onto a fresh server and builds this image, then
# runs it as MODE=worker (a headless API node). The SAME image can run the
# master too, but normally the master runs directly with python.
FROM python:3.11-slim

# System deps are only a fallback: cryptography/asyncssh ship prebuilt wheels
# for slim images, so a flaky apt mirror must NOT break the build. We therefore
# make this step best-effort (|| true) and never fail the image on it.
RUN (apt-get update \
    && apt-get install -y --no-install-recommends gcc libffi-dev curl \
    && rm -rf /var/lib/apt/lists/*) || true

WORKDIR /app

# Install Python deps first (better layer caching).
COPY requirements.txt /app/requirements.txt
# Upgrade pip so it always prefers manylinux wheels (no compiler needed), and
# give pip extra retries / a longer timeout for slow or flaky networks.
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir --retries 10 --timeout 120 \
       -i https://pypi.org/simple -r requirements.txt

# App source.
COPY . /app

# Sessions / db live under /app/data, which is mounted as a host volume so they
# survive container restarts and updates.
RUN mkdir -p /app/data
ENV MODE=worker
# Unbuffered stdout/stderr so `docker logs` shows output immediately.
ENV PYTHONUNBUFFERED=1

# The worker API listens on this port (loopback-published by the master).
EXPOSE 8765

CMD ["python", "main.py"]
