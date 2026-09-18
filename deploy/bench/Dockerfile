ARG PYTHON_IMAGE
FROM ${PYTHON_IMAGE}
WORKDIR /app
COPY requirements.txt /app/requirements.txt
COPY wheels /app/wheels
RUN pip install --no-cache-dir --only-binary=:all: --require-hashes -r requirements.txt
COPY runtime-config/benchmark-cases.json /app/runtime-config/benchmark-cases.json
COPY run.json /app/run.json
RUN mkdir -p /app/results && chown 10001:10001 /app/results
USER 10001
EXPOSE 8080
CMD ["python", "-m", "advisor_deploy.bench", "run", "--plan", "/app/run.json", "--output", "/app/results", "--allow-cloud"]
