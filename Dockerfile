FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_INDEX_URL=https://mirrors.cloud.tencent.com/pypi/simple
ENV PIP_DEFAULT_TIMEOUT=120

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir --default-timeout=120 -r requirements.txt

COPY 01_data /app/01_data
COPY 02_configs /app/02_configs
COPY 03_src /app/03_src
COPY 04_scripts /app/04_scripts
COPY 05_apps /app/05_apps
COPY 06_outputs /app/06_outputs
COPY 07_docs /app/07_docs
COPY .streamlit /app/.streamlit

ENV PYTHONPATH=/app/03_src

EXPOSE 8501

CMD ["streamlit", "run", "05_apps/streamlit_app.py", "--server.address=0.0.0.0", "--server.port=8501"]
