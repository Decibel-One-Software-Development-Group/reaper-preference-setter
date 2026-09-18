#!/usr/bin/env python3
"""Publish a release to the gh-pages branch, keeping only the newest releases.

    publish-gh-pages.py STAGED_DIR SITE_REPO --keep 5 --message "appcast + DMG: ..."

STAGED_DIR  what this release publishes (appcast.xml, the new DMG, the -latest
            alias, the landing page) — copied over the site as-is. May be empty,
            which prunes the site without publishing anything.
SITE_REPO   a checkout of the gh-pages branch (actions/checkout with ref: gh-pages).

This replaces peaceiris/actions-gh-pages with keep_files: true, which kept every
DMG ever shipped: by 0.6.35 SiMONITOR's gh-pages held 84 DMGs / 1,139 MB — past
GitHub's 1 GB limit for a published Pages site — and the publish step and the
Pages deploy both grew with it (0.5 -> 2.5 min, 1.3 -> 4 min).

After the staged files are copied in, appcast.xml is cut to the --keep newest
entries by sparkle:version, and each file that ONLY a dropped entry named is
removed. Sparkle is unaffected: it offers the newest entry, and every entry in
these feeds needs the same minimum macOS. Anything the appcast never named — the
landing page, images, the *-latest alias, the Connect downloads — is never
touched, and neither is anything this release just staged.

Prune trouble (an unreadable version, an appcast that will not parse after the
cut) is reported as a warning and the release still publishes: shipping the
update matters more than the housekeeping. Deleting is what needs the caution.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

ITEM_RE = re.compile(r"[ \t]*<item>.*?</item>[ \t]*\n?", re.S)
VERSION_ELEM_RE = re.compile(r"<sparkle:version>([^<]+)</sparkle:version>")
VERSION_ATTR_RE = re.compile(r"sparkle:version=\"([^\"]+)\"")
ENCLOSURE_URL_RE = re.compile(r"<enclosure[^>]*\surl=\"([^\"]+)\"")
SHORT_RE = re.compile(r"<sparkle:shortVersionString>([^<]+)</sparkle:shortVersionString>")
# Only ever delete a download. If an appcast entry somehow names a page, the
# guard below leaves it alone rather than taking the site apart.
DELETABLE = re.compile(r"\.(dmg|zip|pkg|tar\.gz)$", re.I)


def warn(msg):
    print(f"::warning::publish-gh-pages: {msg}")


def git(repo, *args, check=True):
    return subprocess.run(["git", "-C", repo, *args], check=check,
                          capture_output=True, text=True)


def version_key(text):
    """Sort key for a sparkle:version — '635' or '1.26.1', newest = largest."""
    m = VERSION_ELEM_RE.search(text) or VERSION_ATTR_RE.search(text)
    if not m:
        return None
    parts = re.findall(r"\d+", m.group(1))
    return tuple(int(p) for p in parts) if parts else None


def short_version(text):
    m = SHORT_RE.search(text)
    return m.group(1) if m else "?"


def site_paths(item_text, site_host_suffix=".github.io"):
    """Site-relative paths an entry's enclosures name (ignore off-site URLs)."""
    out = []
    for url in ENCLOSURE_URL_RE.findall(item_text):
        parsed = urlparse(url)
        if not parsed.netloc.lower().endswith(site_host_suffix):
            continue  # e.g. a GitHub Release asset: not a file on this branch
        parts = parsed.path.lstrip("/").split("/", 1)
        rel = parts[1] if len(parts) == 2 else parts[0]  # project site: /<repo>/<file>
        if rel and ".." not in rel.split("/"):
            out.append(rel)
    return out


