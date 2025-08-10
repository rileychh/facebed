FROM python:3.13-alpine
COPY --from=ghcr.io/astral-sh/uv:0.8.8 /uv /uvx /bin/

RUN apk add --no-cache git

WORKDIR /app
COPY user_config.example.py user_config.py
COPY . .

RUN uv pip install --system --no-cache-dir -r requirements.txt

CMD ["python", "main.py", "--timezone=8"]
