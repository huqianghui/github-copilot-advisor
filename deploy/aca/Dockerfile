ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt /app/requirements.txt
COPY wheels /app/wheels
RUN pip install --no-cache-dir --require-hashes -r requirements.txt
COPY runtime-config /app/runtime-config
USER 10001
EXPOSE 3978
CMD ["python", "-m", "teams_adapter"]
