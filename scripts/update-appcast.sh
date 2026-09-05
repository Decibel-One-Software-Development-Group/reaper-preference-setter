#!/bin/bash
# Generate / update the Sparkle appcast.xml for a freshly-built DMG.
# Run from the release workflow after the DMG is signed + notarized + stapled.
#
# Inputs (env):
#   DMG                      — path to the signed+notarized+stapled DMG
#   VERSION                  — version, no "v" prefix (e.g. "1.0.0")
#   RELEASE_URL              — public download URL Sparkle fetches (gh-pages)
#   SIGN_UPDATE              — path to Sparkle's sign_update tool
#   SPARKLE_PRIVATE_KEY_FILE — EdDSA private key on disk (CI materialises it)
#   MIN_OS                   — minimum macOS version (default 11.0)
#
# Output: build/appcast.xml. The workflow publishes it to gh-pages. The new item
# is appended to any existing items so every prior version keeps an upgrade path
# (Sparkle picks the newest by version, not document order). Mirrors SiCam Control,
# SiVLAN and SiChronize Server.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd -P)"
APPCAST="$REPO_ROOT/build/appcast.xml"
EXISTING_APPCAST="$REPO_ROOT/build/existing-appcast.xml"
MIN_OS="${MIN_OS:-11.0}"
REPO_URL="https://github.com/Decibel-One-Software-Development-Group/reaper-preference-setter"

[ -n "${DMG:-}" ] && [ -f "$DMG" ] || { echo "ERROR: \$DMG must point at a built DMG." >&2; exit 1; }
[ -n "${VERSION:-}" ] || { echo "ERROR: set \$VERSION (no v prefix)." >&2; exit 1; }
[ -n "${RELEASE_URL:-}" ] || { echo "ERROR: set \$RELEASE_URL." >&2; exit 1; }
[ -n "${SIGN_UPDATE:-}" ] && [ -x "$SIGN_UPDATE" ] || { echo "ERROR: \$SIGN_UPDATE must point at sign_update." >&2; exit 1; }

KEY_ARG=()
if [ -n "${SPARKLE_PRIVATE_KEY_FILE:-}" ] && [ -f "$SPARKLE_PRIVATE_KEY_FILE" ]; then
  KEY_ARG=(--ed-key-file "$SPARKLE_PRIVATE_KEY_FILE")
fi

# ${KEY_ARG[@]+...} guard: expand to nothing when the array is empty, which
# otherwise trips "unbound variable" under `set -u` on macOS's bash 3.2.
RAW=$("$SIGN_UPDATE" ${KEY_ARG[@]+"${KEY_ARG[@]}"} "$DMG")
echo ">>> sign_update: $RAW"
SIGNATURE=$(echo "$RAW" | sed -n 's/.*sparkle:edSignature="\([^"]*\)".*/\1/p')
LENGTH=$(echo "$RAW" | sed -n 's/.*length="\([^"]*\)".*/\1/p')
[ -n "$SIGNATURE" ] && [ -n "$LENGTH" ] || { echo "ERROR: couldn't parse sign_update output." >&2; exit 1; }

PUB_DATE=$(LC_ALL=C date "+%a, %d %b %Y %H:%M:%S %z")

