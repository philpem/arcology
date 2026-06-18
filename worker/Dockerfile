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
FROM ubuntu:24.04 AS build-hxcfe

# Pinned commit SHA for the tag HxCFloppyEmulator_V2_16_13_1.
# Update this when upgrading HxCFE.  To find the SHA for a new tag:
#   git ls-remote https://github.com/jfdelnero/HxCFloppyEmulator.git refs/tags/<TAG>
# Current as of 2026-03-26.
ARG HXCFE_SHA=0a8f492d9b96e7c35283cf4ddf629c802f63fcf1

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
	git clone https://github.com/jfdelnero/HxCFloppyEmulator.git && \
	cd HxCFloppyEmulator && git checkout "${HXCFE_SHA}" && \
	cd build && make ${MINUSJ} HxCFloppyEmulator_cmdline && \
	cd /build/HxCFloppyEmulator/build && \
	cp -r lib* /usr/local/lib && \
	cp hxcfe /usr/local/bin && \
	cd / && rm -rf /build


#################
# Build common Rust build image
#################
FROM ubuntu:24.04 AS rustbuild

# Disable prompt during package installation
ARG DEBIAN_FRONTEND=noninteractive

# Update apt repository and install needed packages
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt -y update && apt -y upgrade && \
	apt -y install git rustup gcc && \
	rustup default stable


#################
# Build Fluxfox
#################
FROM rustbuild AS build-fluxfox

# Pinned commit SHA — update this when upgrading fluxfox.
# Current as of 2026-03-26; branch: main
ARG FLUXFOX_TAG=32fa0e5dd75346fccaeb466922289deeb12ccccc

# Disable prompt during package installation
ARG DEBIAN_FRONTEND=noninteractive

# Set number of parallel compiler instances
ARG MINUSJ=-j3

RUN	git clone https://github.com/dbalsom/fluxfox && \
	cd fluxfox && \
	git checkout "${FLUXFOX_TAG}" && \
	cargo build -r ${MINUSJ} -p imgviz -p fluxfox_cli && \
	cp target/release/imgviz /usr/local/bin && \
	cp target/release/fluxfox_cli /usr/local/bin


#################
# Build wmf-rs
#################
FROM rustbuild AS build-wmfrs

# Pinned commit SHA — update this when upgrading fluxfox.
# Current as of 2026-03-26; branch: main
ARG WMFRS_TAG=bad045de42e9b4142010cfc62d6995a03b3c65ab

# Above sha is a local fix (for POLYPOLYGON elems) awaiting merge upstream
#ARG WFMRS_REPO=https://github.com/mythrnr/wmf-rs.git
ARG WFMRS_REPO=https://github.com/philpem/wmf-rs.git

# Disable prompt during package installation
ARG DEBIAN_FRONTEND=noninteractive

# Set number of parallel compiler instances
ARG MINUSJ=-j3

RUN git clone "${WFMRS_REPO}" && \
	cd wmf-rs && \
	git checkout "${WMFRS_TAG}" && \
	cargo build ${MINUSJ} -r -p wmf-cli && \
	cp target/release/wmf-cli /usr/local/bin


#################
# Build DiscImageManager - toolchain layer
#
# Installs Lazarus/FPC and its ~700 packages.  No version-specific ARGs are
# declared here, so Docker can cache this layer independently of the DIM
# source SHA/repo.  Changing the git hash or repo URL only invalidates the
# build stage below, not this one.
#################
FROM ubuntu:24.04 AS build-dim-toolchain

# If 1, install Lazarus for a source build; if 0, only wget/unzip are needed.
ARG DISCIMAGEMANAGER_SOURCEBUILD=1

ARG DEBIAN_FRONTEND=noninteractive

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
	apt -y update && apt -y upgrade && \
	apt -y install unzip wget && \
	if [ "${DISCIMAGEMANAGER_SOURCEBUILD}" = "1" ]; then \
		apt -y install git lazarus ; \
	fi

