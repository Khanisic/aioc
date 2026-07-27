# One image, three services: SERVICE_NAME / DOWNSTREAM_URLS in docker-compose.yml
# decide which role a container plays. Keeping a single image means the three
# services can never drift apart in dependencies or behavior.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

EXPOSE 8000
CMD ["python", "app.py"]
