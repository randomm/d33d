# d33d render worker image
# Sandboxed OpenSCAD renderer — compiles LLM-generated .scad into STL, CSG, and six PNG renders.
# BOSL2 is baked in (not mounted), pinned to an explicit tag via --build-arg BOSL2_TAG, and the
# tag's resolved commit is verified against an explicit --build-arg BOSL2_COMMIT so a retargeted
# (moved) tag ref cannot silently inject unverified code into the image.
# See docs/bosl2-pinning.md for the pinning policy.
#
# Pinned base image: docker.io/openscad/openscad:trixie
#   Confirmed: Debian GNU/Linux 13 (trixie), OpenSCAD version 2026.01.19
#   The --camera, --autocenter, --projection, --imgsize, and --colorscheme options
#   are all present in this build. See the "Empirical CLI verification" section of
#   docs/bosl2-pinning.md for the verified flag set.

FROM --platform=linux/amd64 docker.io/openscad/openscad:trixie

# BOSL2_TAG and BOSL2_COMMIT are build-args with NO DEFAULT — a build without either fails loudly.
# This is a hard requirement of the spec: the image must not silently drift.
ARG BOSL2_TAG
ARG BOSL2_COMMIT

# D33D_BUILD_HASH is the sha256 (hex) of the repo-root entrypoint.sh +
# Dockerfile + the two BOSL2 build-arg values, computed host-side by the
# canonical build command (docs/bosl2-pinning.md) and stamped into the
# image below as the ``d33d/build-hash`` label. The server recomputes the
# same hash from the working tree before every render and refuses to run a
# render-worker image whose label is missing or disagrees (issue #236).
# No default: a build without it fails loudly below, exactly like the
# BOSL2 args — an unlabeled image is precisely the stale-image failure mode
# this guard exists to catch.
ARG D33D_BUILD_HASH

# Fail loudly if BOSL2_TAG or BOSL2_COMMIT was not supplied.
# The test -n check is a shell builtin that works in both /bin/sh and /bin/bash.
# (Bash-only syntax like ${VAR:?msg} would fail under /bin/sh which is the default
#  shell for Dockerfile RUN instructions.)
RUN test -n "${BOSL2_TAG}" && test -n "${BOSL2_COMMIT}" && test -n "${D33D_BUILD_HASH}"

# git is not in the base image; install it to fetch BOSL2 at build time.
RUN apt-get update && \
    apt-get install -y --no-install-recommends git ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Create the openscad user (uid 1000) and set it as the runtime user.
# The USER directive is explicit — the container must never run as root.
RUN groupadd -g 1000 openscad && \
    useradd -u 1000 -g openscad -m -d /home/openscad -s /bin/bash openscad

# Vendored BOSL2 — shallow clone of the pinned tag into /opt/openscad-libs/BOSL2, then verified
# against the pinned commit SHA. A tag ref is mutable (upstream can retarget it to point at a
# different commit); pinning the tag alone would let a supply-chain compromise of the tag ref
# inject unverified code into every render. Resolving the clone's HEAD commit and comparing it
# against BOSL2_COMMIT closes that gap: if the tag has been moved, the build fails loudly instead
# of silently vendoring different code.
RUN git clone --depth 1 --branch "${BOSL2_TAG}" \
        https://github.com/BelfrySCAD/BOSL2 \
        /opt/openscad-libs/BOSL2 && \
    cd /opt/openscad-libs/BOSL2 && \
    resolved_commit="$(git rev-parse HEAD)" && \
    if [ "${resolved_commit}" != "${BOSL2_COMMIT}" ]; then \
        echo "BOSL2 tag ${BOSL2_TAG} resolved to ${resolved_commit}, expected ${BOSL2_COMMIT} — refusing to build" >&2; \
        exit 1; \
    fi && \
    rm -rf /opt/openscad-libs/BOSL2/.git && \
    cd / && \
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

# Stamp the content hash of the render-affecting build inputs (see the
# D33D_BUILD_HASH ARG above) as the ``d33d/build-hash`` image label. The
# server recomputes the same hash from its working tree and compares it to
# this label before every render (issue #236): a missing or stale label
# fails loudly instead of rendering with baked-in code that no longer
# matches the source.
LABEL d33d/build-hash="${D33D_BUILD_HASH}"

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
