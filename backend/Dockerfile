FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts
# 测试随镜像分发：容器内 python -m pytest tests/ -q（见 README AI 能力小节）
COPY conftest.py ./conftest.py
COPY tests ./tests

# 非特权运行（2026-09-18 审计整改 F-2）：运行期无 root 写盘需求（应用只写 DB/Redis，
# crawler 心跳写 /tmp 世界可写目录，依赖在 root 阶段装完只读）
RUN useradd -r -u 10001 appuser
USER appuser

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
