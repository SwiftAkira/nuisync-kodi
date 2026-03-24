"""
build.py — Package NuiSync for Kodi repo distribution.

Run this script to generate:
    repo/plugin.video.nuisync/plugin.video.nuisync-1.0.0.zip
    repo/repository.nuisync/repository.nuisync-1.0.0.zip
    repo/addons.xml.md5

Then push the repo/ folder to your GitHub and users can install
via "Install from zip" or add the repo source URL.
"""

import hashlib
import os
import zipfile

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.join(BASE_DIR, "repo")

ADDONS = [
    {
        "id": "plugin.video.nuisync",
        "version": "1.0.0",
        "source": os.path.join(BASE_DIR, "plugin.video.nuisync"),
    },
    {
        "id": "repository.nuisync",
        "version": "1.0.0",
        "source": os.path.join(BASE_DIR, "repository.nuisync"),
    },
]


def build_zip(addon_id, version, source_dir):
    """Create a zip file for a Kodi addon."""
    out_dir = os.path.join(REPO_DIR, addon_id)
    os.makedirs(out_dir, exist_ok=True)

    zip_name = "%s-%s.zip" % (addon_id, version)
    zip_path = os.path.join(out_dir, zip_name)

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(source_dir):
            # Skip __pycache__ and hidden dirs
            dirs[:] = [d for d in dirs
                       if not d.startswith(".") and d != "__pycache__"]
            for f in files:
                if f.startswith(".") or f.endswith(".pyc"):
                    continue
                full = os.path.join(root, f)
                arc = os.path.join(addon_id,
                                   os.path.relpath(full, source_dir))
                zf.write(full, arc)

    print("  -> %s" % zip_path)
    return zip_path


def build_md5():
    """Generate addons.xml.md5 checksum file."""
    xml_path = os.path.join(REPO_DIR, "addons.xml")
    with open(xml_path, "rb") as f:
        md5 = hashlib.md5(f.read()).hexdigest()
    md5_path = xml_path + ".md5"
    with open(md5_path, "w") as f:
        f.write(md5)
    print("  -> %s (%s)" % (md5_path, md5))


def main():
    print("Building NuiSync Kodi repo~\n")

    for addon in ADDONS:
        print("Packaging %s v%s..." % (addon["id"], addon["version"]))
        build_zip(addon["id"], addon["version"], addon["source"])

    print("\nGenerating addons.xml.md5...")
    build_md5()

    print("\nDone! Push the repo/ folder to GitHub.")
    print("Users install via: Settings > File Manager > Add source >")
    print("  https://swiftakira.github.io/nuisync-kodi/repo/")


if __name__ == "__main__":
    main()
