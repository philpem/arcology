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

# Disable prompt during package installation
ARG DEBIAN_FRONTEND=noninteractive

# Set number of parallel compiler instances
ARG MINUSJ=-j3

# Update apt repository and install needed packages
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt -y update && apt -y upgrade && \
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

# Disable prompt during package installation
ARG DEBIAN_FRONTEND=noninteractive

# Set number of parallel compiler instances
ARG MINUSJ=-j3

# Update apt repository and install needed packages
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt -y update && apt -y upgrade && \
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
#ARG DISCIMAGEMANAGER_VERSION=1.49
ARG DISCIMAGEMANAGER_VERSION=feature/decouple-ui-from-cli
# Binary build location -- you shouldn't have to change this
ARG DISCIMAGEMANAGER_URL=https://github.com/geraldholdsworth/DiscImageManager/releases/download/${DISCIMAGEMANAGER_VERSION}/Disc.Image.Manager.${DISCIMAGEMANAGER_VERSION}.Linux.64.bit.Intel.zip
# Github repo location
#ARG DISCIMAGEMANAGER_REPO=https://github.com/geraldholdsworth/DiscImageManager
ARG DISCIMAGEMANAGER_REPO=https://github.com/philpem/DiscImageManager
# If 1, build from source
ARG DISCIMAGEMANAGER_SOURCEBUILD=1
# SpriteToBitmap unit version, defaults to "main" which is the latest dev version
ARG DISCIMAGEMANAGER_STB_VERSION=main
ARG DISCIMAGEMANAGER_GJHCC_VERSION=main

# Disable prompt during package installation
ARG DEBIAN_FRONTEND=noninteractive

# This is split into two stages because the source build needs Lazarus,
# which in turn needs about 700 packages and a full X server.
# It takes a long time to download and install these.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
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
		git clone --branch "${DISCIMAGEMANAGER_VERSION}" --depth=1 ${DISCIMAGEMANAGER_REPO} && \
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
		lazbuild DiscImageManagerCLI.lpi --build-mode="Release Linux 64 bit" && \
		cp ./lib-cli/Release/x86_64-linux/DiscImageManagerCLI /usr/local/bin/DiscImageManager && \
		echo ; \
	else	\
		echo "Binary of DiscImageManager -- version ${DISCIMAGEMANAGER_VERSION}" ; \
		wget ${DISCIMAGEMANAGER_URL} -O discimagemanager.zip && \
		unzip discimagemanager.zip && \
		cp DiscImageManager /usr/local/bin && \
		echo ; \
	fi


#################
# Build local extraction tools
#################
FROM debian:stable AS build-arcology-tools

# Disable prompt during package installation
ARG DEBIAN_FRONTEND=noninteractive

# This is split into two stages so we don't have to re-download the packages when the source code changes.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
	apt -y update && apt -y upgrade && \
	apt -y install build-essential

COPY worker/tools /src

RUN \
    cd /src && \
    gcc -O3 -o /usr/local/bin/fcfs2raw fcfs2raw.c


#################
# Build riscosarc
#################
FROM debian:stable AS build-riscosarc

ARG DEBIAN_FRONTEND=noninteractive

RUN apt -y update && apt -y upgrade && \
    apt -y install git build-essential default-jdk

# Clone and build riscosarc
RUN git clone https://github.com/mjwoodcock/riscosarc.git /src/riscosarc && \
    cd /src/riscosarc && \
    mkdir build && \
    javac -d ./build *.java riscos/archive/*.java riscos/archive/container/*.java && \
    ( cd build && jar --create --file ../riscosarc.jar --main-class riscosarc * ) && \
    cp riscosarc.jar /usr/local/bin/

COPY worker/tools/riscosarc /usr/local/bin/


#################
# Build TBAFS extractor
#################
FROM debian:stable AS build-tbafs

ARG DEBIAN_FRONTEND=noninteractive

RUN apt -y update && apt -y upgrade && \
    apt -y install git build-essential

# Clone and build tbafs extractor
RUN git clone https://github.com/mattgodbolt/tbafs.git /src/tbafs && \
    cd /src/tbafs && \
    cp tbafs.py /usr/local/bin/tbafs-extractor


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

# Copy local tools
COPY --from=build-arcology-tools /usr/local/bin/fcfs2raw /usr/local/bin/

# Copy archive extraction tools
COPY --from=build-riscosarc /usr/local/bin/riscosarc.jar /usr/local/bin/
COPY --from=build-riscosarc /usr/local/bin/riscosarc /usr/local/bin/
COPY --from=build-tbafs /usr/local/bin/tbafs-extractor /usr/local/bin/


# Rebuild shared library cache (needed by hxcfe)
RUN ldconfig

# Disable prompt during package installation
ARG DEBIAN_FRONTEND=noninteractive

# Install runtime dependencies:
# - python3, python3-pip, python3-requests: for Greaseweazle and worker
# - p7zip-full: for file extraction and 7z archives
# - zstd, gzip, bzip2, xz-utils: for decompression
# - unzip, unrar: for ZIP and RAR archives
# - tar: for TAR archives
# - util-linux: for sfdisk (partition detection)
# - file: GNU File for file format identification (TODO, look at replacing with Dexvert)
# - fdisk: For sfdisk - used to carve up PC partitions
# - default-jre: Java JRE for riscosarc
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
	--mount=type=cache,target=/var/lib/apt,sharing=locked \
	sed -i -e 's/main/main non-free/' /etc/apt/sources.list.d/debian.sources && \
	apt -y update && apt -y upgrade && \
	apt -y install \
		python3 python3-pip python3-dev python3-requests python3-numpy git \
		p7zip-full zstd gzip bzip2 xz-utils \
		unzip unrar tar util-linux \
		file fdisk \
		default-jre

# Install Greaseweazle for disk image conversion
RUN python3 -m pip install --break-system-packages \
	git+https://github.com/keirf/greaseweazle@latest

# Clean up build dependencies
RUN apt -y remove python3-dev git && apt -y autoremove

# Copy worker script and package
WORKDIR /app
COPY worker/worker.py .
COPY worker/arcworker/ ./arcworker/

# Environment variables (override at runtime)
ENV ARCOLOGY_API=http://host.docker.internal:5000/api
ENV UPLOAD_DIR=/data/uploads
ENV OUTPUT_DIR=/data/outputs
ENV POLL_INTERVAL=30
ENV LOG_LEVEL=INFO
ENV TOOL_TIMEOUT=3600

# Create data directories
RUN mkdir -p /data/uploads /data/outputs

# Run worker
CMD ["python3", "worker.py"]
