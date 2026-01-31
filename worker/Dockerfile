#################
# Arcology Analysis Worker
#
# Multi-stage build that compiles analysis tools and includes the worker script.
#
# Build:
#   docker build -t arcology-worker -f worker/Dockerfile .
#
# Run single worker:
#   docker run -v /path/to/uploads:/data/uploads \
#              -v /path/to/outputs:/data/outputs \
#              -e ARCOLOGY_API=http://host.docker.internal:5000/api \
#              arcology-worker
#
# Run multiple workers (for parallel processing):
#   docker-compose up -d --scale worker=4
#
#################


#################
# Build HxCFE
#################
FROM debian:stable AS build-hxcfe

# Version to build
ARG HXCFE_TAG=HxCFloppyEmulator_V2_16_13_1

# Disable Prompt During Packages Installation
ARG DEBIAN_FRONTEND=noninteractive

# Set number of parallel compiler instances
ARG MINUSJ=-j3

# Update apt repository and install needed packages
RUN apt -y update && apt -y upgrade && \
	apt -y install git gcc make

# Build HxCFE
RUN \
	mkdir /build && cd /build && \
	git clone --branch "${HXCFE_TAG}" --depth=1 https://github.com/jfdelnero/HxCFloppyEmulator.git && \
	cd HxCFloppyEmulator/build && make ${MINUSJ} HxCFloppyEmulator_cmdline && \
	cd /build/HxCFloppyEmulator/build && \
	cp -r lib* /usr/local/lib && \
	cp hxcfe /usr/local/bin && \
	cd / && rm -rf /build


#################
# Build Fluxfox
#################
FROM debian:stable AS build-fluxfox

# Version to build
ARG FLUXFOX_TAG=main

# Disable Prompt During Packages Installation
ARG DEBIAN_FRONTEND=noninteractive

# Set number of parallel compiler instances
ARG MINUSJ=-j3

# Update apt repository and install needed packages
RUN apt -y update && apt -y upgrade && \
	apt -y install git rustup gcc && \
	rustup default stable && \
	git clone --branch "${FLUXFOX_TAG}" --depth=1 https://github.com/dbalsom/fluxfox && \
	cd fluxfox && \
	cargo update && \
	cargo build -r ${MINUSJ} -p imgviz -p fluxfox_cli && \
	cp target/release/imgviz /usr/local/bin && \
	cp target/release/fluxfox_cli /usr/local/bin


#################
# Build DiscImageManager
#################
FROM debian:stable AS build-discimagemanager

# Version to build
ARG DISCIMAGEMANAGER_VERSION=1.49
# Binary build location -- you shouldn't have to change this
ARG DISCIMAGEMANAGER_URL=https://github.com/geraldholdsworth/DiscImageManager/releases/download/${DISCIMAGEMANAGER_VERSION}/Disc.Image.Manager.${DISCIMAGEMANAGER_VERSION}.Linux.64.bit.Intel.zip
# If 1, build from source
ARG DISCIMAGEMANAGER_SOURCEBUILD=1
# SpriteToBitmap unit version, defaults to "main" which is the latest dev version
ARG DISCIMAGEMANAGER_STB_VERSION=main
ARG DISCIMAGEMANAGER_GJHCC_VERSION=main

# Disable Prompt During Packages Installation
ARG DEBIAN_FRONTEND=noninteractive

# This is split into two stages because the source build needs Lazarus,
# which in turn needs about 700 packages and a full X server.
# It takes a long time to download and install these.
RUN \
	apt -y update && apt -y upgrade && \
	apt -y install unzip wget && \
	if [ ${DISCIMAGEMANAGER_SOURCEBUILD} -eq 1 ]; then \
		apt -y install git lazarus ;  \
	fi

