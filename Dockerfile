############################################################################
# Change/Verify these values when adopting this Dockerfile into another org:
#   GH_ORG, IMAGE_NAMESPACE,
#   DATA_ASSIMILATION_ORG, DATA_ASSIMILATION_REF,
#   MSW_MGR_ORG, MSW_MGR_REF,
#   NGEN_FORCING_ORG, NGEN_FORCING_REF
############################################################################

# Ownership / branding overrides
ARG GH_ORG=NGWPC
ARG IMAGE_NAMESPACE=ngwpc

# External repository sources (org and ref/branch overrides)
ARG DATA_ASSIMILATION_ORG=${GH_ORG}
ARG DATA_ASSIMILATION_REF=development
ARG EWTS_ORG=${GH_ORG}
ARG EWTS_REF=development
ARG MSW_MGR_ORG=${GH_ORG}
ARG MSW_MGR_REF=development
ARG NGEN_FORCING_ORG=${GH_ORG}
ARG NGEN_FORCING_REF=development
############################################################################

ARG BASE_REPO=rockylinux
ARG BASE_TAG=8

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
    io.${IMAGE_NAMESPACE}.ngen.forcing.org="${NGEN_FORCING_ORG}" \
    io.${IMAGE_NAMESPACE}.ngen.forcing.ref="${NGEN_FORCING_REF}" \
    io.${IMAGE_NAMESPACE}.ngen.forcing.revision="${NGEN_FORCING_REVISION}"

# Install build and runtime dependencies
RUN set -eux && \
    dnf install -y yum-utils epel-release && \
    dnf install -y \
        redis \
        findutils \
        file \
        jq \
        libpq \
        git \
        openssl openssl-devel \
        # Python 3.11 stack
        python3.11 python3.11-libs python3.11-devel \
        python3.11-pip \
        python3.11-setuptools && \
    dnf clean all

# Install Python virtual environment
ENV VIRTUAL_ENV=/ngencerf/ngencerf-python
ENV PATH=${VIRTUAL_ENV}/bin:${PATH}

RUN --mount=type=cache,target=/root/.cache/pip,id=pip-cache \
    set -eux && \
    python3.11 -m venv ${VIRTUAL_ENV}

WORKDIR /ngencerf/ngencerf-server/

# Pre-copy requirements for better caching
COPY requirements.txt .

RUN --mount=type=cache,target=/root/.cache/pip,id=pip-cache \
    set -eux && \
    pip3 install --upgrade pip && \
    pip3 install -r requirements.txt && \
    rm -f requirements.txt

# ── EWTS (Error and Warning Trapping System)
ARG EWTS_CACHE_BUST=1
RUN --mount=type=cache,target=/root/.cache/pip,id=pip-cache \
    echo "EWTS cache bust: ${EWTS_CACHE_BUST}" && \
    set -eux && \
    ewts_dir="$(mktemp -d)" && \
    git clone "https://github.com/${EWTS_ORG}/nwm-ewts.git" "${ewts_dir}" && \
    cd "${ewts_dir}" && \
    git checkout "${EWTS_REF}" && \
    pip install "${ewts_dir}/runtime/python/ewts" && \
    rm -rf "${ewts_dir}"

ARG CACHE_BUST=1
RUN set -eux && \
    echo $CACHE_BUST && pip3 install "git+https://github.com/${MSW_MGR_ORG}/nwm-msw-mgr.git@${MSW_MGR_REF}" && \
    echo $CACHE_BUST && pip3 install "git+https://github.com/${DATA_ASSIMILATION_ORG}/nwm-data-assimilation.git@${DATA_ASSIMILATION_REF}" && \
    pip3 cache purge

# Should parallel similar functionality in the run_cerf.sh
COPY .git .git

# Create git_info files for server and nwm-msw-mgr
RUN set -eux && \
    # ----- Server git_info -----
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
      > $GIT_INFO_PATH && \
    \
    # ----- nwm-msw-mgr git_info -----
    GIT_INFO_PATH="/ngencerf/ngencerf-server/nwm-msw-mgr_git_info.json" && \
    tmpdir=$(mktemp -d) && \
    git init "$tmpdir" && \
    cd "$tmpdir" && \
    git remote add origin "https://github.com/${MSW_MGR_ORG}/nwm-msw-mgr.git" && \
    (git fetch --depth 1 origin "${MSW_MGR_REF}" \
     || git fetch --depth 1 origin "refs/tags/${MSW_MGR_REF}:refs/tags/${MSW_MGR_REF}" \
     || git fetch origin "${MSW_MGR_REF}" \
     || git fetch origin "refs/tags/${MSW_MGR_REF}:refs/tags/${MSW_MGR_REF}") && \
    git checkout FETCH_HEAD && \
    # detect branch vs tag vs bare SHA for git_info metadata
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
      > $GIT_INFO_PATH && \
    cd / && rm -rf "$tmpdir"

# Remove .git directory
RUN rm -rf .git

# Copy application code
COPY . /ngencerf/ngencerf-server/

# Fetch bmi_forcing_templates into an internal, non-mounted path to be copied at runtime by runCerf.sh
RUN set -eux && \
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

# Build CLI executable in cli/dist
RUN cli/build_cli.sh

# Copy additional configuration files
COPY ./cerfserver-docker.env /ngencerf/ngencerf-server/cerfserver.env
COPY ./cerfServer/__local_settings.py /ngencerf/ngencerf-server/cerfServer/local_settings.py

# Set the entry point and expose the application port
ENTRYPOINT [ "/ngencerf/ngencerf-server/runCerf.sh" ]
EXPOSE 8000
