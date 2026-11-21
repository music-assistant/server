# Turns the repository references of the development image into owner/repo@ref, and finds the
# providers of a fetched provider repository.
# Sourced by entrypoint.sh; every function prints its result and aborts on a reference it refuses.

# Function to parse repository reference and return owner/repo@ref
parse_repo_ref() {
    input="$1"
    default_owner="$2"
    default_repo="$3"

    # If input starts with "pr-", convert to pull request reference
    if echo "$input" | grep -q "^pr-"; then
        pr_number=$(echo "$input" | sed 's/pr-//')
        echo "${default_owner}/${default_repo}@refs/pull/${pr_number}/head"
        return
    fi

    # If input contains "/" (branch name or fork reference)
    if echo "$input" | grep -q "/"; then
        # Check if it already has @ for branch
        if echo "$input" | grep -q "@"; then
            echo "$input"
            return
        fi
        # A bare slash is ambiguous: "owner/repo" names a fork, but "user/my-feature"
        # is just as likely a branch name. Ask the default repo which one it is.
        # Patterns are fully qualified because ls-remote matches loosely otherwise.
        probe_rc=0
        git ls-remote --exit-code \
            "https://github.com/${default_owner}/${default_repo}" \
            "refs/heads/${input}" "refs/tags/${input}" >/dev/null 2>&1 || probe_rc=$?
        case "$probe_rc" in
            0)
                echo "${default_owner}/${default_repo}@${input}"
                ;;
            2)
                # It's just owner/repo, use default branch
                echo "${input}@$(default_branch "$input")"
                ;;
            *)
                # Anything else means the lookup itself failed. Guessing here would
                # silently install something other than what was asked for.
                echo "" >&2
                echo "ERROR: could not reach GitHub to look up '${input}'" >&2
                echo "       Check the network connection and restart." >&2
                exit 1
                ;;
        esac
        return
    fi

    # Otherwise, it's just a branch/commit reference
    echo "${default_owner}/${default_repo}@${input}"
}

# Function to parse a provider repository reference, which has no default repository:
# owner/repo, owner/repo@ref or owner/repo@pr-<number>
parse_provider_ref() {
    input="$1"
    repo="${input%%@*}"
    owner="${repo%%/*}"
    name="${repo#*/}"
    ref="${input#"$repo"}"

    case "$owner" in
        "" | *[!A-Za-z0-9-]*) provider_ref_error "$input" ;;
    esac
    # a name equal to the whole repo part means it has no slash
    case "$name" in
        "" | "$repo" | *[!A-Za-z0-9._-]*) provider_ref_error "$input" ;;
    esac
    case "$ref" in
        "")
            echo "${repo}@$(default_branch "$repo")"
            ;;
        # a reference that starts with a dash would reach git as an option
        "@" | @-*)
            provider_ref_error "$input"
            ;;
        @pr-[0-9]*)
            case "${ref#@pr-}" in
                *[!0-9]*) echo "${repo}${ref}" ;;
                *) echo "${repo}@refs/pull/${ref#@pr-}/head" ;;
            esac
            ;;
        *)
            echo "${repo}${ref}"
            ;;
    esac
}

# Function to print the provider folders (a folder with a manifest.json) of a repository
# checkout: the top-level ones and those in providers/, as the server loads them
provider_folders() {
    for manifest in "$1"/*/manifest.json "$1"/providers/*/manifest.json; do
        if [ -f "$manifest" ]; then
            dirname "$manifest"
        fi
    done
}

# Function to report the branch a repository points HEAD at
default_branch() {
    head=$(git ls-remote --symref "https://github.com/$1" HEAD 2>/dev/null \
        | awk '/^ref:/ { sub("refs/heads/", "", $2); print $2; exit }')
    # An unreachable repository is reported later, by whoever tries to fetch it.
    echo "${head:-main}"
}

# Function to abort with the forms a provider repository reference can take
provider_ref_error() {
    echo "" >&2
    echo "ERROR: '$1' does not name a provider repository. Use one of:" >&2
    echo "       owner/repo          the default branch of the repository" >&2
    echo "       owner/repo@ref      a branch, a tag or a commit" >&2
    echo "       owner/repo@pr-12    pull request 12 of the repository" >&2
    exit 1
}
