# Test image standing in for an isolated target machine.
#   docker build --build-arg BASE=debian:12 -t pipmeup-test:debian12 docker/
#   docker build --build-arg BASE=ubuntu:20.04 --build-arg PYTHON_VERSION=3.14 \
#                -t pipmeup-test:py314 docker/
#
# PYTHON_VERSION empty : use the distribution's own python3 (Debian 12 -> 3.11, 13 -> 3.13).
# PYTHON_VERSION set   : install that CPython through uv (python-build-standalone,
#                        needs glibc >= 2.17), for releases the distribution lacks.
ARG BASE=debian:12
FROM ${BASE}

ARG PYTHON_VERSION=
ENV DEBIAN_FRONTEND=noninteractive
COPY apt-packages.txt apt-optional.txt /tmp/

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
        $(if [ -z "$PYTHON_VERSION" ]; then echo python3 python3-dev python3-venv python3-pip; else echo python3-pip python3-setuptools; fi) \
 && sed 's/#.*//' /tmp/apt-packages.txt | xargs apt-get install -y --no-install-recommends \
 && sed 's/#.*//' /tmp/apt-optional.txt | xargs -r -n1 sh -c \
        'apt-get install -y --no-install-recommends "$0" >/dev/null 2>&1 || echo "skipped optional package $0"' \
 && rm -rf /var/lib/apt/lists/* /tmp/apt-*.txt

RUN if [ -n "$PYTHON_VERSION" ]; then \
        pip3 install uv \
     && UV_PYTHON_INSTALL_DIR=/opt/python uv python install "$PYTHON_VERSION" \
     && ln -s "$(ls -d /opt/python/cpython-*/bin/python3 | head -n1)" /usr/local/bin/python3; \
    fi

WORKDIR /work
CMD ["python3", "/check/container_check.py"]
