FROM python:3.13-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates && \
    curl -L https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl -o /usr/local/bin/kubectl && \
    chmod +x /usr/local/bin/kubectl && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir -r requirements.txt

WORKDIR /app
COPY main.py .

CMD ["python", "main.py"]
