FROM python:3.13-alpine
WORKDIR /facebed
COPY . .
RUN /bin/sh -c "[ -f ./config.yaml ] || echo '{}' > ./config.yaml"
RUN adduser -D facebed
USER facebed
RUN pip3.13 install -r ./requirements.txt
CMD ["python3.13", "./facebed.py", "-c", "./config.yaml"]
