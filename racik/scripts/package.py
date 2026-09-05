"""Build the shippable Racik packages, one per language.

There is one codebase. Two hand-maintained forks would drift within a week, so
the language builds differ only in which language they *default* to — both
carry the full ID/EN string catalogue and the in-app toggle. This script
rewrites exactly two marked lines per build:

    ui/index.html   const DEFAULT_LANG = "…";  /* BUILD:DEFAULT_LANG */
    racik/i18n.py   DEFAULT_LANG = "…"         # BUILD:DEFAULT_LANG

and swaps in the matching README. Everything else is byte-identical.

`data/racik.db` is deliberately excluded: it is a 66 MB derived artefact that
`scripts/build_db.py` regenerates in about 30 seconds from the raw sources that
*are* included. Shipping it would quadruple the archive for something the
recipient can rebuild — and a stale copy is worse than none.

Run:  python scripts/package.py            -> dist/racik-id.zip, dist/racik-en.zip
      python scripts/package.py --keep     -> also leaves the unzipped trees
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"

# Files and directories copied into every package, in the order listed.
INCLUDE = [
    "racik", "scripts", "tests", "ui", "docs",
    "data/prices_id.json", "data/raw",
    "requirements.txt", ".env.example",
]

# Never packaged: caches, the derived database, previous archives.
EXCLUDE_NAMES = {"__pycache__", ".pytest_cache", "dist", "racik.db",
                 ".DS_Store", "Thumbs.db"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".zip", ".log"}

README_FOR = {"id": "README.id.md", "en": "README.md"}
OTHER_README = {"id": "README.md", "en": "README.id.md"}

UI_MARKER = re.compile(r'(const DEFAULT_LANG = ")(id|en)(";\s*/\* BUILD:DEFAULT_LANG \*/)')
PY_MARKER = re.compile(r'(^DEFAULT_LANG = ")(id|en)(".*$)', re.M)


def _keep(path: Path) -> bool:
    if path.name in EXCLUDE_NAMES or path.suffix in EXCLUDE_SUFFIXES:
        return False
    return not any(part in EXCLUDE_NAMES for part in path.parts)


def _copy_into(staging: Path) -> None:
    for entry in INCLUDE:
        source = ROOT / entry
        if not source.exists():
            print(f"  ! skipping missing {entry}")
            continue
        target = staging / entry
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(
                source, target,
                ignore=lambda d, names: [n for n in names
                                         if not _keep(Path(d) / n)])
        else:
            shutil.copy2(source, target)


def _set_default_language(staging: Path, lang: str) -> None:
    """Rewrite the two build markers, failing loudly if either is missing."""
    ui = staging / "ui" / "index.html"
    text = ui.read_text(encoding="utf-8")
    text, count = UI_MARKER.subn(rf"\g<1>{lang}\g<3>", text)
    if count != 1:
        raise SystemExit(f"expected 1 UI DEFAULT_LANG marker, found {count}")
    ui.write_text(text, encoding="utf-8")

    i18n = staging / "racik" / "i18n.py"
    text = i18n.read_text(encoding="utf-8")
    text, count = PY_MARKER.subn(rf"\g<1>{lang}\g<3>", text)
    if count != 1:
        raise SystemExit(f"expected 1 i18n DEFAULT_LANG marker, found {count}")
    i18n.write_text(text, encoding="utf-8")


def _set_readme(staging: Path, lang: str) -> None:
    """Promote the language's README to README.md, keep the other alongside."""
    primary = staging / README_FOR[lang]
    other = staging / OTHER_README[lang]
    shutil.copy2(ROOT / README_FOR[lang], primary)
    shutil.copy2(ROOT / OTHER_README[lang], other)
    if lang == "id":                       # README.id.md becomes README.md
        readme = (staging / "README.id.md").read_text(encoding="utf-8")
        english = (staging / "README.md").read_text(encoding="utf-8")
        (staging / "README.md").write_text(readme, encoding="utf-8")
        (staging / "README.en.md").write_text(english, encoding="utf-8")
        (staging / "README.id.md").unlink()


def _verify(staging: Path, lang: str) -> None:
    """Cheap guards against shipping a broken archive."""
    checks = [
        staging / "racik" / "api.py",
        staging / "ui" / "index.html",
        staging / "README.md",
        staging / "requirements.txt",
        staging / "data" / "prices_id.json",
        staging / "data" / "raw" / "TKPI_2020_Table_4_All_Data.csv",
        staging / "data" / "raw" / "Indonesian_Recipes_HF_Regional.xlsx",
        staging / "scripts" / "build_db.py",
        staging / "docs" / "architecture.svg",
    ]
    missing = [str(p.relative_to(staging)) for p in checks if not p.exists()]
    if missing:
        raise SystemExit(f"package is incomplete, missing: {missing}")

    ui = (staging / "ui" / "index.html").read_text(encoding="utf-8")
    if f'const DEFAULT_LANG = "{lang}"' not in ui:
        raise SystemExit(f"UI default language was not set to '{lang}'")
    for key in ('"brand.sub"', '"page.title"'):
        if ui.count(key) < 2:
            raise SystemExit(f"UI is missing a language block for {key}")

    i18n = (staging / "racik" / "i18n.py").read_text(encoding="utf-8")
    if f'DEFAULT_LANG = "{lang}"' not in i18n:
        raise SystemExit(f"server default language was not set to '{lang}'")

    if (staging / "data" / "racik.db").exists():
        raise SystemExit("racik.db must not be packaged")


def _zip(staging: Path, archive: Path) -> int:
    archive.parent.mkdir(parents=True, exist_ok=True)
    if archive.exists():
        archive.unlink()
    count = 0
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in sorted(staging.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(staging.parent))
                count += 1
    return count


def build(lang: str, keep: bool = False) -> Path:
    name = f"racik-{lang}"
    staging = DIST / name
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    print(f"\n== {name} ==")
    _copy_into(staging)
    _set_default_language(staging, lang)
    _set_readme(staging, lang)
    (staging / ".env.example").write_text(
        "# Optional. Without it Racik answers with the rule-based planner.\n"
        "SEALION_API_KEY=\n", encoding="utf-8")
    _verify(staging, lang)

    archive = DIST / f"{name}.zip"
    files = _zip(staging, archive)
    size_mb = archive.stat().st_size / 1e6
    print(f"  {files} files -> {archive.relative_to(ROOT)} ({size_mb:.1f} MB)")

    if not keep:
        shutil.rmtree(staging)
    return archive


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build the shippable Racik packages, one per language.")
    parser.add_argument("--keep", action="store_true",
                        help="leave the unzipped staging trees in dist/")
    parser.add_argument("--lang", choices=["id", "en"], action="append",
                        help="build only this language (repeatable)")
    args = parser.parse_args()

    for lang in (args.lang or ["id", "en"]):
        build(lang, keep=args.keep)
    print(f"\nPackages written to {DIST}")
    print("Recipients run:  pip install -r requirements.txt "
          "&& python scripts/build_db.py")


if __name__ == "__main__":
    main()
