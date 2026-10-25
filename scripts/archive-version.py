#!/usr/bin/env python3
"""Keep every version of an app downloadable, and list them on its product page.

The app repos are private, so their GitHub Releases can't be downloaded by the
public, and each app's gh-pages keeps only the newest 5 installers (Pages has a
1 GB limit per site). So each release also copies its installer into the PUBLIC
repo Decibel-One-Software-Development-Group/downloads: one release per version,
tagged <slug>-v<version>, its body the release notes. The product page then lists
every archived version under "All versions".

Simon's rulings, 2026-10-24: the list sits on each app's own product page; it
starts with the version current that day and adds every later one; each row
shows the version, the date, the macOS it needs, its release notes and its size.

  archive-version.py upload --slug simonitor --app SiMONITOR --version 0.6.51 \\
      --notes release-notes/0.6.51.md --file app/build/Simonitor-v0.6.51.dmg
  archive-version.py render --slug simonitor build/gh-pages/index.html [more pages]

upload needs DOWNLOADS_TOKEN (contents: write on the downloads repo) and `gh`.
render reads the public repo, with GH_TOKEN when set. A page marks where the
list goes with <!-- versions:start --> and <!-- versions:end -->; render fills
the space between them, in the page's own language (<html lang>), and leaves it
empty when there is nothing to list, so the section simply isn't there.

Neither command ever fails a release: trouble is a ::warning:: and exit 0, and
a page is only rewritten once its list is complete. The same file sits in every
app repo beside publish-gh-pages.py; change one, change them all.
"""

import argparse
import datetime
import html
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request

REPO = "Decibel-One-Software-Development-Group/downloads"
START, END = "<!-- versions:start -->", "<!-- versions:end -->"
META = re.compile(r"<!--\s*archive\s+(\{.*?\})\s*-->", re.S)
DOT = " \u00b7 "

TEXT = {
    "en": {"h": "All versions",
           "note": "Every version stays here after a newer one comes out.",
           "notes": "What’s new", "dl": "Download",
           "macos": "macOS {} or later",
           "months": "January February March April May June July August September October November December",
           "date": "{d} {m} {y}"},
    "es": {"h": "Todas las versiones",
           "note": "Cada versión sigue aquí cuando sale una nueva.",
           "notes": "Novedades", "dl": "Descargar",
           "macos": "macOS {} o posterior",
           "months": "enero febrero marzo abril mayo junio julio agosto septiembre octubre noviembre diciembre",
           "date": "{d} de {m} de {y}"},
    "ja": {"h": "すべてのバージョン",
           "note": "新しいバージョンが出た後も、"
                   "各バージョンをここから"
                   "ダウンロードできます。",
           "notes": "新機能", "dl": "ダウンロード",
           "macos": "macOS {} 以降",
           "months": None, "date": "{y}年{n}月{d}日"},
    "ko": {"h": "모든 버전",
           "note": "새 버전이 나온 뒤에도 각 버전을 "
                   "여기에서 내려받을 수 있습니다.",
           "notes": "새로운 기능", "dl": "다운로드",
           "macos": "macOS {} 이상",
           "months": None, "date": "{y}년 {n}월 {d}일"},
}


def warn(msg):
    print(f"::warning::archive-version: {msg}")


def vkey(v):
    return tuple(int(p) if p.isdigit() else 0 for p in re.split(r"[.\-]", v))


def macos_label(v):
    return re.sub(r"(\.0)+$", "", v)


def date_text(iso, t):
    d = datetime.date.fromisoformat(iso)
    months = t["months"].split() if t["months"] else None
    return t["date"].format(d=d.day, n=d.month, y=d.year, m=months[d.month - 1] if months else "")


def size_text(n, lang):
    mb = n / 1e6
    s = f"{mb:.1f}" if mb < 10 else f"{mb:.0f}"
    return (s.replace(".", ",") if lang == "es" else s) + " MB"


