# syntax=docker/dockerfile:1.4

############################################################################
# Change/Verify these values when adopting this Dockerfile into another org:
#   GH_ORG, IMAGE_NAMESPACE,
#   DATA_ASSIMILATION_ORG, DATA_ASSIMILATION_REF,
#   EWTS_ORG, EWTS_REF,
#   MSW_MGR_ORG, MSW_MGR_REF,
#   NGEN_ORG, NGEN_REF,
#   NGEN_FORCING_ORG, NGEN_FORCING_REF
############################################################################

# Ownership / branding overrides
ARG GH_ORG=NOAA-OWP
ARG IMAGE_NAMESPACE=noaa-owp

# External repository sources (org and ref/branch overrides)
ARG DATA_ASSIMILATION_ORG=${GH_ORG}
ARG DATA_ASSIMILATION_REF=development
ARG EWTS_ORG=${GH_ORG}
ARG EWTS_REF=development
ARG MSW_MGR_ORG=${GH_ORG}
ARG MSW_MGR_REF=development
ARG NGEN_ORG=${GH_ORG}
ARG NGEN_REF=development
ARG NGEN_FORCING_ORG=${GH_ORG}
ARG NGEN_FORCING_REF=development

############################################################################
# Image selection
############################################################################

ARG BASE_REPO=python
ARG BASE_TAG=3.12-slim-bookworm

FROM ${BASE_REPO}:${BASE_TAG}

# Re-expose args after FROM for the remaining build stage
# Keeps whatever value was already set
ARG GH_ORG
ARG IMAGE_NAMESPACE
ARG DATA_ASSIMILATION_ORG
ARG DATA_ASSIMILATION_REF
ARG EWTS_ORG
ARG EWTS_REF
ARG MSW_MGR_ORG
ARG MSW_MGR_REF
ARG NGEN_ORG
ARG NGEN_REF
ARG NGEN_FORCING_ORG
ARG NGEN_FORCING_REF

# OCI Metadata Arguments
ARG BASE_REPO
ARG BASE_TAG
ARG BASE_NAME="${BASE_REPO}:${BASE_TAG}"
ARG BASE_DIGEST="unknown"
ARG BASE_REVISION="unknown"
ARG IMAGE_SOURCE="unknown"
ARG IMAGE_VENDOR="unknown"
ARG IMAGE_VERSION="unknown"
ARG IMAGE_REVISION="unknown"
ARG DATA_ASSIMILATION_REVISION="unknown"
ARG EWTS_REVISION="unknown"
ARG MSW_MGR_REVISION="unknown"
ARG NGEN_REVISION="unknown"
ARG NGEN_FORCING_REVISION="unknown"

# Image Labels: OCI-spec annotations followed by custom source-repo metadata.
# Single LABEL instruction per Docker best practices.
LABEL org.opencontainers.image.base.name="${BASE_NAME}" \
    org.opencontainers.image.base.digest="${BASE_DIGEST}" \
    org.opencontainers.image.source="${IMAGE_SOURCE}" \
    org.opencontainers.image.vendor="${IMAGE_VENDOR}" \
    org.opencontainers.image.version="${IMAGE_VERSION}" \
    org.opencontainers.image.revision="${IMAGE_REVISION}" \
    org.opencontainers.image.title="NGENCERF Server" \
    org.opencontainers.image.description="Docker image for the NGENCERF server application" \
    io.${IMAGE_NAMESPACE}.image.base.revision="${BASE_REVISION}" \
    io.${IMAGE_NAMESPACE}.data.assimilation.org="${DATA_ASSIMILATION_ORG}" \
    io.${IMAGE_NAMESPACE}.data.assimilation.ref="${DATA_ASSIMILATION_REF}" \
    io.${IMAGE_NAMESPACE}.data.assimilation.revision="${DATA_ASSIMILATION_REVISION}" \
    io.${IMAGE_NAMESPACE}.ewts.org="${EWTS_ORG}" \
    io.${IMAGE_NAMESPACE}.ewts.ref="${EWTS_REF}" \
    io.${IMAGE_NAMESPACE}.ewts.revision="${EWTS_REVISION}" \
    io.${IMAGE_NAMESPACE}.msw.mgr.org="${MSW_MGR_ORG}" \
    io.${IMAGE_NAMESPACE}.msw.mgr.ref="${MSW_MGR_REF}" \
    io.${IMAGE_NAMESPACE}.msw.mgr.revision="${MSW_MGR_REVISION}" \
    io.${IMAGE_NAMESPACE}.ngen.org="${NGEN_ORG}" \
    io.${IMAGE_NAMESPACE}.ngen.ref="${NGEN_REF}" \
    io.${IMAGE_NAMESPACE}.ngen.revision="${NGEN_REVISION}" \
    io.${IMAGE_NAMESPACE}.ngen.forcing.org="${NGEN_FORCING_ORG}" \
    io.${IMAGE_NAMESPACE}.ngen.forcing.ref="${NGEN_FORCING_REF}" \
    io.${IMAGE_NAMESPACE}.ngen.forcing.revision="${NGEN_FORCING_REVISION}"