# --- Release notes → HTML for the Sparkle <description> -----------------------
# Render release-notes/<version>.md (a small Markdown subset: ## / ### headings,
# - / * bullet lists, paragraphs; inline **bold**, `code`, [text](url)) to HTML
# so the Sparkle update window shows the real notes. Falls back to a "See release
# notes" stub when the file is absent. The result lands inside a CDATA section, so
# no XML escaping is needed. bash 3.2-compatible (the CI runner's /bin/bash).
render_notes_html() {
  local file="$1" line text kind buf="" bufkind="" in_list=0

  # Emit whatever block has been accumulating.
  flush() {
    [ -z "$buf" ] && return
    text="$(printf '%s' "$buf" | sed -E \
      -e 's/\*\*([^*]+)\*\*/<strong>\1<\/strong>/g' \
      -e 's/`([^`]+)`/<code>\1<\/code>/g' \
      -e 's/\[([^]]+)\]\(([^)]+)\)/<a href="\2">\1<\/a>/g')"
    if [ "$bufkind" = "li" ]; then
      if [ "$in_list" -eq 0 ]; then printf '<ul>\n'; in_list=1; fi
      printf '<li>%s</li>\n' "$text"
    else
      if [ "$in_list" -eq 1 ]; then printf '</ul>\n'; in_list=0; fi
      printf '<%s>%s</%s>\n' "$bufkind" "$text" "$bufkind"
    fi
    buf=""; bufkind=""
  }

  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      '' ) flush; if [ "$in_list" -eq 1 ]; then printf '</ul>\n'; in_list=0; fi; continue ;;
      '### '* ) flush; bufkind="h4"; buf="${line#'### '}"; continue ;;
      '## '*  ) flush; bufkind="h3"; buf="${line#'## '}";  continue ;;
      '# '*   ) flush; bufkind="h3"; buf="${line#'# '}";   continue ;;
      '- '*   ) flush; bufkind="li"; buf="${line#'- '}";   continue ;;
      '* '*   ) flush; bufkind="li"; buf="${line#'* '}";   continue ;;
    esac
    # A continuation of the block above: markdown hard-wraps a paragraph or a
    # bullet across lines, and treating each line as its own block turns one
    # sentence into several and strands bullet text outside the list.
    if [ -n "$buf" ]; then
      buf="$buf $(printf '%s' "$line" | sed -E 's/^[[:space:]]+//')"
    else
      bufkind="p"; buf="$line"
    fi
  done < "$file"
  flush
  if [ "$in_list" -eq 1 ]; then printf '</ul>\n'; fi
}

NOTES_MD="$REPO_ROOT/release-notes/$VERSION.md"
if [ -f "$NOTES_MD" ]; then
  DESC_HTML="$(render_notes_html "$NOTES_MD")"
  echo ">>> release notes: injected $NOTES_MD"
else
  DESC_HTML="<p>See <a href=\"$REPO_URL/releases/tag/v$VERSION\">release notes</a>.</p>"
  echo ">>> release notes: none found at $NOTES_MD — using stub"
fi

# Append a cumulative version history (release-notes/HISTORY.md, newest-first) so the
# Sparkle update window shows both what's new AND where the app has been. Convention:
# each release, write release-notes/<new-version>.md and prepend the just-shipped
# version's one-liner to HISTORY.md.
HISTORY_MD="$REPO_ROOT/release-notes/HISTORY.md"
if [ -f "$HISTORY_MD" ]; then
  DESC_HTML="$DESC_HTML
<hr/>
<h3>Version history</h3>
$(render_notes_html "$HISTORY_MD")"
  echo ">>> release notes: appended version history from $HISTORY_MD"
fi

NEW_ITEM_FILE=$(mktemp); trap 'rm -f "$NEW_ITEM_FILE"' EXIT
cat > "$NEW_ITEM_FILE" <<EOF
    <item>
      <title>SiRPS $VERSION</title>
      <link>$REPO_URL/releases/tag/v$VERSION</link>
      <sparkle:version>$VERSION</sparkle:version>
      <sparkle:shortVersionString>$VERSION</sparkle:shortVersionString>
      <description><![CDATA[
$DESC_HTML
      ]]></description>
      <pubDate>$PUB_DATE</pubDate>
      <enclosure
        url="$RELEASE_URL"
        sparkle:version="$VERSION"
        sparkle:shortVersionString="$VERSION"
        length="$LENGTH"
        type="application/octet-stream"
        sparkle:edSignature="$SIGNATURE"/>
      <sparkle:minimumSystemVersion>$MIN_OS</sparkle:minimumSystemVersion>
    </item>
EOF

if [ -f "$EXISTING_APPCAST" ]; then
  echo ">>> splicing into existing appcast"
  awk -v itemFile="$NEW_ITEM_FILE" '
    /<\/channel>/ { while ((getline line < itemFile) > 0) print line; close(itemFile); print; next }
    { print }
  ' "$EXISTING_APPCAST" > "$APPCAST"
else
  echo ">>> generating fresh appcast"
  {
    cat <<EOF
<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:sparkle="http://www.andymatuschak.org/xml-namespaces/sparkle" xmlns:dc="http://purl.org/dc/elements/1.1/">
  <channel>
    <title>SiRPS</title>
    <link>$REPO_URL</link>
    <description>SiRPS — REAPER preferences and DiGiCo session track lists (Decibel One)</description>
    <language>en</language>
EOF
    cat "$NEW_ITEM_FILE"
    cat <<EOF
  </channel>
</rss>
EOF
  } > "$APPCAST"
fi
echo "appcast.xml written ($(wc -l < "$APPCAST") lines)"
