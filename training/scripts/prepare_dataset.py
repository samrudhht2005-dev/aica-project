"""
Prepare YOLO dataset structure and data.yaml for AICA curated products.

Does NOT fabricate images. Place real photos under dataset/aica_products/raw/<class>/
then annotate into images/{train,val,test} + labels/{train,val,test}.
"""
from __future__ import annotations

import argparse
import random
import shutil
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
DATASET = ROOT / "dataset" / "aica_products"

import sys
sys.path.insert(0, str(ROOT))
from vision.product_classes import PRODUCT_CLASSES  # noqa: E402


def ensure_dirs():
    for split in ("train", "val", "test"):
        (DATASET / "images" / split).mkdir(parents=True, exist_ok=True)
        (DATASET / "labels" / split).mkdir(parents=True, exist_ok=True)
    for cls in PRODUCT_CLASSES:
        (DATASET / "raw" / cls.replace(" ", "_").lower()).mkdir(parents=True, exist_ok=True)


def write_data_yaml(out: Path | None = None) -> Path:
    out = out or (DATASET / "data.yaml")
    data = {
        "path": str(DATASET).replace("\\", "/"),
        "train": "images/train",
        "val": "images/val",
        "test": "images/test",
        "names": {i: name for i, name in enumerate(PRODUCT_CLASSES)},
    }
    with open(out, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
    print(f"Wrote {out}")
    return out


def count_split() -> dict:
    stats = {}
    for split in ("train", "val", "test"):
        imgs = list((DATASET / "images" / split).glob("*.jpg")) + list(
            (DATASET / "images" / split).glob("*.png")
        )
        labels = list((DATASET / "labels" / split).glob("*.txt"))
        stats[split] = {"images": len(imgs), "labels": len(labels)}
    return stats


def split_from_raw(train_ratio=0.7, val_ratio=0.2, seed=42):
    """
    Move/copy already-annotated pairs from raw staging if present as
    raw/<class>/*.jpg with matching .txt beside them.

    Prefer: collect with capture_dataset.py, annotate with LabelImg/Roboflow,
    export YOLO format into images/labels splits manually, or use this helper
    when each raw folder already contains paired .jpg+.txt.
    """
    random.seed(seed)
    ensure_dirs()
    moved = 0
    for cls_idx, cls in enumerate(PRODUCT_CLASSES):
        folder = DATASET / "raw" / cls.replace(" ", "_").lower()
        images = sorted(list(folder.glob("*.jpg")) + list(folder.glob("*.png")))
        pairs = []
        for img in images:
            lbl = img.with_suffix(".txt")
            if lbl.exists():
                pairs.append((img, lbl))
        random.shuffle(pairs)
        n = len(pairs)
        if n == 0:
            continue
        n_train = int(n * train_ratio)
        n_val = int(n * val_ratio)
        splits = (
            [("train", pairs[:n_train])]
            + [("val", pairs[n_train : n_train + n_val])]
            + [("test", pairs[n_train + n_val :])]
        )
        for split, items in splits:
            for i, (img, lbl) in enumerate(items):
                stem = f"{cls.replace(' ', '_').lower()}_{split}_{i:04d}"
                shutil.copy2(img, DATASET / "images" / split / f"{stem}{img.suffix.lower()}")
                shutil.copy2(lbl, DATASET / "labels" / split / f"{stem}.txt")
                moved += 1
    print(f"Copied {moved} annotated pairs into train/val/test.")
    return moved


def main():
    parser = argparse.ArgumentParser(description="Prepare AICA product detection dataset")
    parser.add_argument("--split-raw", action="store_true", help="Split annotated raw/ into train/val/test")
    args = parser.parse_args()
    ensure_dirs()
    write_data_yaml()
    if args.split_raw:
        split_from_raw()
    print("Dataset stats:", count_split())
    print("Classes:")
    for i, name in enumerate(PRODUCT_CLASSES):
        print(f"  {i}: {name}")
    print(
        "\nNext: put real photos in dataset/aica_products/raw/<class>/, "
        "annotate (YOLO txt), then --split-raw or export into images/labels."
    )


if __name__ == "__main__":
    main()