# Install build and runtime dependencies
RUN set -eux && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        file \
        findutils \
        gcc \
        g++ \
        git \
        jq \
        libpq5 \
        libpq-dev \
        make \
        openssl \
        pkg-config \
        xz-utils \
        # GDAL/Fiona requirements
        gdal-bin \
        libgdal-dev \
        libproj-dev \
        proj-data && \
    rm -rf /var/lib/apt/lists/*

# Install Python virtual environment
ENV VIRTUAL_ENV=/ngencerf/ngencerf-python
ENV PATH=${VIRTUAL_ENV}/bin:${PATH}

RUN --mount=type=cache,target=/root/.cache/pip,id=pip-cache-bookworm \
    set -eux && \
    python -m venv ${VIRTUAL_ENV}

WORKDIR /ngencerf/ngencerf-server/

# Pre-copy requirements for better caching
COPY requirements.txt .

RUN --mount=type=cache,target=/root/.cache/pip,id=pip-cache-bookworm \
    set -eux && \
    pip install --upgrade pip && \
    pip install -r requirements.txt && \
    rm -f requirements.txt

# ── EWTS (Error and Warning Trapping System)
#
# The dev image only needs the Python EWTS runtime. The native EWTS
# libraries and ngen integration are built only in the production image.
ARG EWTS_CACHE_BUST=1
RUN --mount=type=cache,target=/root/.cache/pip,id=pip-cache-bookworm \
    set -eux && \
    echo "EWTS cache bust: ${EWTS_CACHE_BUST}" && \
    ewts_dir="$(mktemp -d)" && \
    git clone --depth 1 -b "${EWTS_REF}" \
        "https://github.com/${EWTS_ORG}/nwm-ewts.git" "${ewts_dir}" \
     || (git clone "https://github.com/${EWTS_ORG}/nwm-ewts.git" "${ewts_dir}" && \
         cd "${ewts_dir}" && git checkout "${EWTS_REF}") && \
    pip install "${ewts_dir}/runtime/python/ewts" && \
    rm -rf "${ewts_dir}"

# Per-dependency cache-bust: CI passes the resolved commit SHA, so a stale
# Docker layer can't reinstall an older revision (see .github/workflows/cicd.yml)
ARG MSW_MGR_CACHE_BUST=1
ARG DATA_ASSIMILATION_CACHE_BUST=1
RUN --mount=type=cache,target=/root/.cache/pip,id=pip-cache-bookworm \
    set -eux && \
    echo "nwm-msw-mgr cache bust: ${MSW_MGR_CACHE_BUST}" && \
    python -m pip install \
        "git+https://github.com/${MSW_MGR_ORG}/nwm-msw-mgr.git@${MSW_MGR_REF}" && \
    echo "nwm-data-assimilation cache bust: ${DATA_ASSIMILATION_CACHE_BUST}" && \
    python -m pip install \
        "git+https://github.com/${DATA_ASSIMILATION_ORG}/nwm-data-assimilation.git@${DATA_ASSIMILATION_REF}"

# Should parallel similar functionality in the run_cerf.sh
COPY .git .git

# Create git_info files for server, nwm-msw-mgr, and nwm-data-assimilation
RUN set -eux && \
    # ----- Server git_info -----
    # Ensure local tag metadata includes all remote tags before creating git_info.
    git fetch --force --tags origin '+refs/tags/*:refs/tags/*' && \
    # Get the remote URL from Git configuration
    repo_url=$(git config --get remote.origin.url) && \
    # Extract the repo name (everything after the last slash) and remove any trailing .git
    key=${repo_url##*/} && \
    key=${key%.git} && \
    # Construct the file path using the derived key
    GIT_INFO_PATH="${key}_git_info.json" && \
    jq -n \
      --arg commit_hash "$(git rev-parse HEAD)" \
      --arg branch "$(git rev-parse --abbrev-ref HEAD)" \
      --arg tags "$(git tag --points-at HEAD | tr '\n' ' ')" \
      --arg author "$(git log -1 --pretty=format:'%an')" \
      --arg commit_date "$(date -u -d @$(git log -1 --pretty=format:'%ct') +'%Y-%m-%d %H:%M:%S UTC')" \
      --arg message "$(git log -1 --pretty=format:'%s' | tr '\n' ';')" \
      --arg build_date "$(date -u +'%Y-%m-%d %H:%M:%S UTC')" \
      "{\"$key\": {commit_hash: \$commit_hash, branch: \$branch, tags: \$tags, author: \$author, commit_date: \$commit_date, message: \$message, build_date: \$build_date}}" \
      > "$GIT_INFO_PATH" && \
    \
    # ----- nwm-msw-mgr git_info -----
    GIT_INFO_PATH="/ngencerf/ngencerf-server/nwm-msw-mgr_git_info.json" && \
    tmpdir=$(mktemp -d) && \
    git init "$tmpdir" && \
    cd "$tmpdir" && \
    git remote add origin "https://github.com/${MSW_MGR_ORG}/nwm-msw-mgr.git" && \
    git fetch --force --tags origin '+refs/tags/*:refs/tags/*' && \
    (git fetch --depth 1 origin "${MSW_MGR_REF}" \
     || git fetch --depth 1 origin "refs/tags/${MSW_MGR_REF}:refs/tags/${MSW_MGR_REF}" \
     || git fetch origin "${MSW_MGR_REF}" \
     || git fetch origin "refs/tags/${MSW_MGR_REF}:refs/tags/${MSW_MGR_REF}") && \
    git checkout FETCH_HEAD && \
    # Detect branch vs tag vs bare SHA for git_info metadata
    branch=$(git branch -r --contains HEAD 2>/dev/null \
             | grep -v '\->' | sed 's|origin/||' | head -n1 | xargs) && \
    branch=${branch:-""} && \
    tags=$(git tag --points-at HEAD 2>/dev/null | tr '\n' ' ') && \
    jq -n \
      --arg commit_hash "$(git rev-parse HEAD)" \
      --arg branch "$branch" \
      --arg tags "$tags" \
      --arg author "$(git log -1 --pretty=format:'%an')" \
      --arg commit_date "$(date -u -d @$(git log -1 --pretty=format:'%ct') +'%Y-%m-%d %H:%M:%S UTC')" \
      --arg message "$(git log -1 --pretty=format:'%s' | tr '\n' ';')" \
      --arg build_date "$(date -u +'%Y-%m-%d %H:%M:%S UTC')" \
      '{"nwm-msw-mgr": {commit_hash: $commit_hash, branch: $branch, tags: $tags, author: $author, commit_date: $commit_date, message: $message, build_date: $build_date}}' \
      > "$GIT_INFO_PATH" && \
    cd / && \
    rm -rf "$tmpdir" && \
    \
    # ----- nwm-data-assimilation git_info -----
    GIT_INFO_PATH="/ngencerf/ngencerf-server/nwm-data-assimilation_git_info.json" && \
    tmpdir=$(mktemp -d) && \
    git init "$tmpdir" && \
    cd "$tmpdir" && \
    git remote add origin "https://github.com/${DATA_ASSIMILATION_ORG}/nwm-data-assimilation.git" && \
    git fetch --force --tags origin '+refs/tags/*:refs/tags/*' && \
    (git fetch --depth 1 origin "${DATA_ASSIMILATION_REF}" \
     || git fetch --depth 1 origin "refs/tags/${DATA_ASSIMILATION_REF}:refs/tags/${DATA_ASSIMILATION_REF}" \
     || git fetch origin "${DATA_ASSIMILATION_REF}" \
     || git fetch origin "refs/tags/${DATA_ASSIMILATION_REF}:refs/tags/${DATA_ASSIMILATION_REF}") && \
    git checkout FETCH_HEAD && \
    # Detect branch vs tag vs bare SHA for git_info metadata
    branch=$(git branch -r --contains HEAD 2>/dev/null \
             | grep -v '\->' | sed 's|origin/||' | head -n1 | xargs) && \
    branch=${branch:-""} && \
    tags=$(git tag --points-at HEAD 2>/dev/null | tr '\n' ' ') && \
    jq -n \
      --arg commit_hash "$(git rev-parse HEAD)" \
      --arg branch "$branch" \
      --arg tags "$tags" \
      --arg author "$(git log -1 --pretty=format:'%an')" \
      --arg commit_date "$(date -u -d @$(git log -1 --pretty=format:'%ct') +'%Y-%m-%d %H:%M:%S UTC')" \
      --arg message "$(git log -1 --pretty=format:'%s' | tr '\n' ';')" \
      --arg build_date "$(date -u +'%Y-%m-%d %H:%M:%S UTC')" \
      '{"nwm-data-assimilation": {commit_hash: $commit_hash, branch: $branch, tags: $tags, author: $author, commit_date: $commit_date, message: $message, build_date: $build_date}}' \
      > "$GIT_INFO_PATH" && \
    cd / && \
    rm -rf "$tmpdir"