def min_macos_from_dmg(dmg):
    """LSMinimumSystemVersion of the app inside a DMG (macOS only), or None."""
    if sys.platform != "darwin":
        return None
    mnt = tempfile.mkdtemp(prefix="archive-dmg-")
    try:
        subprocess.run(["hdiutil", "attach", "-nobrowse", "-readonly", "-noautoopen",
                        "-mountpoint", mnt, dmg], check=True, capture_output=True)
        try:
            for name in sorted(os.listdir(mnt)):
                plist = os.path.join(mnt, name, "Contents", "Info.plist")
                if name.endswith(".app") and os.path.isfile(plist):
                    with open(plist, "rb") as f:
                        return plistlib.load(f).get("LSMinimumSystemVersion")
        finally:
            subprocess.run(["hdiutil", "detach", mnt, "-quiet"], capture_output=True)
    except (subprocess.CalledProcessError, OSError) as e:
        warn(f"could not read the minimum macOS from {dmg}: {e}")
    finally:
        shutil.rmtree(mnt, ignore_errors=True)
    return None


def gh(args, token, check=True):
    env = dict(os.environ, GH_TOKEN=token)
    return subprocess.run(["gh", *args], env=env, check=check, capture_output=True, text=True)


def cmd_upload(a):
    token = os.environ.get("DOWNLOADS_TOKEN", "")
    files = []
    for spec in a.file:
        path, _, label = spec.partition("::")
        if not os.path.isfile(path):
            warn(f"{path} is missing; nothing archived")
            return 0
        files.append((path, label))
    macos = a.macos or next((min_macos_from_dmg(p) for p, _ in files if p.endswith(".dmg")), None)
    date = a.date or datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    notes = ""
    if a.notes:
        with open(a.notes, encoding="utf-8") as f:
            notes = f.read().strip()
    meta = {"app": a.app, "version": a.version, "date": date, "macos": macos, "notes": bool(notes),
            "files": [{"name": os.path.basename(p), "label": l} for p, l in files]}
    head = f"**{a.app} {a.version}**" + DOT + date_text(date, TEXT["en"])
    if macos:
        head += DOT + TEXT["en"]["macos"].format(macos_label(macos))
    body = f"<!-- archive {json.dumps(meta, ensure_ascii=False)} -->\n{head}\n" + (f"\n{notes}\n" if notes else "")
    tag = f"{a.slug}-v{a.version}"
    if a.dry_run:
        print(f"tag {tag} in {REPO}\nfiles {[p for p, _ in files]}\n---\n{body}")
        return 0
    if not token:
        warn(f"DOWNLOADS_TOKEN is not set, so {tag} was not archived. "
             f"Copy it later with: archive-version.py upload ... (see the script's header)")
        return 0
    if not shutil.which("gh"):
        warn("gh is not installed; nothing archived")
        return 0
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
        f.write(body)
        body_file = f.name
    paths = [p for p, _ in files]
    try:
        if gh(["release", "view", tag, "-R", REPO], token, check=False).returncode == 0:
            gh(["release", "edit", tag, "-R", REPO, "--notes-file", body_file], token)
            gh(["release", "upload", tag, *paths, "-R", REPO, "--clobber"], token)
        else:
            gh(["release", "create", tag, *paths, "-R", REPO, "--title", f"{a.app} {a.version}",
                "--notes-file", body_file, "--latest=false"], token)
        print(f"archived {tag}: https://github.com/{REPO}/releases/tag/{tag}")
    except subprocess.CalledProcessError as e:
        warn(f"archiving {tag} failed: {(e.stderr or '').strip()[:500]}")
    finally:
        os.unlink(body_file)
    return 0


def fetch_releases():
    out, page = [], 1
    while True:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{REPO}/releases?per_page=100&page={page}",
            headers={"Accept": "application/vnd.github+json", "User-Agent": "decibel-one-archive"})
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(req, timeout=30) as r:
            batch = json.load(r)
        out += batch
        if len(batch) < 100:
            return out
        page += 1


