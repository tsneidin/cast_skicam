FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core ca-certificates && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir Pillow==11.3.0 PyChromecast==14.0.9
WORKDIR /app
COPY app.py cameras.json ./
ENV PYTHONUNBUFFERED=1 TZ=America/Denver
CMD ["python", "app.py"]
