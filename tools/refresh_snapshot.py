#!/usr/bin/env python3
"""Refresh the fragrance snapshot embedded in the page.

The page ships with a copy of the collection baked into EMBEDDED_DATA so the
first paint has something to show before Supabase answers. When that copy drifts
from the database, the app notices the difference on load and repaints. Run this
after adding or editing fragrances in Ben's Corner to bring the two back in line.

    python tools/refresh_snapshot.py                # every index*.html beside tools/
    python tools/refresh_snapshot.py path/to/index.html ...

Read-only against Supabase. Nothing is written unless the fetch succeeds and
returns rows.

Photos live in the database as base64 data URLs, several megabytes of them, so
they are never embedded. Each one is written to images/f/<id>.<ext> and the
snapshot points at the file instead. Existing files are only rewritten when the
bytes actually differ.
"""

import base64
import hashlib
import json
import os
import re
import sys
import urllib.request

MIME_EXT = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
}

DATA_URL_RE = re.compile(r"data:([^;]+);base64,(.*)$", re.S)
EMBEDDED_RE = re.compile(r"(const EMBEDDED_DATA = )(\[.*?\])(;\s*\n)", re.S)


def read_config(html):
    """Take the Supabase endpoint and public key from the page being updated,
    so this script can never disagree with the app about where data lives."""
    url = re.search(r"const SUPABASE_URL\s*=\s*'([^']+)'", html)
    key = re.search(r"const SUPABASE_ANON_KEY\s*=\s*'([^']+)'", html)
    if not url or not key:
        sys.exit("Could not find SUPABASE_URL / SUPABASE_ANON_KEY in the page.")
    return url.group(1), key.group(1)


def fetch_rows(base_url, key):
    req = urllib.request.Request(
        f"{base_url}/rest/v1/fragrances?select=data&order=id.asc",
        headers={"apikey": key, "Authorization": f"Bearer {key}"},
    )
    with urllib.request.urlopen(req, timeout=120) as res:
        if res.status != 200:
            sys.exit(f"Supabase returned HTTP {res.status}")
        rows = json.load(res)
    return [r["data"] for r in rows]


def externalise_images(rows, img_dir):
    """Move base64 photos out to files and rewrite each record to a path."""
    written, updated, reused, missing_ext = [], [], 0, []
    os.makedirs(img_dir, exist_ok=True)
    for rec in rows:
        img = rec.get("image") or ""
        if not img:
            rec["image"] = ""
            continue
        if not img.startswith("data:"):
            continue  # already a path; leave it alone
        m = DATA_URL_RE.match(img)
        if not m:
            missing_ext.append(rec["id"])
            rec["image"] = ""
            continue
        mime, b64 = m.group(1).strip().lower(), m.group(2)
        ext = MIME_EXT.get(mime)
        if not ext:
            missing_ext.append((rec["id"], mime))
            rec["image"] = ""
            continue
        raw = base64.b64decode(b64)
        name = f"{rec['id']}.{ext}"
        path = os.path.join(img_dir, name)
        if not os.path.exists(path):
            with open(path, "wb") as fh:
                fh.write(raw)
            written.append(name)
        elif hashlib.md5(open(path, "rb").read()).digest() != hashlib.md5(raw).digest():
            with open(path, "wb") as fh:
                fh.write(raw)
            updated.append(name)
        else:
            reused += 1
        rec["image"] = f"images/f/{name}"
    return written, updated, reused, missing_ext


def refresh(html_path):
    if not os.path.isfile(html_path):
        print(f"  skipped, no such file: {html_path}")
        return False

    raw_bytes = open(html_path, "rb").read()
    crlf = raw_bytes.count(b"\r\n") > 0
    html = raw_bytes.decode("utf-8")
    if crlf:
        html = html.replace("\r\n", "\n")

    match = EMBEDDED_RE.search(html)
    if not match:
        print("  EMBEDDED_DATA not found, nothing changed.")
        return False

    base_url, key = read_config(html)
    rows = fetch_rows(base_url, key)
    if not rows:
        print("  Supabase returned no rows, nothing changed.")
        return False

    img_dir = os.path.join(os.path.dirname(os.path.abspath(html_path)), "images", "f")
    written, updated, reused, bad = externalise_images(rows, img_dir)

    new_json = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
    if any(str(r.get("image", "")).startswith("data:") for r in rows):
        sys.exit("  Refusing to write: a base64 image survived into the snapshot.")

    old_json = match.group(2)
    if old_json == new_json and not written and not updated:
        print(f"  already current: {len(rows)} fragrances, nothing to do.")
        return False

    try:
        old_count = len(json.loads(old_json))
    except ValueError:
        old_count = "?"

    html = html[: match.start(2)] + new_json + html[match.end(2):]
    if crlf:
        html = html.replace("\n", "\r\n")
    with open(html_path, "wb") as fh:
        fh.write(html.encode("utf-8"))

    # Read the file back and confirm the snapshot survived the round trip byte
    # for byte. Every file handle above is binary and every decode is explicitly
    # utf-8, because a single text-mode open() here picks up the Windows locale
    # (cp1252), silently reads the page as the wrong charset and writes back
    # double-encoded mojibake: "Fougere" acquires a stray A-tilde and every
    # accent and dash in the collection quietly rots. That happened once; this
    # check is what makes it impossible to ship unnoticed.
    verify = open(html_path, "rb").read().decode("utf-8")
    written_back = EMBEDDED_RE.search(verify)
    if not written_back or json.loads(written_back.group(2)) != rows:
        sys.exit("  Wrote the file but it did not read back identically. "
                 "Restore it from git before doing anything else.")

    print(f"  fragrances: {old_count} -> {len(rows)}")
    print(f"  snapshot:   {len(old_json)} -> {len(new_json)} chars")
    if written:
        print(f"  new photos:     {len(written)} ({', '.join(written[:8])}"
              f"{'...' if len(written) > 8 else ''})")
    if updated:
        print(f"  changed photos: {len(updated)} ({', '.join(updated[:8])}"
              f"{'...' if len(updated) > 8 else ''})")
    print(f"  photos already on disk: {reused}")
    if bad:
        print(f"  WARNING: unusable image data on records: {bad}")

    referenced = {os.path.basename(r["image"]) for r in rows if r.get("image")}
    orphans = sorted(f for f in os.listdir(img_dir) if f not in referenced)
    if orphans:
        print(f"  note: {len(orphans)} file(s) in images/f no longer referenced "
              f"(left in place): {', '.join(orphans[:8])}"
              f"{'...' if len(orphans) > 8 else ''}")
    return True


def main():
    targets = sys.argv[1:]
    if not targets:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        targets = [os.path.join(root, n) for n in ("index.html", "index_2.html")]
        targets = [t for t in targets if os.path.isfile(t)]
        if not targets:
            sys.exit("No index.html or index_2.html found next to tools/. "
                     "Pass a path explicitly.")

    changed = False
    for path in targets:
        print(f"{path}")
        changed = refresh(path) or changed

    print()
    print("Snapshot updated. Commit the page and any new images."
          if changed else "Nothing needed updating.")


if __name__ == "__main__":
    main()