# Pre-compile the FPC RTL and Lazarus base packages (LazUtils etc.) so they
# are frozen into this layer.  Without this, lazbuild does a full first-run
# compilation inside build-discimagemanager on every rebuild that changes the
# DIM source SHA, even though the toolchain itself hasn't changed.
#
# Step 1: compile a trivial FPC program — forces RTL .ppu files to be written
#         (a no-op if apt already installed pre-compiled units, which is fine).
# Step 2: build a minimal Lazarus project — forces LazUtils and the other base
#         packages to be compiled and cached under /root/.lazarus/.
COPY worker/lazarus-warmup /tmp/lazwarmup
RUN if [ "${DISCIMAGEMANAGER_SOURCEBUILD}" = "1" ]; then \
	printf 'begin end.\n' > /tmp/fpcwarmup.pas && \
	fpc /tmp/fpcwarmup.pas -o/tmp/fpcwarmup && \
	rm -f /tmp/fpcwarmup.pas /tmp/fpcwarmup /tmp/fpcwarmup.o && \
	cd /tmp/lazwarmup && lazbuild warmup.lpi && \
	rm -rf /tmp/lazwarmup /tmp/fpcwarmup* ; \
fi


#################
# Build DiscImageManager
#################
FROM build-dim-toolchain AS build-discimagemanager

# If 1, build from source
ARG DISCIMAGEMANAGER_SOURCEBUILD=1

# Github repo location
# Currently we use a fork, but it would be nice to move back to upstream.
#ARG DISCIMAGEMANAGER_REPO=https://github.com/geraldholdsworth/DiscImageManager
ARG DISCIMAGEMANAGER_REPO=https://github.com/philpem/DiscImageManager

# Pinned commit SHAs/tags: update these when upgrading DiscImageManager or its deps.
# Current as of 2026-04-02.
#ARG DISCIMAGEMANAGER_VERSION=1.49
ARG DISCIMAGEMANAGER_VERSION=285e859658a6b88e4324eb299efa7f73ef91a74a

# SpriteToBitmap and GJHCustomComponents pinned commit SHAs
ARG DISCIMAGEMANAGER_STB_VERSION=2cbd3ac945186bb5ed7e31685ee4e8287589ddd9
ARG DISCIMAGEMANAGER_GJHCC_VERSION=9f1397c7cb69aa117cfebf687eee701866f9da7c

# Binary release version — only used when DISCIMAGEMANAGER_SOURCEBUILD=0.
# Kept separate from DISCIMAGEMANAGER_VERSION (a commit SHA for source builds)
# so the binary download URL stays valid when the source SHA changes.
ARG DISCIMAGEMANAGER_BINARY_VERSION=1.49
ARG DISCIMAGEMANAGER_URL=https://github.com/geraldholdsworth/DiscImageManager/releases/download/${DISCIMAGEMANAGER_BINARY_VERSION}/Disc.Image.Manager.${DISCIMAGEMANAGER_BINARY_VERSION}.Linux.64.bit.Intel.zip

ARG DEBIAN_FRONTEND=noninteractive

RUN	\
	if [ "${DISCIMAGEMANAGER_SOURCEBUILD}" = "1" ]; then \
		echo "Source build of DiscImageManager --" ; \
		echo "  DIM   branch: ${DISCIMAGEMANAGER_VERSION}" ; \
		echo "  STB   branch: ${DISCIMAGEMANAGER_STB_VERSION}" ; \
		echo "  GJHCC branch: ${DISCIMAGEMANAGER_GJHCC_VERSION}" ; \
		git clone ${DISCIMAGEMANAGER_REPO} DiscImageManager && \
		git -C DiscImageManager checkout "${DISCIMAGEMANAGER_VERSION}" && \
		git clone https://github.com/geraldholdsworth/SpriteToBitmap && \
		git -C SpriteToBitmap checkout "${DISCIMAGEMANAGER_STB_VERSION}" && \
		git clone https://github.com/geraldholdsworth/GJHCustomComponents && \
		git -C GJHCustomComponents checkout "${DISCIMAGEMANAGER_GJHCC_VERSION}" && \
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
		echo "Binary of DiscImageManager -- version ${DISCIMAGEMANAGER_BINARY_VERSION}" ; \
		wget ${DISCIMAGEMANAGER_URL} -O discimagemanager.zip && \
		unzip discimagemanager.zip && \
		cp DiscImageManager /usr/local/bin && \
		echo ; \
	fi


#################
# Build local extraction tools
#################
FROM ubuntu:24.04 AS build-arcology-tools

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
# Build scotch (Acorn Replay encoder/decoder/transcoder)
#################
FROM ubuntu:24.04 AS build-scotch

