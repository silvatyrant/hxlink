FROM python:3.12.8-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install Python dependencies before copying source code
# so this layer is cached when only source changes
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY hxlink/ ./hxlink/
COPY LICENSE.ha-ble-adv .

COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

ENTRYPOINT ["./entrypoint.sh"]
