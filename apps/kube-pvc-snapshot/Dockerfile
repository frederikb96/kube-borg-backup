FROM python:3.13-slim

RUN pip install --no-cache-dir -r requirements.txt

WORKDIR /app
COPY configmap.yaml.j2 .
COPY main.py .

CMD ["python", "main.py"]
