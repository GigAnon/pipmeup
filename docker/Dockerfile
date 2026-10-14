# Test image standing in for an isolated Debian target machine.
#   docker build --build-arg BASE=debian:12 -t pipmeup-test:debian12 docker/
ARG BASE=debian:12
FROM ${BASE}

ENV DEBIAN_FRONTEND=noninteractive
COPY apt-packages.txt /tmp/apt-packages.txt
# If the base image already ships a Python (python:3.14-bookworm), keep it and
# install no system Python; otherwise take Debian's python3.
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
        $(command -v python3 >/dev/null || echo python3 python3-dev python3-venv python3-pip) \
 && sed 's/#.*//' /tmp/apt-packages.txt | xargs apt-get install -y --no-install-recommends \
 && rm -rf /var/lib/apt/lists/* /tmp/apt-packages.txt

WORKDIR /work
CMD ["python3", "/check/container_check.py"]
