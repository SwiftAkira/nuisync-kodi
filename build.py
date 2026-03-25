"""
build.py — Package NuiSync for Kodi repo distribution.

Run this script to generate:
    repo/plugin.video.nuisync/plugin.video.nuisync-<ver>.zip  (for repo auto-updates)
    repo/repository.nuisync/repository.nuisync-<ver>.zip      (for repo auto-updates)
    repo/repository.nuisync-<ver>.zip                          (for Kodi "Install from zip")
    repo/addons.xml
    repo/addons.xml.md5
    repo/index.html

Then push to GitHub and users can install via:
    Settings > File Manager > Add source >
    https://swiftakira.github.io/nuisync-kodi/repo/
"""

import hashlib
import os
import re
import shutil
import zipfile
import xml.etree.ElementTree as ET

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.join(BASE_DIR, "repo")

ADDONS = [
    {
        "id": "plugin.video.nuisync",
        "source": os.path.join(BASE_DIR, "plugin.video.nuisync"),
    },
    {
        "id": "repository.nuisync",
        "source": os.path.join(BASE_DIR, "repository.nuisync"),
    },
]


def _read_version(source_dir):
    """Read version from addon.xml."""
    tree = ET.parse(os.path.join(source_dir, "addon.xml"))
    return tree.getroot().get("version")


def build_zip(addon_id, version, source_dir):
    """Create a zip file for a Kodi addon."""
    out_dir = os.path.join(REPO_DIR, addon_id)
    os.makedirs(out_dir, exist_ok=True)

    zip_name = "%s-%s.zip" % (addon_id, version)
    zip_path = os.path.join(out_dir, zip_name)

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        dirs_added = set()
        for root, dirs, files in os.walk(source_dir):
            dirs[:] = [d for d in dirs
                       if not d.startswith(".") and d != "__pycache__"]

            rel = os.path.relpath(root, source_dir)
            if rel == ".":
                dir_arc = addon_id + "/"
            else:
                dir_arc = addon_id + "/" + rel.replace("\\", "/") + "/"
            if dir_arc not in dirs_added:
                zf.mkdir(dir_arc)
                dirs_added.add(dir_arc)

            for f in files:
                if f.startswith(".") or f.endswith(".pyc"):
                    continue
                full = os.path.join(root, f)
                arc = os.path.join(addon_id,
                                   os.path.relpath(full, source_dir))
                arc = arc.replace("\\", "/")
                zf.write(full, arc)

    print("  -> %s" % zip_path)
    return zip_path


def build_addons_xml():
    """Generate addons.xml by concatenating raw addon.xml files.

    Uses raw text concatenation (not ElementTree serialization) to
    preserve the exact XML that Kodi expects. This is the standard
    approach used by Kodi repo generators.
    """
    xml_parts = ['<?xml version="1.0" encoding="UTF-8"?>\n<addons>']
    for addon in ADDONS:
        addon_xml = os.path.join(addon["source"], "addon.xml")
        with open(addon_xml, "r", encoding="utf-8") as f:
            raw = f.read().strip()
        # Strip the XML declaration if present
        raw = re.sub(r'<\?xml[^?]*\?>\s*', '', raw)
        xml_parts.append(raw)
    xml_parts.append('</addons>')
    content = "\n".join(xml_parts) + "\n"

    xml_path = os.path.join(REPO_DIR, "addons.xml")
    with open(xml_path, "w", encoding="utf-8") as f:
        f.write(content)
    print("  -> %s" % xml_path)


def build_md5():
    """Generate addons.xml.md5 checksum file."""
    xml_path = os.path.join(REPO_DIR, "addons.xml")
    with open(xml_path, "rb") as f:
        md5 = hashlib.md5(f.read()).hexdigest()
    md5_path = xml_path + ".md5"
    with open(md5_path, "w") as f:
        f.write(md5)
    print("  -> %s (%s)" % (md5_path, md5))


def build_index(zip_names):
    """Generate repo/index.html with direct links Kodi can browse."""
    lines = ["<html><body>"]
    for name in zip_names:
        lines.append('<a href="%s">%s</a><br/>' % (name, name))
    lines.append("</body></html>")

    path = os.path.join(REPO_DIR, "index.html")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    print("  -> %s" % path)


def main():
    print("Building NuiSync Kodi repo~\n")

    root_zips = []
    for addon in ADDONS:
        version = _read_version(addon["source"])
        addon_id = addon["id"]
        print("Packaging %s v%s..." % (addon_id, version))

        # Build zip in subdirectory (for repository auto-update downloads)
        zip_path = build_zip(addon_id, version, addon["source"])

        # Copy addon.xml alongside the zip (Kodi reads this for metadata)
        src_addon_xml = os.path.join(addon["source"], "addon.xml")
        dst_addon_xml = os.path.join(REPO_DIR, addon_id, "addon.xml")
        shutil.copy2(src_addon_xml, dst_addon_xml)
        print("  -> %s (metadata)" % dst_addon_xml)

        # Copy icon.png if it exists (Kodi shows this when browsing repo)
        src_icon = os.path.join(addon["source"], "icon.png")
        if os.path.exists(src_icon):
            dst_icon = os.path.join(REPO_DIR, addon_id, "icon.png")
            shutil.copy2(src_icon, dst_icon)
            print("  -> %s (icon)" % dst_icon)

        # Copy fanart.jpg if it exists
        src_fanart = os.path.join(addon["source"], "fanart.jpg")
        if os.path.exists(src_fanart):
            dst_fanart = os.path.join(REPO_DIR, addon_id, "fanart.jpg")
            shutil.copy2(src_fanart, dst_fanart)
            print("  -> %s (fanart)" % dst_fanart)

        # Copy zip to repo root (for Kodi "Install from zip" browsing)
        zip_name = os.path.basename(zip_path)
        root_copy = os.path.join(REPO_DIR, zip_name)
        shutil.copy2(zip_path, root_copy)
        print("  -> %s (browsable copy)" % root_copy)
        root_zips.append(zip_name)

    print("\nGenerating addons.xml...")
    build_addons_xml()

    print("\nGenerating addons.xml.md5...")
    build_md5()

    print("\nGenerating index.html...")
    build_index(root_zips)

    print("\nDone! Push to GitHub.")
    print("Users install via: Settings > File Manager > Add source >")
    print("  https://swiftakira.github.io/nuisync-kodi/repo/")


if __name__ == "__main__":
    main()
