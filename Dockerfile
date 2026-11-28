FROM golang:1.26 AS douyin-builder
WORKDIR /src/douyin-scanner
COPY douyin-scanner/go.mod ./
COPY douyin-scanner/main.go ./
RUN go mod tidy
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -ldflags="-s -w" -o /out/douyin-scanner .

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app
COPY requirements.txt .
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium

COPY --from=douyin-builder /out/douyin-scanner /app/douyin-scanner
COPY . .
RUN chmod +x /app/start.sh
EXPOSE 10000
CMD ["/app/start.sh"]