def rows_for(slug, releases):
    rows = []
    for rel in releases:
        if rel.get("draft") or not rel.get("tag_name", "").startswith(f"{slug}-v"):
            continue
        m = META.search(rel.get("body") or "")
        if not m:
            continue
        try:
            meta = json.loads(m.group(1))
        except ValueError:
            continue
        assets = {x["name"]: x for x in rel.get("assets", [])}
        files = [(f.get("label", ""), assets[f["name"]]) for f in meta.get("files", []) if f["name"] in assets]
        if files:
            rows.append({"version": meta["version"], "date": meta["date"], "macos": meta.get("macos"),
                         "notes": rel["html_url"] if meta.get("notes", True) else None, "files": files})
    rows.sort(key=lambda r: (vkey(r["version"]), r["date"]), reverse=True)
    return rows


def section(rows, lang):
    t = TEXT.get(lang, TEXT["en"])
    e = html.escape
    out = ['<section class="versions" aria-labelledby="versions-h">',
           f'  <h2 id="versions-h">{e(t["h"])}</h2>',
           f'  <p class="versions-note">{e(t["note"])}</p>',
           '  <ol class="versions-list">']
    for r in rows:
        meta = [e(date_text(r["date"], t))]
        if r["macos"]:
            meta.append(e(t["macos"].format(macos_label(r["macos"]))))
        if r["notes"]:
            meta.append(f'<a href="{e(r["notes"])}">{e(t["notes"])}</a>')
        links = "".join(
            f'<a class="v-dl" href="{e(x["browser_download_url"])}">{e(label or t["dl"])} '
            f'<span class="v-size">{e(size_text(x["size"], lang))}</span></a>'
            for label, x in r["files"])
        out += ['    <li>',
                f'      <div class="v-main"><span class="v-num">{e(r["version"])}</span>'
                f'<span class="v-meta">{DOT.join(meta)}</span></div>',
                f'      <div class="v-files">{links}</div>',
                '    </li>']
    out += ['  </ol>', '</section>']
    return "\n".join(out)


def cmd_render(a):
    try:
        if a.releases_json:
            with open(a.releases_json, encoding="utf-8") as f:
                releases = json.load(f)
        else:
            releases = fetch_releases()
    except Exception as e:  # any trouble: leave the pages as they are
        warn(f"could not read {REPO}: {e}; the version list is left out")
        return 0
    rows = rows_for(a.slug, releases)
    for page in a.pages:
        with open(page, encoding="utf-8") as f:
            text = f.read()
        i, j = text.find(START), text.find(END)
        if i < 0 or j < i:
            warn(f"{page} has no {START} ... {END} markers; left as it is")
            continue
        m = re.search(r'<html[^>]*\blang="([a-z]{2})', text)
        block = section(rows, m.group(1) if m else "en") if rows else ""
        indent = text[text.rfind("\n", 0, i) + 1:i]
        inner = "".join("\n" + indent + ln for ln in block.splitlines()) + "\n" + indent
        with open(page, "w", encoding="utf-8") as f:
            f.write(text[:i + len(START)] + inner + text[j:])
        print(f"{page}: {len(rows)} version(s) listed")
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("upload", help="copy this version into the public downloads repo")
    u.add_argument("--slug", required=True, help="tag prefix, e.g. simonitor")
    u.add_argument("--app", required=True, help="display name, e.g. SiMONITOR")
    u.add_argument("--version", required=True)
    u.add_argument("--notes", help="the release notes (Markdown); the row has no notes link without them")
    u.add_argument("--file", action="append", required=True,
                   help="installer to archive; PATH or PATH::LABEL (the button's text). Repeatable")
    u.add_argument("--macos", help="minimum macOS; read from the first DMG's app when left out")
    u.add_argument("--date", help="release date, YYYY-MM-DD; today (UTC) when left out")
    u.add_argument("--dry-run", action="store_true", help="print what would be archived")
    r = sub.add_parser("render", help="write the version list into product pages")
    r.add_argument("--slug", required=True)
    r.add_argument("--releases-json", help="read releases from this file instead of GitHub (previews, tests)")
    r.add_argument("pages", nargs="+")
    a = p.parse_args()
    return cmd_upload(a) if a.cmd == "upload" else cmd_render(a)


if __name__ == "__main__":
    sys.exit(main())
