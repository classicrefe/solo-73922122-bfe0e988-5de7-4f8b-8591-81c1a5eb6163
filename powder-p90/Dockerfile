FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts
COPY tests ./tests

RUN mkdir -p /app/data
ENV POWDER_DB=/app/data/powder.db
EXPOSE 8000

# 两个工作进程；批号锁为文件锁，跨进程有效。
CMD ["gunicorn", "--workers", "2", "--bind", "0.0.0.0:8000", "app.main:app"]
