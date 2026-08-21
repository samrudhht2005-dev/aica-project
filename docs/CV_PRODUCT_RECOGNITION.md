# AICA Computer Vision — Product Recognition

## Diagnosis (why only Maggi / Coca-Cola / Coffee / Potato seemed to work)

The existing pipeline was **not** failing because “YOLOv8 is bad.” It failed because of **data + dishonest fallbacks**.

| Finding | Detail |
|--------|--------|
| Dataset | `training/generate_dataset.py` draws **synthetic coloured rectangles** with the class name as OpenCV text. **No real product photos.** |
| Size | ~10 train / 3 val / 2 test images **per class** (30 classes) |
| Training | **1 epoch**, `imgsz=320`, batch 8 (`training/train.py`) |
| Val metrics | After that run: **precision=0, recall=0, mAP50=0, mAP50-95=0** (`training/runs/grocery_yolo/results.csv`) |
| Weights loaded | `vision/best.pt` = that synthetic run (also under `training/runs/grocery_yolo/weights/`) |
| Fake “working” classes | `vision/yolo_inference.py` also ran **COCO-pretrained YOLOv8n** and remapped: `bottle→Coca Cola`, `cup→Coffee`, `apple→Potato`, `book→Maggi`, etc. |
| Gemini assist | Cropped boxes were sent to Gemini and auto-added at **hardcoded 0.95** confidence |
| Simulation mode | Random product names with oscillating fake confidence |

So Maggi/Coke/Coffee/Potato appearing “recognized” was largely **COCO remapping + Gemini**, not a trustworthy grocery detector.

Legacy files are **preserved** (`yolov8n.pt`, `vision/best.pt`, old `dataset/`). They are **not** used for real POS acceptance anymore.

---

## Selected starter classes (4)

Chosen from products you actually have on hand (visually distinct):

| ID | Product |
|----|---------|
| 0 | Ketchup |
| 1 | Fevicol |
| 2 | Dairy Milk |
| 3 | Lipton Green Tea |

Defined in `vision/product_classes.py`. Extend only after new photos + retrain.

---

## Model choice

| Option | Decision |
|--------|----------|
| Legacy YOLOv8n + synthetic | Keep on disk; **do not use for POS** |
| **YOLO26n** (Ultralytics 8.4+) | **Preferred** transfer-learning start (`train_product_detector.py`) |
| YOLO11n / YOLOv8n | Automatic fallbacks if YOLO26 weights unavailable |

Use the **nano** variant for real-time webcam on typical laptops.

Production weights path: `vision/weights/aica_product_detector.pt`  
Config: `vision/detector_config.json` (default accept threshold **0.55** — tune after validation; this is **not** “accuracy”).

---

## What you need to collect (required before training)

The current real-product dataset under `dataset/aica_products/` is **empty on purpose**. I will **not** fabricate photos or pretend the model is trained.

### Target

- **4 products** (table above)
- **≈100–150 images per product** (80+ is a workable start if angles/lighting vary)
- Diversity > identical duplicates

### How to photograph (≈1 day)

For each product:

1. Front, slight left, slight right, small rotations  
2. Near / mid / far on a table/counter  
3. Bright indoor, normal, dimmer light  
4. Different backgrounds (table, counter, plain surface)  
5. Slight occlusion / next to another trained product (some frames)

### Capture tool

```bash
python training/scripts/capture_dataset.py --camera 0 --class Ketchup
```

- `SPACE` save · `N`/`P` next/prev class · `Q` quit  
- Files go to `dataset/aica_products/raw/<class>/`

### Annotate (required)

Every image needs a YOLO bounding box `.txt`:

```text
class_id  x_center  y_center  width  height   # all normalized 0–1
```

Tools: [LabelImg](https://github.com/HumanSignal/labelImg), Roboflow, CVAT, etc.  
Class order must match `vision/product_classes.py`.

Then:

```bash
python training/scripts/prepare_dataset.py --split-raw
```

Or export train/val/test YOLO folders into `dataset/aica_products/images|labels/{train,val,test}`.

Suggested split: **70% train / 20% val / 10% test**.

---

## Train → validate → deploy

```bash
# 1) Structure + data.yaml
python training/scripts/prepare_dataset.py

# 2) Train (blocks if too few images)
python training/scripts/train_product_detector.py --model yolo26n.pt --epochs 80 --imgsz 640

# 3) Metrics on held-out test
python training/scripts/validate_product_detector.py --split test

# 4) Real webcam logging (do not mix into train until scored)
python training/scripts/evaluate_camera.py --camera 0 --ground-truth "Dairy Milk"
```

After training, restart AICA. The detector loads `vision/weights/aica_product_detector.pt`.

Tune `confidence_threshold` in `vision/detector_config.json` using val/camera logs (prefer **high precision / low false positives** for billing).

---

## Runtime POS behaviour (after this update)

1. Camera → curated detector only (no COCO grocery remap)  
2. Confidence ≥ threshold → **accepted** candidate  
3. Below threshold → **“Product not confidently recognized.”** (no guess billed)  
4. User clicks **Add to Cart** → `/camera/confirm_product` maps to **SQL** product (price/stock/GST)  
5. Checkout still uses existing POS sale logic  
6. Optional auto-add toggle (off by default)

UI: dynamic status bar (`scanning` / `detected` / `uncertain` / `no_model`) + detection panel with real confidence.

---

## Pipeline for viva / presentation

```text
Raw photos → YOLO labels → train/val/test → transfer learning (YOLO26n)
→ val metrics (P/R/F1/mAP) → camera eval → deploy aica_product_detector.pt
→ class id → inventory SQL → confirm → POS cart → existing billing
```

---

## Known limitations (honest)

- Until real photos are collected and trained, physical camera mode correctly reports **detector not trained / not confidently recognized**.  
- Simulation mode is **demo-only**, not CV accuracy.  
- 4 classes only; add more later with the same pipeline.  
- Model confidence ≠ accuracy; always cite validation mAP / camera logs.

---

## Warehouse note

Detected names must exist as `Product` rows **for the signed-in organisation**. Add **Ketchup, Fevicol, Dairy Milk, Lipton Green Tea** under Warehouse if the org inventory is empty.