# Pinned commit SHA — update this when upgrading scotch.
# To find the SHA for a new commit:
#   git ls-remote https://github.com/philpem/scotch.git refs/heads/main
# Current as of 2026-06-15; branch: main.
ARG SCOTCH_VERSION=7451dbee25a8a43bbc4bd6b0a0c08f45e0564e0c
ARG SCOTCH_REPO=https://github.com/philpem/scotch.git

# Disable prompt during package installation
ARG DEBIAN_FRONTEND=noninteractive

# Set number of parallel compiler instances
ARG MINUSJ=-j3

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt -y update && apt -y upgrade && \
    apt -y install git build-essential cmake

# scotch is pure C11 + libm (vendored ARM emulator); no third-party runtime deps.
# We only need the transcoder (Replay -> raw RGB24 + WAV) and the inspector.
RUN git clone "${SCOTCH_REPO}" /src/scotch && \
    cd /src/scotch && git checkout "${SCOTCH_VERSION}" && \
    cmake -S . -B build -DCMAKE_BUILD_TYPE=Release && \
    cmake --build build ${MINUSJ} --target replay-transcode replay-inspect && \
    cp build/replay-transcode /usr/local/bin/ && \
    cp build/replay-inspect /usr/local/bin/


#################
# Build riscosarc
#################
FROM ubuntu:24.04 AS build-riscosarc

# Pinned commit SHA — update this when upgrading riscosarc.
# Current as of 2026-06-11; branch: master
ARG RISCOSARC_VERSION=a885d02abd71a22d68440c44e895651b68fef211
ARG RISCOSARC_REPO=https://github.com/mjwoodcock/riscosarc.git

ARG DEBIAN_FRONTEND=noninteractive

RUN apt -y update && apt -y upgrade && \
    apt -y install git build-essential default-jdk

