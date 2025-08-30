FROM python:3.13-slim

RUN pip install --no-cache-dir -r requirements.txt

WORKDIR /app
COPY main.py pod.yaml.j2 .

CMD ["python", "main.py"]
