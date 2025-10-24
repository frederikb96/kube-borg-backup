FROM python:3.13-slim

# Ensure Python output is not buffered (important for kubectl logs)
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY kube_pvc_snapshot/ ./kube_pvc_snapshot/
COPY kube_snapshot_borgbackup/ ./kube_snapshot_borgbackup/

# Default entrypoint - can be overridden in k8s manifests
CMD ["python", "-m", "kube_pvc_snapshot.main"]
