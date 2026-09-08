FROM python:3.12-slim
WORKDIR /srv
# 国内网络：pip 走清华镜像，模型下载经 HF_ENDPOINT 指向 hf-mirror（在 compose 里设）
ENV PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    PYTHONUNBUFFERED=1
RUN pip install --no-cache-dir fastembed fastapi "uvicorn[standard]"
COPY server.py /srv/server.py
EXPOSE 80
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "80"]
