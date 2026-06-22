FROM python:3.11-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    apt-transport-https \
    bash \
    ca-certificates \
    curl \
    fuse \
    gnupg \
    libsm6 \
    libxext6 \
    libglib2.0-0 \
    procps \
    && curl -fsSL https://mega.nz/linux/repo/Debian_12/Release.key \
    | gpg --dearmor -o /usr/share/keyrings/megacmd.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/megacmd.gpg] https://mega.nz/linux/repo/Debian_12/ ./" \
    > /etc/apt/sources.list.d/megacmd.list \
    && apt-get update && apt-get install -y --no-install-recommends \
    megacmd \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY app ./app
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh

RUN chmod +x /usr/local/bin/entrypoint.sh

RUN mkdir -p /data/tmp /data/compressed

EXPOSE 8000

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
