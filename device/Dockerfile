FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY device.py .
# Version "firmware" injectée par la CI (sha du commit), affichée dans le dashboard.
ARG VERSION=dev
ENV VERSION=$VERSION PYTHONUNBUFFERED=1
CMD ["python", "device.py"]
