FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libzbar0 libgl1 libglib2.0-0 && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=10000
CMD ["bash","-lc","exec gunicorn -w 2 -k gthread -b 0.0.0.0:${PORT} qr:app"]
