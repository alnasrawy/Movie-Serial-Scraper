"""
sync_release.py — مزامنة الملفات المعدّلة من التطوير إلى release/.

الاستخدام:
    python sync_release.py              # فحص وتزامن الملفات المعدّلة فقط
    python sync_release.py --check      # عرض الملفات التي ستُزامن دون نسخ
    python sync_release.py --full       # إعادة بناء release/ بالكامل من dev
    python sync_release.py file1 file2  # مزامنة ملفات محدّدة فقط

المنطق:
    - ينسخ الملفات من dev إلى release/ فقط إذا تغيّرت (حسب mtime + الحجم).
    - لا يلمس الملفات غير المعدّلة — استبدال الملف المُعدّل فقط.
    - يتجاهل مجلدات الاختبارات والملفات الحسّاسة (.env, caches, logs).
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.abspath(__file__))
RELEASE = os.path.join(ROOT, "release")

# المجلدات المستبعدة من المزامنة (اختبارات، كاش، ...)
EXCLUDE_DIRS = {"__pycache__", ".pytest_cache", "tests", ".git"}
# الملفات المستبعدة من المزامنة (حسّاسة / غير مطلوبة في الإنتاج)
EXCLUDE_FILES = {".env", ".gitignore", "server_output.log", "server.log", "sync_release.py"}

# العناصر من الجذر المسموح بنقلها إلى release/
INCLUDE_TOPLEVEL = [
    "film_scraper",
    ".dockerignore",
    ".env.example",
    "DEPLOYMENT.md",
    "Dockerfile",
    "Dockerfile.lite",
    "README.md",
    "docker-compose.yml",
    "render.yaml",
    "requirements-dev.txt",
    "requirements-lite.txt",
    "requirements.txt",
]


def _is_excluded(rel_path: str) -> bool:
    parts = rel_path.replace("\\", "/").split("/")
    if any(p in EXCLUDE_DIRS for p in parts):
        return True
    return parts[-1] in EXCLUDE_FILES


def collect_dev_files() -> list[str]:
    """كل الملفات في dev المؤهلة للنقل إلى release/ (بمسار نسبي من ROOT)."""
    files: list[str] = []
    for entry in INCLUDE_TOPLEVEL:
        src = os.path.join(ROOT, entry)
        if not os.path.exists(src):
            continue
        if os.path.isdir(src):
            for cur, dirs, fnames in os.walk(src):
                dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
                for f in fnames:
                    rel = os.path.relpath(os.path.join(cur, f), ROOT)
                    if not _is_excluded(rel):
                        files.append(rel)
        else:
            rel = os.path.relpath(src, ROOT)
            if not _is_excluded(rel):
                files.append(rel)
    return sorted(files)


def changed_files() -> list[str]:
    """الملفات المعدّلة فقط: اختلفت mtime أو الحجم عن نسخة release/."""
    changed: list[str] = []
    for rel in collect_dev_files():
        src = os.path.join(ROOT, rel)
        dst = os.path.join(RELEASE, rel)
        if not os.path.exists(dst):
            changed.append(rel)
            continue
        s_stat = os.stat(src)
        d_stat = os.stat(dst)
        if s_stat.st_size != d_stat.st_size or s_stat.st_mtime > d_stat.st_mtime + 1:
            changed.append(rel)
    return changed


def sync(rel_paths: list[str]) -> list[str]:
    synced: list[str] = []
    for rel in rel_paths:
        src = os.path.join(ROOT, rel)
        dst = os.path.join(RELEASE, rel)
        if _is_excluded(rel):
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        synced.append(rel)
    return synced


def main() -> int:
    parser = argparse.ArgumentParser(description="مزامنة ملفات التطوير إلى release/")
    parser.add_argument("--check", action="store_true", help="عرض الملفات المعدّلة دون نسخ")
    parser.add_argument("--full", action="store_true", help="إعادة بناء release/ بالكامل")
    parser.add_argument("files", nargs="*", help="ملفات محدّدة للمزامنة (مسار نسبي من جذر المشروع)")
    args = parser.parse_args()

    if args.full:
        # حذف release/ وإعادة نسخ كل شيء نظيفاً
        if os.path.exists(RELEASE):
            shutil.rmtree(RELEASE)
        rels = collect_dev_files()
        synced = sync(rels)
        print(f"إعادة بناء كاملة: {len(synced)} ملف نسخ إلى release/")
        return 0

    if args.files:
        rels = [f.replace("\\", "/") for f in args.files]
        invalid = [r for r in rels if not os.path.exists(os.path.join(ROOT, r))]
        if invalid:
            print("مسارات غير موجودة في dev:", ", ".join(invalid), file=sys.stderr)
            return 1
        synced = sync(rels)
        print(f"تمت مزامنة {len(synced)} ملف:")
        for r in synced:
            print(f"  + {r}")
        return 0

    changed = changed_files()
    if args.check:
        if changed:
            print(f"{len(changed)} ملف معدّل جاهز للمزامنة:")
            for r in changed:
                print(f"  ~ {r}")
        else:
            print("لا توجد تغييرات — كل شيء متزامن.")
        return 0

    if not changed:
        print("لا توجد تغييرات — كل شيء متزامن.")
        return 0
    synced = sync(changed)
    print(f"تمت مزامنة {len(synced)} ملف معدّل:")
    for r in synced:
        print(f"  + {r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())