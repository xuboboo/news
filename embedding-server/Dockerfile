FROM python:3.12-slim
WORKDIR /srv
# 国内网络：pip 走清华镜像，模型下载经 HF_ENDPOINT 指向 hf-mirror（在 compose 里设）
ENV PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    PYTHONUNBUFFERED=1
# 版本锁定的就是现网容器实测版本（2026-09-18 审计整改 F-4：不可复现构建）；
# 升级需改这里并重建验证（fastapi/uvicorn 与后端镜像各自独立，不要求同版本）
RUN pip install --no-cache-dir fastembed==0.8.0 fastapi==0.141.1 "uvicorn[standard]==0.52.4"
COPY server.py /srv/server.py
EXPOSE 80
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "80"]