# Remove .git directory
RUN rm -rf .git

# Copy application code
COPY . /ngencerf/ngencerf-server/

# Fetch bmi_forcing_templates into an internal, non-mounted path to be copied at runtime by runCerf.sh
# Cache-bust on the resolved ngen-forcing commit so a ref update isn't masked by a cached layer
ARG NGEN_FORCING_CACHE_BUST=1
RUN set -eux && \
    echo "ngen-forcing cache bust: ${NGEN_FORCING_CACHE_BUST}" && \
    PREBUILT_DIR="/ngencerf/prebuilt/bmi_forcing_templates" && \
    NGEN_FORCING_URL="https://github.com/${NGEN_FORCING_ORG}/ngen-forcing.git" && \
    \
    echo "Preparing bmi_forcing_templates from ${NGEN_FORCING_URL}, ref (branch/tag/commit): ${NGEN_FORCING_REF}" && \
    \
    # Ensure prebuilt directory exists and is empty
    rm -rf "$PREBUILT_DIR" && \
    mkdir -p "$PREBUILT_DIR" && \
    \
    # Clone sparse repo; allow branch, tag, or commit refs
    git clone --filter=blob:none --no-checkout --sparse \
        "$NGEN_FORCING_URL" tmp-ngen-forcing && \
    cd tmp-ngen-forcing && \
    (git fetch --depth 1 origin "${NGEN_FORCING_REF}" \
     || git fetch --depth 1 origin "refs/tags/${NGEN_FORCING_REF}:refs/tags/${NGEN_FORCING_REF}" \
     || git fetch origin "${NGEN_FORCING_REF}" \
     || git fetch origin "refs/tags/${NGEN_FORCING_REF}:refs/tags/${NGEN_FORCING_REF}") && \
    git checkout FETCH_HEAD && \
    \
    git sparse-checkout set \
        NextGen_Forcings_Engine_BMI/BMI_NextGen_Configs/config_templates && \
    \
    # Copy *contents* of config_templates into PREBUILT_DIR
    cp -a NextGen_Forcings_Engine_BMI/BMI_NextGen_Configs/config_templates/. \
        "$PREBUILT_DIR"/ && \
    \
    cd /ngencerf/ngencerf-server && \
    rm -rf tmp-ngen-forcing || true

# Copy additional configuration files
COPY ./cerfserver-docker.env /ngencerf/ngencerf-server/cerfserver.env

# Set the entry point and expose the application port
ENTRYPOINT [ "/ngencerf/ngencerf-server/runCerf.sh" ]
EXPOSE 8000
