#!/usr/bin/env bash
# Replace checkout-local Git credentials/headers with the canonical updater
# remote while retaining a normal repository config for in-appliance updates.
set -euo pipefail

if [ "$#" -ne 1 ]; then
    echo "usage: sanitize_git_config.sh PACKAGED_REPOSITORY" >&2
    exit 2
fi

repository="$1"
git_dir="${repository}/.git"
config="${git_dir}/config"
if [ ! -d "${git_dir}" ] || [ ! -f "${config}" ]; then
    echo "refusing to package a non-standalone Git checkout: ${repository}" >&2
    exit 1
fi
if [ "$(git -C "${repository}" rev-parse --show-object-format)" != "sha1" ]; then
    echo "unsupported Git object format in packaged source repository" >&2
    exit 1
fi

branch="$(git -C "${repository}" symbolic-ref --short -q HEAD || true)"
temporary="$(mktemp "${git_dir}/.config.safe.XXXXXX")"
trap 'rm -f -- "${temporary}"' EXIT
cat > "${temporary}" <<'EOF'
[core]
	repositoryformatversion = 0
	filemode = true
	bare = false
	logallrefupdates = true
[remote "origin"]
	url = https://github.com/felipeab10/vhackintosh.git
	fetch = +refs/heads/*:refs/remotes/origin/*
EOF
if [ -n "${branch}" ]; then
    if ! git check-ref-format --branch "${branch}" >/dev/null; then
        echo "invalid current branch name in packaged source repository" >&2
        exit 1
    fi
    git config --file "${temporary}" "branch.${branch}.remote" origin
    git config --file "${temporary}" "branch.${branch}.merge" "refs/heads/${branch}"
fi
chmod 0644 "${temporary}"
mv -f -- "${temporary}" "${config}"
trap - EXIT
