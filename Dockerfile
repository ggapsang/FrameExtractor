# syntax=docker/dockerfile:1
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# OpenCV(opencv-python-headless)는 libGL이 필요 없으나 일부 코덱은
# 시스템 라이브러리에 의존한다.  최소 의존 추가.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY src/ ./src/
ENV PYTHONPATH=/app/src

EXPOSE 9110

CMD ["python", "-m", "frame_extractor"]