# Clone and build riscosarc
RUN git clone "${RISCOSARC_REPO}" /src/riscosarc && \
    cd /src/riscosarc && git checkout "${RISCOSARC_VERSION}" && \
    mkdir build && \
    javac -d ./build *.java riscos/archive/*.java riscos/archive/container/*.java && \
    ( cd build && jar --create --file ../riscosarc.jar --main-class riscosarc * ) && \
    cp riscosarc.jar /usr/local/bin/

COPY worker/tools/riscosarc /usr/local/bin/


#################
# Build TBAFS extractor
#################
FROM ubuntu:24.04 AS build-tbafs

# Pinned commit SHA — update this when upgrading tbafs.
# Current as of 2026-03-26; branch: main
ARG TBAFS_VERSION=e91ef8ed7b83a77db52e342b752df282772b8838

ARG DEBIAN_FRONTEND=noninteractive

RUN apt -y update && apt -y upgrade && \
    apt -y install git build-essential

# Clone and build tbafs extractor
RUN git clone https://github.com/mattgodbolt/tbafs.git /src/tbafs && \
    cd /src/tbafs && git checkout "${TBAFS_VERSION}" && \
    cp tbafs.py /usr/local/bin/tbafs-extractor


#################
# Download unpackaged external tools
#################
FROM ubuntu:24.04 AS get-external
ARG DEBIAN_FRONTEND=noninteractive

RUN apt -y update && apt -y upgrade && \
    apt -y install wget

# Install the vibe2svg converters from Dexvert
ARG DEXVERT_SHA=64225a11efdc76c3be1c61d6a89504655749fc94
RUN mkdir -p /opt/dexvert && \
	wget https://raw.githubusercontent.com/Sembiance/dexvert/${DEXVERT_SHA}/bin/vibe2svg/emf/emf.py -O /opt/dexvert/emf2svg.py && \
	chmod +x /opt/dexvert/*.py


#################
# Assemble final image with tools + worker
#################

FROM ubuntu:24.04

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

# Copy scotch Acorn Replay transcoder (used with ffmpeg by REPLAY_TRANSCODE)
COPY --from=build-scotch /usr/local/bin/replay-transcode /usr/local/bin/
COPY --from=build-scotch /usr/local/bin/replay-inspect /usr/local/bin/
# Bundle the freeware Acorn Replay decompressor modules (Acorn freeware via
# RISC OS Open; Escape © Eidos 1993; LinePack © H. B. Pedersen 1995).  These are
# the decoders replay-transcode runs under its ARM simulator for compressed codecs.
COPY --from=build-scotch /src/scotch/vendor/armovie-codecs/ /usr/local/share/armovie-codecs/

# Copy image conversion tools
COPY --from=build-wmfrs /usr/local/bin/wmf-cli /usr/local/bin

# Copy Dexvert tools
COPY --from=get-external /opt/dexvert/emf2svg.py /opt/dexvert/

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
# - ffmpeg: mux scotch's decoded Replay frames into MP4 (REPLAY_TRANSCODE); also
#           the basis for future AVI/other-container output
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
	--mount=type=cache,target=/var/lib/apt,sharing=locked \
	sed -i '/^Components:/ s/$/ multiverse/' /etc/apt/sources.list.d/ubuntu.sources && \
	apt -y update && apt -y upgrade && \
	apt -y install \
		python3 python3-pip python3-dev python3-requests python3-numpy python3-boto3 git \
		p7zip-full zstd gzip bzip2 xz-utils \
		unzip unrar tar util-linux \
		file fdisk \
		default-jre \
		ffmpeg \
		libcairo2 libfontconfig1 python3-cairo python3-cairocffi python3-pillow \
		fonts-freefont-ttf \
		scour

# Install Greaseweazle for disk image conversion.
# Pinned to a specific commit SHA; update when upgrading (was: @latest).
# Current as of 2026-04-01.
ARG GREASEWEAZLE_VERSION=26690f89967d519e0106ab9566019a026b920bb4
RUN python3 -m pip install --break-system-packages \
	git+https://github.com/keirf/greaseweazle@${GREASEWEAZLE_VERSION}

# Install Sentry SDK for error and performance reporting (optional; enabled by SENTRY_DSN env var).
RUN python3 -m pip install --break-system-packages sentry-sdk

# Install Acorn image conversion libraries.
# drawfile_render (philpem fork with signed-coordinate fix) — not pip-installable; clone manually.
# Pinned to a specific commit SHA — update when upgrading.
# Current as of 2026-04-01.
# The repo also bundles spritefile.py (by David Boddie) which is not on PyPI;
# we expose it to the worker by setting PYTHONPATH.
# riscos-free-fonts is a submodule; --recurse-submodules fetches it, then
# submodule update pins it to the SHA recorded in the checked-out commit.
# philpem fork — adds self.warnings and fixes next==0 infinite loop in spritefile.py.
# Revert to dcf21 once the fix is merged upstream (PR pending).
ARG DRAWFILE_RENDER_SHA=f5bbbab90b8cf6951dfa43b885ed34d98e1005f6
ARG DRAWFILE_RENDER_REPO=https://github.com/philpem/drawfile-render
#ARG DRAWFILE_RENDER_SHA=7834d3bb626e68a51f49da145978d296599bc24d
#ARG DRAWFILE_RENDER_REPO=https://github.com/dcf21/drawfile-render.git
RUN git clone --recurse-submodules "${DRAWFILE_RENDER_REPO}" /opt/drawfile_render && \
    git -C /opt/drawfile_render checkout "${DRAWFILE_RENDER_SHA}" && \
    git -C /opt/drawfile_render submodule update --init --recursive

# Make spritefile (bundled in drawfile_render repo) importable by the worker
ENV PYTHONPATH="/opt/drawfile_render"

# Set locale so that unzip -O iso-8859-1 can output correctly-encoded UTF-8
# filenames. Without this, the default C locale causes unzip to fail the
# charset conversion from ISO-8859-1 to the target encoding.
ENV LANG=C.UTF-8

# Clean up build dependencies
RUN apt -y remove python3-dev git && apt -y autoremove

# Copy worker script, package, and shared module
WORKDIR /app
COPY worker/worker.py .
COPY worker/arcworker/ ./arcworker/
COPY arcology_shared/ ./arcology_shared/

# Environment variables (override at runtime)
ENV ARCOLOGY_API=http://host.docker.internal:5000/api
ENV UPLOAD_DIR=/data/uploads
ENV OUTPUT_DIR=/data/outputs
ENV POLL_INTERVAL=10
ENV LOG_LEVEL=INFO
ENV TOOL_TIMEOUT=3600
# Acorn Replay decompressor modules for transcoding compressed codecs, bundled
# from scotch above. Override to point at a different module set if needed.
# See doc/REPLAY_TRANSCODE.md.
ENV REPLAY_MODULES_DIR=/usr/local/share/armovie-codecs

# Create data directories
RUN mkdir -p /data/uploads /data/outputs

# Run worker
CMD ["python3", "worker.py"]
