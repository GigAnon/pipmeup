# Test image standing in for an isolated Debian target machine.
#   docker build --build-arg BASE=debian:12 -t pipmeup-test:debian12 docker/
ARG BASE=debian:12
FROM ${BASE}

ENV DEBIAN_FRONTEND=noninteractive
COPY apt-packages.txt /tmp/apt-packages.txt
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        build-essential python3 python3-dev python3-venv python3-pip \
 && sed 's/#.*//' /tmp/apt-packages.txt | xargs apt-get install -y --no-install-recommends \
 && rm -rf /var/lib/apt/lists/* /tmp/apt-packages.txt

WORKDIR /work
CMD ["python3", "/check/container_check.py"]
