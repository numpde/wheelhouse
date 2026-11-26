#!/usr/bin/env bash
# Publish the tested bundle from a frozen commit; failed drafts stay inspectable.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."

fail() { printf '%s\n' "$*" >&2; exit 1; }
docker_host=$1
[[ -z $(git status --porcelain) ]] || fail 'Commit pending changes before publishing.'
command -v gh >/dev/null || fail 'Install and authenticate the GitHub CLI before publishing.'
commit=$(git rev-parse HEAD)
repository=numpde/wheelhouse
origin=$(git remote get-url --push origin)
case "$origin" in
    git@github.com:numpde/wheelhouse.git|https://github.com/numpde/wheelhouse.git) ;;
    *) fail 'Origin must identify numpde/wheelhouse before publishing.' ;;
esac
shopt -s nullglob
notes=(notes/*_release_v*.md)
[[ ${#notes[@]} -gt 0 ]] || fail 'Write notes/NNN_release_vX.Y.Z.md before committing.'
declare -A release_notes=()
for note in "${notes[@]}"; do
    [[ $note =~ ^notes/[0-9]+_release_(v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*))\.md$ ]] \
        || fail "Release note must use notes/NNN_release_vX.Y.Z.md: $note"
    version=${BASH_REMATCH[1]}
    [[ ! -v release_notes[$version] ]] || fail "More than one release note declares $version."
    release_notes[$version]=$note
done
# Numeric version order, independent of note numbering or modification time.
tag=$(printf '%s\n' "${!release_notes[@]}" | LC_ALL=C sort -V | tail -n 1)
note=${release_notes[$tag]}
git show-ref --verify --quiet "refs/tags/$tag" && fail "Tag $tag already exists locally; write a note for the next version."
remote_tags=$(git ls-remote --tags origin "refs/tags/$tag")
[[ -z $remote_tags ]] || fail "Tag $tag already exists remotely; published versions must not be replaced."

scratch=$(mktemp -d -t wheelhouse-release.XXXXXXXX)
finish() {
    status=$?
    if [[ $status == 0 ]]; then
        rm -rf -- "$scratch"
    else
        printf 'Release failed; retained inputs and log: %s\n' "$scratch" >&2
        printf 'Inspect any remote release and tag before retrying.\n' >&2
    fi
}
trap finish EXIT
mkdir "$scratch/source"
git archive "$commit" | tar -x -C "$scratch/source"
printf 'Qualifying %s from %s (%s); log: %s/qualification.log\n' "$tag" "$note" "$commit" "$scratch"
if ! bash "$scratch/source/make/run.sh" test "$docker_host" \
    > "$scratch/qualification.log" 2>&1; then
    tail -n 40 "$scratch/qualification.log" >&2
    fail 'Release tests failed; nothing was published.'
fi
artifacts=$scratch/source/dist
(cd "$artifacts" && sha256sum --check SHA256SUMS)

# Push only the named tag. A pre-existing or conflicting tag fails without force.
git tag -a "$tag" "$commit" -m "Release $tag"
git push origin "refs/tags/$tag"
gh release create "$tag" --repo "$repository" --verify-tag --draft \
    --title "Wheelhouse $tag" --notes-file "$scratch/source/$note" \
    "$artifacts/wheelhouse.pyz" "$artifacts/SHA256SUMS"
gh release download "$tag" --repo "$repository" --dir "$scratch/downloaded"
for name in wheelhouse.pyz SHA256SUMS; do
    cmp -- "$artifacts/$name" "$scratch/downloaded/$name" \
        || fail "Uploaded $name differs from the tested file; draft remains unpublished."
done
gh release edit "$tag" --repo "$repository" --draft=false
printf 'Published https://github.com/%s/releases/tag/%s\n' "$repository" "$tag"
