# EdgeRIC-RT muApp host image (pod edgeric-0). The muApp code is bind-mounted at /edgeric, not baked in.
#   docker build -t edgeric:local edgeric-rt && k3d image import edgeric:local -c "$CLUSTER"
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 OMP_NUM_THREADS=1
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch \
 && pip install numpy pyyaml
RUN apt-get update && apt-get install -y --no-install-recommends procps util-linux \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /edgeric
CMD ["sleep", "infinity"]
