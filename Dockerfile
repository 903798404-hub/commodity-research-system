FROM python:3.12-slim

ARG MARKET_DATA_GIT_HEAD
ARG MARKET_DATA_GIT_TREE
ARG MARKET_DATA_RELEASE_ID
ARG MARKET_DATA_BUILD_TIME
ARG MARKET_DATA_SOURCE

LABEL org.opencontainers.image.revision="${MARKET_DATA_GIT_HEAD}" \
      org.opencontainers.image.version="${MARKET_DATA_RELEASE_ID}" \
      org.opencontainers.image.created="${MARKET_DATA_BUILD_TIME}" \
      org.opencontainers.image.source="${MARKET_DATA_SOURCE}"

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_INDEX_URL=https://mirrors.cloud.tencent.com/pypi/simple
ENV PIP_DEFAULT_TIMEOUT=120

WORKDIR /app

RUN sed -i \
        -e 's|http://deb.debian.org/debian|http://mirrors.cloud.tencent.com/debian|g' \
        -e 's|http://deb.debian.org/debian-security|http://mirrors.cloud.tencent.com/debian-security|g' \
        /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends fonts-noto-cjk fontconfig \
    && fc-cache -fv \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir --default-timeout=120 -r requirements.txt

COPY 02_configs /app/02_configs
COPY 03_src /app/03_src
COPY 04_scripts /app/04_scripts
COPY 05_apps /app/05_apps
COPY 07_docs /app/07_docs
COPY .streamlit /app/.streamlit

RUN python -c 'import json,re,sys; commit,tree,release_id,build_time,source=sys.argv[1:]; match=re.fullmatch(r"spread-\d{8}-([0-9a-f]{12,40})-b\d{2,}",release_id); assert re.fullmatch(r"[0-9a-f]{40}",commit), "MARKET_DATA_GIT_HEAD must be a full 40-character commit"; assert re.fullmatch(r"[0-9a-f]{40}",tree) and tree != commit, "MARKET_DATA_GIT_TREE must be a full tree SHA distinct from the commit"; assert match and commit.startswith(match.group(1)), "MARKET_DATA_RELEASE_ID must identify MARKET_DATA_GIT_HEAD"; assert build_time and source, "build time and source are required"; payload={"application":"spread-dashboard","release_id":release_id,"git_commit":commit,"git_tree":tree,"build_time":build_time,"source":source}; open("/app/RELEASE.json","w",encoding="utf-8").write(json.dumps(payload,ensure_ascii=False,indent=2,sort_keys=True)+"\n")' \
        "$MARKET_DATA_GIT_HEAD" "$MARKET_DATA_GIT_TREE" "$MARKET_DATA_RELEASE_ID" "$MARKET_DATA_BUILD_TIME" "$MARKET_DATA_SOURCE" \
    && chmod 0444 /app/RELEASE.json \
    && mkdir -p /app/01_data /app/06_outputs /app/10_logs

ENV PYTHONPATH=/app/03_src

EXPOSE 8501

CMD ["streamlit", "run", "05_apps/streamlit_app.py", "--server.address=0.0.0.0", "--server.port=8501"]
