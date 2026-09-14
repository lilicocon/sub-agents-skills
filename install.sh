#!/bin/bash
set -euo pipefail

# Install skill files only; backend CLIs, login and personal roles are separate.
TARGET=""
SKILL_NAME=""
REPO_URL="https://github.com/shinpr/sub-agents-skills"
TEMP_DIR=""
STAGE_DIR=""
BACKUP=""
DESTINATION=""

usage() {
    cat <<USAGE
Usage: $0 --target <install-path> [--skill <skill-name>]

  --target <path>   Required skills directory, e.g. ~/.codex/skills or ~/.cursor/skills
  --skill <name>    Install one skill (default: all skills)
  -h, --help       Show this help

From a local checkout:
  bash install.sh --target ~/.codex/skills --skill sub-agents

Store personal roles in ~/.codex/worker-agents or project .agents, outside the installation.
USAGE
}

error() {
    echo "Error: $1" >&2
    exit 1
}

cleanup() {
    local status=$?
    # A failed publish must leave the previous installation available.
    if [[ -n "$BACKUP" && -e "$BACKUP" && ! -e "$DESTINATION" ]]; then
        if ! mv "$BACKUP" "$DESTINATION"; then
            echo "Error: restore failed; previous installation remains at $BACKUP" >&2
            STAGE_DIR=""
            status=1
        fi
    fi
    [[ -z "$STAGE_DIR" ]] || rm -rf "$STAGE_DIR"
    [[ -z "$TEMP_DIR" ]] || rm -rf "$TEMP_DIR"
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

while [[ $# -gt 0 ]]; do
    case $1 in
        --target|--skill)
            [[ $# -ge 2 && -n "$2" && "$2" != --* ]] || error "Missing value for $1"
            if [[ "$1" == --target ]]; then TARGET="$2"; else SKILL_NAME="$2"; fi
            shift 2
            ;;
        -h|--help) usage; exit 0 ;;
        *) error "Unknown option: $1" ;;
    esac
done

[[ -n "$TARGET" ]] || error "--target is required (see --help)"
if [[ -n "$SKILL_NAME" && ! "$SKILL_NAME" =~ ^[a-zA-Z0-9][a-zA-Z0-9_-]*$ ]]; then
    error "Invalid skill name: $SKILL_NAME"
fi
case "$TARGET" in
    '~') TARGET="$HOME" ;;
    '~/'*) TARGET="$HOME/${TARGET:2}" ;;
esac

# BASH_SOURCE can be unset when the installer is piped to bash.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-}")" && pwd -P)"
SKILLS_SOURCE="$SCRIPT_DIR/skills"
if [[ ! -d "$SKILLS_SOURCE" ]]; then
    echo "Downloading skills from repository..."
    TEMP_DIR=$(mktemp -d)
    git clone --depth 1 "$REPO_URL" "$TEMP_DIR" || error "Failed to clone $REPO_URL"
    SKILLS_SOURCE="$TEMP_DIR/skills"
fi
[[ -d "$SKILLS_SOURCE" ]] || error "Skills directory not found: $SKILLS_SOURCE"
SKILLS_SOURCE="$(cd "$SKILLS_SOURCE" && pwd -P)"
mkdir -p "$TARGET"
TARGET="$(cd "$TARGET" && pwd -P)"
# Do not replace the checkout itself, including through a symlink.
case "$TARGET/" in
    "$SKILLS_SOURCE/"*) error "Install target must be outside source skills: $TARGET" ;;
esac
case "$SKILLS_SOURCE/" in
    "$TARGET/"*) error "Install target must not contain source skills: $TARGET" ;;
esac

STAGE_DIR=$(mktemp -d "$TARGET/.sub-agents-install.XXXXXX")
mkdir "$STAGE_DIR/new" "$STAGE_DIR/old"
installed=0
for skill_dir in "$SKILLS_SOURCE"/*/; do
    [[ -d "$skill_dir" ]] || continue
    skill_name=$(basename "$skill_dir")
    [[ -z "$SKILL_NAME" || "$skill_name" == "$SKILL_NAME" ]] || continue
    [[ -f "$skill_dir/SKILL.md" ]] || continue
    [[ ! -L "$TARGET/$skill_name" ]] || error "Refusing symlink destination: $TARGET/$skill_name"
    # Stage every copy before changing any existing installation.
    cp -R "${skill_dir%/}" "$STAGE_DIR/new/$skill_name"
    installed=$((installed + 1))
done
[[ $installed -gt 0 ]] || error "No matching skills found: ${SKILL_NAME:-all}"

for staged in "$STAGE_DIR/new"/*/; do
    skill_name=$(basename "$staged")
    DESTINATION="$TARGET/$skill_name"
    echo "Installing skill: $skill_name -> $DESTINATION"
    BACKUP="$STAGE_DIR/old/$skill_name"
    if [[ -e "$DESTINATION" ]]; then
        mv "$DESTINATION" "$BACKUP"
    fi
    mv "${staged%/}" "$DESTINATION"
    BACKUP=""
done

echo "Done! Installed $installed skill(s) to $TARGET"
echo "Backend CLIs and their login are separate. Keep custom roles in project .agents or ~/.codex/worker-agents."
