#!/bin/sh
set -e

# Parse configuration: an empty or unset variable is not asked for
server_repo="${SERVER_REPO:-}"
frontend_repo="${FRONTEND_REPO:-}"
provider_repo="${PROVIDER_REPO:-}"

. "$(dirname "$0")/repo_ref.sh"

echo ""
echo "-----------------------------------------------------------"
echo "Music Assistant Development Server"
echo "For developers only: runs the code of the repositories you name"
echo "-----------------------------------------------------------"
echo ""

# Check if server_repo is empty
if [ -z "$server_repo" ]; then
    build_from_source=false
    echo "No SERVER_REPO specified - using the nightly build baked into this image"
else
    build_from_source=true
    echo "Building from source using SERVER_REPO: $server_repo"
fi
echo ""

# Function to build git URL from parsed reference
build_git_url() {
    parsed="$1"
    echo "git+https://github.com/${parsed}"
}

# Function to abort with a readable reason for an unreachable repository or reference
repo_ref_error() {
    parsed="$1"
    echo ""
    echo "ERROR: could not fetch '${parsed#*@}' from https://github.com/${parsed%@*}"
    echo "       Check that the repository exists and is public, and that the branch,"
    echo "       PR or commit is spelled correctly. A fork can be written owner/repo@ref."
    exit 1
}

# Function to fetch the latest state of one reference, without history, into an emptied folder
fetch_repo_ref() {
    parsed="$1"
    target_dir="$2"
    rm -rf "$target_dir"
    mkdir -p "$target_dir"
    # Fetching the reference is the only form that covers branches, tags,
    # refs/pull/<number>/head and full commit hashes alike, and it always lands on the
    # latest state of the remote.
    git -C "$target_dir" init -q .
    git -C "$target_dir" remote add origin "https://github.com/${parsed%%@*}.git"
    git -C "$target_dir" fetch --depth 1 origin -- "${parsed#*@}" || repo_ref_error "$parsed"
    git -C "$target_dir" reset --hard FETCH_HEAD
}

# Activate virtual environment
. "$VIRTUAL_ENV/bin/activate"

# Allow PyPI packages when the requirements file also uses the PyTorch extra index.
# Without this, uv's default first-index strategy lets the PyTorch index "own" any
# package it hosts (e.g. pillow), failing resolution when the pinned version only
# exists on PyPI. Matches the server repo's Dockerfile/CI/setup.sh behavior.
export UV_INDEX_STRATEGY=unsafe-best-match

# There is no terminal, so a missing or private repository makes git block on a
# credential prompt and report that instead of the actual problem.
export GIT_TERMINAL_PROMPT=0

# Parsed before the long install, so a reference it refuses stops the start right away
if [ -n "$provider_repo" ]; then
    provider_ref=$(parse_provider_ref "$provider_repo")
fi

echo "-----------------------------------------------------------"
echo "Step 1: Installing Music Assistant Server"
echo "-----------------------------------------------------------"
echo ""

if [ "$build_from_source" = true ]; then
    # Parse server repository reference
    server_ref=$(parse_repo_ref "$server_repo" "music-assistant" "server")
    server_url=$(build_git_url "$server_ref")

    echo "Server repository: $server_ref"
    echo "Server URL: $server_url"
    echo ""

    # Build requirements URL from the same reference
    # Convert git reference to raw GitHub URL for requirements file
    # Format: owner/repo@ref -> https://raw.githubusercontent.com/owner/repo/ref/requirements_all.txt
    req_owner=$(echo "$server_ref" | cut -d'/' -f1)
    req_repo=$(echo "$server_ref" | cut -d'/' -f2 | cut -d'@' -f1)
    req_ref=$(echo "$server_ref" | cut -d'@' -f2)
    requirements_url="https://raw.githubusercontent.com/${req_owner}/${req_repo}/${req_ref}/requirements_all.txt"

    echo "Installing dependencies from: $requirements_url"
    echo ""

    # This URL only resolves when the repository and the reference both exist, so a
    # 404 here names a wrong reference before uv reports it as a dependency problem.
    # Every other outcome is left to uv, which retries where a single request cannot.
    req_rc=0
    curl -fsI --connect-timeout 10 --max-time 30 -o /dev/null "$requirements_url" || req_rc=$?
    if [ "$req_rc" -eq 22 ]; then
        repo_ref_error "$server_ref"
    fi

    # the bundled app vars live inside the package, so a source install removes them
    site_packages=$(python -c 'import sysconfig; print(sysconfig.get_path("purelib"))')
    bundled_app_secrets="${site_packages}/music_assistant/helpers/app_secrets.json"
    if [ -f "$bundled_app_secrets" ]; then
        cp "$bundled_app_secrets" /tmp/app_secrets.json
    fi

    # uv builds in TMPDIR, and git dependencies alone outgrow the RAM-backed /tmp on small hosts
    build_tmp="/data/server-build-tmp"
    rm -rf "$build_tmp"
    mkdir -p "$build_tmp"

    # Install dependencies from the branch's requirements_all.txt
    TMPDIR="$build_tmp" uv pip install \
        --no-cache \
        --link-mode=copy \
        -r "$requirements_url"

    echo "✓ Dependencies installed"
    echo ""

    # Install server from specified repository
    TMPDIR="$build_tmp" uv pip install \
        --no-cache \
        --link-mode=copy \
        "$server_url"

    rm -rf "$build_tmp"

    if [ -f /tmp/app_secrets.json ]; then
        cp /tmp/app_secrets.json "$bundled_app_secrets"
        rm -f /tmp/app_secrets.json
        echo "✓ Bundled app variables restored"
        echo ""
    fi