RUN	\
	if [ ${DISCIMAGEMANAGER_SOURCEBUILD} -eq 1 ]; then \
		echo "Source build of DiscImageManager --" ; \
		echo "  DIM   branch: ${DISCIMAGEMANAGER_VERSION}" ; \
		echo "  STB   branch: ${DISCIMAGEMANAGER_STB_VERSION}" ; \
		echo "  GJHCC branch: ${DISCIMAGEMANAGER_GJHCC_VERSION}" ; \
		git clone --branch "${DISCIMAGEMANAGER_VERSION}" --depth=1 https://github.com/geraldholdsworth/DiscImageManager && \
		git clone --branch "${DISCIMAGEMANAGER_STB_VERSION}" --depth=1 https://github.com/geraldholdsworth/SpriteToBitmap && \
		git clone --branch "${DISCIMAGEMANAGER_GJHCC_VERSION}" --depth=1 https://github.com/geraldholdsworth/GJHCustomComponents && \
		cd DiscImageManager && \
		ln -s ../GJHCustomComponents/Source GJHCustomComponents && \
		ln -s ../GJHCustomComponents/Source GJHCustomConponents && \
		ln -s ../SpriteToBitmap/Lazarus\ Source SpriteToBitmap && \
		cd GJHCustomComponents && \
		echo "--- Building GJHCustomComponents ---" && \
		lazbuild GJHComponents.lpi && \
		cd .. && \
		cd LazarusSource && \
		echo "--- Building DiscImageManager ---" && \
		lazbuild DiscImageManager.lpi --build-mode="Release Linux 64 bit" && \
		cp ./lib/Release/x86_64-linux/DiscImageManager /usr/local/bin && \
		echo ; \
	else	\
		echo "Binary of DiscImageManager -- version ${DISCIMAGEMANAGER_VERSION}" ; \
		wget ${DISCIMAGEMANAGER_URL} -O discimagemanager.zip && \
		unzip discimagemanager.zip && \
		cp DiscImageManager /usr/local/bin && \
		echo ; \
	fi


#################
# Assemble final image with tools + worker
#################

FROM debian:stable

LABEL maintainer="philpem@philpem.me.uk"
LABEL version="0.1"
LABEL description="Arcology analysis worker"

# Copy HxCFE
COPY --from=build-hxcfe /usr/local/bin/hxcfe /usr/local/bin/
COPY --from=build-hxcfe /usr/local/lib/lib* /usr/local/lib/

# Copy Fluxfox
COPY --from=build-fluxfox /usr/local/bin/imgviz /usr/local/bin/
COPY --from=build-fluxfox /usr/local/bin/fluxfox_cli /usr/local/bin/

# Copy DiscImageManager
COPY --from=build-discimagemanager /usr/local/bin/DiscImageManager /usr/local/bin/

# Rebuild shared library cache (needed by hxcfe)
RUN ldconfig

# Disable Prompt During Packages Installation
ARG DEBIAN_FRONTEND=noninteractive

# Install runtime dependencies:
# - libgtk2.0-0, xvfb: for DiscImageManager
# - python3, python3-pip: for Greaseweazle and worker
# - p7zip-full: for file extraction
# - zstd, gzip, bzip2: for decompression
RUN \
	apt -y update && apt -y upgrade && \
	apt -y install \
		libgtk2.0-0 xvfb \
		python3 python3-pip python3-dev git \
		p7zip-full zstd gzip bzip2 \
	&& rm -rf /var/lib/apt/lists/*

# Install Python packages:
# - Greaseweazle for disk image conversion
# - requests for API communication
RUN python3 -m pip install --break-system-packages \
	git+https://github.com/keirf/greaseweazle@latest \
	requests

# Clean up build dependencies
RUN apt -y remove python3-dev git && apt -y autoremove

# Copy worker script
WORKDIR /app
COPY worker/worker.py .

# Environment variables (override at runtime)
ENV ARCOLOGY_API=http://host.docker.internal:5000/api
ENV UPLOAD_DIR=/data/uploads
ENV OUTPUT_DIR=/data/outputs
ENV POLL_INTERVAL=30
ENV LOG_LEVEL=INFO

# Create data directories
RUN mkdir -p /data/uploads /data/outputs

# Run worker
CMD ["python3", "worker.py"]
