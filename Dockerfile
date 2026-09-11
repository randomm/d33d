# d33d render worker image
# Sandboxed OpenSCAD renderer — compiles LLM-generated .scad into STL, CSG, and six PNG renders.
# BOSL2 is baked in (not mounted) and pinned to an explicit tag via --build-arg BOSL2_TAG.
# See docs/bosl2-pinning.md for the pinning policy.
#
# Pinned base image: docker.io/openscad/openscad:trixie
#   Confirmed: Debian GNU/Linux 13 (trixie), OpenSCAD version 2026.01.19
#   The --camera, --autocenter, --projection, --imgsize, and --colorscheme options
#   are all present in this build. See the "Empirical CLI verification" section of
#   docs/bosl2-pinning.md for the verified flag set.

FROM --platform=linux/amd64 docker.io/openscad/openscad:trixie

# BOSL2_TAG is a build-arg with NO DEFAULT — a build without it fails loudly.
# This is a hard requirement of the spec: the image must not silently drift.
ARG BOSL2_TAG

# Fail loudly if BOSL2_TAG was not supplied.
# The test -n check is a shell builtin that works in both /bin/sh and /bin/bash.
# (Bash-only syntax like ${VAR:?msg} would fail under /bin/sh which is the default
#  shell for Dockerfile RUN instructions.)
RUN test -n "${BOSL2_TAG}"

# git is not in the base image; install it to fetch BOSL2 at build time.
RUN apt-get update && \
    apt-get install -y --no-install-recommends git ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Create the openscad user (uid 1000) and set it as the runtime user.
# The USER directive is explicit — the container must never run as root.
RUN groupadd -g 1000 openscad && \
    useradd -u 1000 -g openscad -m -d /home/openscad -s /bin/bash openscad

# Vendored BOSL2 — shallow clone of the pinned tag into /opt/openscad-libs/BOSL2.
# The OPENSCADPATH env var lets `include <BOSL2/std.scad>` resolve from the vendored path.
RUN git clone --depth 1 --branch "${BOSL2_TAG}" \
        https://github.com/BelfrySCAD/BOSL2 \
        /opt/openscad-libs/BOSL2 && \
    chown -R openscad:openscad /opt/openscad-libs

# The /work volume is mounted rw at runtime by the caller.
# It is not baked in — the image rootfs is read-only at runtime.
RUN mkdir -p /work && chown openscad:openscad /work

ENV OPENSCADPATH=/opt/openscad-libs
WORKDIR /work

# The entrypoint is a bash script that runs the eight sequential openscad invocations.
# It is copied in as root (before the USER switch) and made executable as root.
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

USER openscad

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