else
    installed_version=$(python -c 'import importlib.metadata; print(importlib.metadata.version("music-assistant"))')
    echo "Using the nightly build from this image: $installed_version"
    echo ""
    echo "Set SERVER_REPO to install a branch, PR or fork instead."
    echo "A newer nightly comes with a newer version of this image."
fi

echo ""
echo "✓ Server installation complete"
echo ""

# Check if we should build frontend - this is independent of server source
build_frontend=false
if [ -n "$frontend_repo" ]; then
    build_frontend=true
    # Parse frontend repository reference
    frontend_ref=$(parse_repo_ref "$frontend_repo" "music-assistant" "frontend")

    echo "-----------------------------------------------------------"
    echo "Step 2: Building and Installing Music Assistant Frontend"
    echo "-----------------------------------------------------------"
    echo ""
    echo "Frontend repository: $frontend_ref"
    echo ""
else
    echo "-----------------------------------------------------------"
    echo "Step 2: Skipping Frontend Build"
    echo "-----------------------------------------------------------"
    echo ""
    echo "No FRONTEND_REPO specified - using frontend bundled with server"
    echo ""
fi

if [ "$build_frontend" = true ]; then

# Build on disk rather than the tmpfs /tmp, so node_modules and the build don't
# take up RAM, and pnpm can hardlink from its store on the same filesystem
frontend_dir="/data/frontend-build"

echo "Cloning frontend repository..."
fetch_repo_ref "$frontend_ref" "$frontend_dir"
cd "$frontend_dir"

echo "✓ Frontend cloned ($(git rev-parse --short HEAD))"
echo ""

# Check if package.json exists (verify it's a valid frontend repo)
if [ ! -f "package.json" ]; then
    echo "ERROR: package.json not found in frontend repository!"
    echo "This doesn't appear to be a valid Music Assistant frontend repository."
    exit 1
fi

echo "Installing frontend dependencies..."
# Enable Corepack so the package manager pinned in package.json's "packageManager"
# field (e.g. "pnpm@11.8.0") is used at the correct version. Persist Corepack's
# download cache under /data so it isn't re-fetched on every restart.
export COREPACK_HOME=/data/.corepack
export COREPACK_ENABLE_DOWNLOAD_PROMPT=0

# Detect the package manager from the lockfile present in the repo.
if [ -f "pnpm-lock.yaml" ]; then
    echo "Detected pnpm project"
    # Persistent store dir so packages aren't re-downloaded on every restart.
    corepack pnpm install --frozen-lockfile --store-dir /data/.pnpm-store
elif [ -f "yarn.lock" ]; then
    echo "Detected yarn project"
    # Persistent cache dir so yarn doesn't re-download packages on every restart.
    yarn config set cache-folder /data/.yarn-cache
    yarn install --frozen-lockfile --network-timeout 300000
else
    echo "Detected npm project"
    npm ci
fi

echo "✓ Dependencies installed"
echo ""

echo "Building frontend..."
# Only the bundle is needed here; skip the type-check (the frontend CI runs it),
# it needs several GB of RAM
./node_modules/.bin/vite build

echo "✓ Frontend build complete"
echo ""

# Check if the Python package structure exists
if [ ! -f "setup.py" ] && [ ! -f "pyproject.toml" ]; then
    echo "ERROR: No Python package configuration found!"
    echo "Frontend repository must be installable as a Python package."
    exit 1
fi

echo "Installing frontend as Python package..."
cd "$frontend_dir"
uv pip install --no-cache --link-mode=copy .

echo "✓ Frontend installation complete"
echo ""

# Cleanup
cd /
rm -rf "$frontend_dir"

fi  # End of build_frontend check

if [ -n "$provider_repo" ]; then
    echo "-----------------------------------------------------------"
    echo "Step 3: Fetching Provider Repository"
    echo "-----------------------------------------------------------"
    echo ""
    echo "Provider repository: $provider_ref"
    echo ""

    # kept while the server runs: the providers load from here
    provider_dir="/data/provider-repo"
    fetch_repo_ref "$provider_ref" "$provider_dir"

    if [ -z "$(provider_folders "$provider_dir")" ]; then
        echo ""
        echo "ERROR: ${provider_ref%%@*} holds no provider folder (a folder with a manifest.json,"
        echo "       at the top of the repository or in providers/)."
        exit 1
    fi

    echo "✓ Provider repository fetched ($(git -C "$provider_dir" rev-parse --short HEAD))"
    echo ""

    # the server only loads providers from a local folder in dev mode
    export PYTHONDEVMODE=1
    set -- "$@" --dev-providers-dir "$provider_dir"
fi

echo "-----------------------------------------------------------"
echo "Starting Music Assistant"
echo "-----------------------------------------------------------"
echo ""

# Start Music Assistant via the server image's entrypoint, which preloads jemalloc
# (that preload also pulls in libstdc++, which aiolibdatachannel's extension needs),
# with the arguments the container was started with
exec /usr/local/bin/entrypoint.sh --data-dir /data --cache-dir /data/.cache "$@"
