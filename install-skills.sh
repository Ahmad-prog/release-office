#!/usr/bin/env bash
# Copies the role skills into ~/.claude/skills so Claude Code can load them.
# The source of truth is ./skills. Edit there and run this again.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
dest="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}"

for dir in "$here"/skills/office-*; do
  name="$(basename "$dir")"
  # The skills name this folder as /path/to/release-office. The installed copy gets the real path.
  rendered="$(sed "s|/path/to/release-office|$here|g" "$dir/SKILL.md")"
  if [ -f "$dest/$name/SKILL.md" ] && [ "$rendered" != "$(cat "$dest/$name/SKILL.md")" ]; then
    echo "$name: the installed copy differed from the source and is being replaced"
  fi
  mkdir -p "$dest/$name"
  printf '%s\n' "$rendered" > "$dest/$name/SKILL.md"
  echo "installed $name"
done

echo "Start a new Claude Code session to pick them up: /office-manager, /office-developer, /office-coauthor, /office-reviewer"
