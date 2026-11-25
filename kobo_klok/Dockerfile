FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY assets/ ./assets/
COPY app.py .

ENV PORT=8099
ENV TZ=Europe/Amsterdam
EXPOSE 8099

CMD ["python", "app.py"]
