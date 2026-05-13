FROM python:3.11-slim

# v3.1.3 P0-4 fix:容器内必须装 docker-cli + docker compose plugin,update.sh 才能
# 跑 docker ps / docker exec / docker compose up --build。之前 fanout 28 台全失败的根因。
# 用 docker 官方 convenience script 装(debian apt 默认源没 docker-cli)。
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl procps git ca-certificates gnupg \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
    && chmod a+r /etc/apt/keyrings/docker.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian $(. /etc/os-release && echo $VERSION_CODENAME) stable" > /etc/apt/sources.list.d/docker.list \
    && apt-get update && apt-get install -y --no-install-recommends docker-ce-cli docker-compose-plugin \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "main.py"]
