"""
Convert Roboflow YOLO-Seg polygon labels to YOLO detection boxes
and align class IDs with vision.product_classes.PRODUCT_CLASSES.

AICA order:
  0 Ketchup, 1 Fevicol, 2 Dairy Milk, 3 Lipton Green Tea
"""
from __future__ import annotations

import collections
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LABELS = ROOT / "dataset" / "aica_products" / "labels"
IMAGES = ROOT / "dataset" / "aica_products" / "images"

NAME_TO_ID = {
    "ketchup": 0,
    "fevicol": 1,
    "dairy_milk": 2,
    "lipton_green_tea": 3,
}


def prod_from_name(name: str) -> str | None:
    if name.startswith("ketchup"):
        return "ketchup"
    if name.startswith("fevicol"):
        return "fevicol"
    if name.startswith("dairy_milk"):
        return "dairy_milk"
    if name.startswith("lipton"):
        return "lipton_green_tea"
    return None


def poly_to_bbox_line(parts: list[str], forced_cls: int | None) -> str | None:
    coords = [float(x) for x in parts[1:]]
    if len(coords) < 4:
        return None

    if len(coords) == 4:
        cx, cy, w, h = coords
        cls = forced_cls if forced_cls is not None else int(float(parts[0]))
        return f"{cls} {cx:.6f} {cy:.6f} {w:.6f} {h:.6f}"

    xs = coords[0::2]
    ys = coords[1::2]
    xmin, xmax = max(0.0, min(xs)), min(1.0, max(xs))
    ymin, ymax = max(0.0, min(ys)), min(1.0, max(ys))
    w = max(xmax - xmin, 1e-6)
    h = max(ymax - ymin, 1e-6)
    cx = (xmin + xmax) / 2.0
    cy = (ymin + ymax) / 2.0
    cls = forced_cls if forced_cls is not None else int(float(parts[0]))
    return f"{cls} {cx:.6f} {cy:.6f} {w:.6f} {h:.6f}"


def main():
    n_poly = n_box = n_fixed = empty = 0
    for p in LABELS.rglob("*.txt"):
        prod = prod_from_name(p.name)
        expected = NAME_TO_ID.get(prod) if prod else None
        raw = p.read_text(encoding="utf-8").replace("\\n", "\n")
        out_lines: list[str] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) < 5:
                continue
            if len(parts) > 5:
                n_poly += 1
            else:
                n_box += 1
            if expected is not None and int(float(parts[0])) != expected:
                n_fixed += 1
            bbox = poly_to_bbox_line(parts, expected)
            if bbox:
                out_lines.append(bbox)
        if out_lines:
            p.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
        else:
            empty += 1
            p.write_text("", encoding="utf-8")

    print(f"polygons->boxes: {n_poly}, already-bbox: {n_box}, class fixes: {n_fixed}, empty: {empty}")

    by_id: dict[int, collections.Counter] = collections.defaultdict(collections.Counter)
    for p in LABELS.rglob("*.txt"):
        prod = prod_from_name(p.name) or "other"
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                by_id[int(line.split()[0])][prod] += 1
    print("class map (id -> filename product counts):")
    for cid in sorted(by_id):
        print(f"  {cid}: {dict(by_id[cid])}")

    for split in ("train", "val", "test"):
        imgs = {
            p.stem
            for p in (IMAGES / split).iterdir()
            if p.suffix.lower() in {".jpg", ".jpeg", ".png"}
        }
        lbls = {p.stem for p in (LABELS / split).glob("*.txt")}
        empty_lbl = sum(
            1
            for stem in imgs & lbls
            if not (LABELS / split / f"{stem}.txt").read_text(encoding="utf-8").strip()
        )
        print(
            f"{split}: images={len(imgs)} labels={len(lbls)} "
            f"missing_labels={len(imgs - lbls)} empty_labels={empty_lbl}"
        )


if __name__ == "__main__":
    main()
