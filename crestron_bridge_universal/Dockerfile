ARG BUILD_FROM
FROM $BUILD_FROM

# Alpine-based HA base image. Install Python + pip.
RUN apk add --no-cache python3 py3-pip

# Install cipclient. --break-system-packages required on Alpine (PEP 668);
# safe inside a container.
RUN pip3 install --break-system-packages python-cipclient

# Bridge code + startup wrapper
COPY crestron_bridge.py /app/crestron_bridge.py
COPY run.sh /run.sh
RUN chmod a+x /run.sh

WORKDIR /app

CMD [ "/run.sh" ]
