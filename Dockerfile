FROM python:3.13-alpine AS builder
WORKDIR /app
COPY requirements.txt .
RUN pip3.13 install -r ./requirements.txt

FROM python:3.13-alpine
WORKDIR /facebed
COPY . .
RUN /bin/sh -c "echo '{}' > ./config.yaml"
COPY --from=builder /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
RUN adduser -D facebed
USER facebed
CMD ["python3.13", "./facebed.py", "-c", "./config.yaml"]
