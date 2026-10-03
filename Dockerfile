# IDly 리포트 서버. 분석은 서버 안 스레드에서 몇 분씩 돌고, PDF는 Playwright(크로뮴)로 만든다.
# 그래서 서버리스가 아니라 계속 켜 두는 서버(Railway·Render 등)에 올린다.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PYTHONUTF8=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY requirements.txt .
# 크로뮴과 시스템 라이브러리, PDF 한글이 깨지지 않게 CJK 글꼴
RUN pip install -r requirements.txt \
    && playwright install --with-deps chromium \
    && apt-get update && apt-get install -y --no-install-recommends fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

COPY . .

# 분석 진행 상태를 서버 메모리에 두므로 프로세스는 하나만 띄운다
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --proxy-headers --forwarded-allow-ips='*'"]
