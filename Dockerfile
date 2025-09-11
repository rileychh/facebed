FROM python:3.13-alpine AS builder
RUN apk add --no-cache gcc libffi-dev musl-dev
WORKDIR /app
COPY requirements.txt .
RUN pip3.13 install -r ./requirements.txt

FROM python:3.13-alpine
WORKDIR /facebed
RUN adduser -D facebed
COPY . .
RUN /bin/sh -c "[ -f ./config.yaml ] || echo '{}' > ./config.yaml" && chown facebed:facebed ./config.yaml
COPY --from=builder --chown=facebed:facebed /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
USER facebed
CMD ["python3.13", "./facebed.py", "-c", "./config.yaml"]
