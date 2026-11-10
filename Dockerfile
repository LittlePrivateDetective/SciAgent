# SciAgent 单容器镜像：API + 前端静态页一起交付
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# 系统依赖：tesseract 用于扫描件兜底 OCR（可选，装不上也不影响主流程）；
# fonts-noto-cjk 保证页图渲染中文不缺字。
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr tesseract-ocr-chi-sim tesseract-ocr-eng \
        fonts-noto-cjk curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY web ./web
COPY docs ./docs
COPY .env.example ./.env.example

RUN mkdir -p data/pdf data/uploads data/pages data/exports data/logs data/chroma

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
