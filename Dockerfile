# traintap decoder image — bundles librtlsdr + the pinned pyrtlsdr so users
# never have to fight the host install. Run with USB access to the RTL-SDR:
#   docker run --rm --device=/dev/bus/usb ghcr.io/trickv/traintap --mode eot
FROM python:3.12-slim

# librtlsdr0 = runtime lib pyrtlsdr needs; rtl-sdr = rtl_test for debugging.
RUN apt-get update \
 && apt-get install -y --no-install-recommends rtl-sdr librtlsdr0 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml requirements.txt README.md ./
COPY traintap ./traintap
# pyproject pins pyrtlsdr==0.2.93 + setuptools<81 (works with stock librtlsdr).
RUN pip install --no-cache-dir .

VOLUME /data
ENTRYPOINT ["traintap"]
# Default: park on EOT (the busy frequency) and log to /data. DPU is decoded
# from the same capture automatically. Override the command for scan/hot/etc.
CMD ["--mode", "eot", \
     "--csv", "/data/trains.csv", \
     "--passes-csv", "/data/passes.csv", \
     "--signal-csv", "/data/signal.csv"]