def copy_staged(staged_dir, site_repo):
    """Copy the staged release over the site, as keep_files: true did."""
    copied = []
    for root, _dirs, files in os.walk(staged_dir):
        for name in files:
            src = os.path.join(root, name)
            rel = os.path.relpath(src, staged_dir)
            dst = os.path.join(site_repo, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            copied.append(rel)
    return copied


def trim_appcast(text, keep):
    """Return (new_text, kept_items, dropped_items) or None if it cannot be cut."""
    items = [(m.group(0), m.start(), m.end()) for m in ITEM_RE.finditer(text)]
    if not items:
        warn("appcast.xml has no <item> entries — nothing pruned")
        return None
    keyed = []
    for raw, start, end in items:
        key = version_key(raw)
        if key is None:
            warn(f"an appcast entry has no readable sparkle:version — nothing pruned")
            return None
        keyed.append((key, start, end, raw))
    width = max(len(k[0]) for k in keyed)
    ranked = sorted(keyed, key=lambda k: k[0] + (0,) * (width - len(k[0])), reverse=True)
    kept, dropped = ranked[:keep], ranked[keep:]
    if not dropped:
        return text, kept, dropped
    drop_spans = sorted((d[1], d[2]) for d in dropped)
    out, cursor = [], 0
    for start, end in drop_spans:
        out.append(text[cursor:start])
        cursor = end
    out.append(text[cursor:])
    new_text = "".join(out)
    # The cut must not have disturbed what stays: the feed still parses, holds
    # exactly the kept entries, and each one is byte-identical (EdDSA signatures).
    try:
        parsed = ET.fromstring(new_text)
    except ET.ParseError as exc:
        warn(f"appcast.xml would not parse after pruning ({exc}) — nothing pruned")
        return None
    n_items = len(parsed.findall(".//item"))
    if n_items != len(kept):
        warn(f"pruned appcast has {n_items} entries, expected {len(kept)} — nothing pruned")
        return None
    for _key, _s, _e, raw in kept:
        if raw not in new_text:
            warn("a kept appcast entry changed while pruning — nothing pruned")
            return None
    return new_text, kept, dropped


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("staged_dir")
    ap.add_argument("site_repo")
    ap.add_argument("--keep", type=int, required=True,
                    help="releases to keep in the appcast and on the branch")
    ap.add_argument("--message", required=True, help="commit message")
    ap.add_argument("--branch", default="gh-pages")
    ap.add_argument("--remote", default="origin")
    ap.add_argument("--no-push", action="store_true")
    args = ap.parse_args()

    if args.keep < 1:
        sys.exit("--keep must be at least 1")
    for path in (args.staged_dir, args.site_repo):
        if not os.path.isdir(path):
            sys.exit(f"not a directory: {path}")

    staged = copy_staged(args.staged_dir, args.site_repo)
    print(f"published: {', '.join(sorted(staged)) if staged else '(nothing staged)'}")

    appcast = os.path.join(args.site_repo, "appcast.xml")
    removed = []
    if os.path.exists(appcast):
        with open(appcast, encoding="utf-8") as fh:
            original = fh.read()
        result = trim_appcast(original, args.keep)
        if result:
            new_text, kept, dropped = result
            keep_paths = {p for k in kept for p in site_paths(k[3])}
            drop_paths = {p for d in dropped for p in site_paths(d[3])}
            for rel in sorted(drop_paths - keep_paths - set(staged)):
                if not DELETABLE.search(rel):
                    warn(f"appcast entry names {rel}, which is not a download — kept")
                    continue
                full = os.path.join(args.site_repo, rel)
                tracked = git(args.site_repo, "ls-files", "--error-unmatch", "--", rel,
                              check=False).returncode == 0
                if not tracked and not os.path.exists(full):
                    continue  # already gone
                # Size only when the file is actually checked out — a partial
                # clone has the index entry but not the bytes, and asking git
                # for the size there would download the very DMG being dropped.
                size = os.path.getsize(full) if os.path.exists(full) else None
                git(args.site_repo, "rm", "--cached", "-q", "--ignore-unmatch", "--", rel)
                if os.path.exists(full):
                    os.remove(full)
                removed.append((rel, size))
            if new_text != original:
                with open(appcast, "w", encoding="utf-8") as fh:
                    fh.write(new_text)
            print(f"appcast: {len(kept) + len(dropped)} entries -> {len(kept)} "
                  f"({', '.join(short_version(k[3]) for k in kept)})")
    else:
        warn("no appcast.xml on the branch — nothing pruned")

    if removed:
        sizes = [s for _r, s in removed]
        total = f" ({sum(sizes) / 1e6:.0f} MB)" if all(s is not None for s in sizes) else ""
        print(f"removed {len(removed)} superseded downloads{total}: "
              f"{', '.join(r for r, _s in removed[:5])}"
              f"{' …' if len(removed) > 5 else ''}")

    for rel in staged + (["appcast.xml"] if os.path.exists(appcast) else []):
        git(args.site_repo, "add", "--", rel)
    if not git(args.site_repo, "diff", "--cached", "--quiet", check=False).returncode:
        print("gh-pages already up to date — nothing to commit")
        return
    git(args.site_repo, "-c", "user.name=github-actions[bot]",
        "-c", "user.email=github-actions[bot]@users.noreply.github.com",
        "commit", "-q", "-m", args.message)
    if args.no_push:
        print("committed (push skipped)")
        return
    git(args.site_repo, "push", args.remote, f"HEAD:refs/heads/{args.branch}")
    print(f"pushed to {args.branch}: {git(args.site_repo, 'rev-parse', '--short', 'HEAD').stdout.strip()}")


if __name__ == "__main__":
    main()
