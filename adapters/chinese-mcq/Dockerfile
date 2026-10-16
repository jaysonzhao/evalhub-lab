FROM registry.access.redhat.com/ubi9/python-311:latest

USER 0
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py datasets_config.py parser.py prompts.py ./
RUN mkdir -p /tmp/chinese-mcq-results && chown -R 1001:0 /app /tmp/chinese-mcq-results && chmod -R g+rwX /app /tmp/chinese-mcq-results
ENV PYTHONUNBUFFERED=1 EVALHUB_MODE=k8s EVALHUB_JOB_SPEC_PATH=/meta/job.json HOME=/tmp DATA_DIR=/test_data
USER 1001
ENTRYPOINT ["python", "main.py"]
