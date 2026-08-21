"""
LEGACY synthetic trainer (do not use for real POS).

This script trained YOLOv8n for 1 epoch on coloured rectangles from
generate_dataset.py. Metrics were ~0 mAP. Kept for reference only.

Use instead:
  python training/scripts/prepare_dataset.py
  python training/scripts/capture_dataset.py
  python training/scripts/train_product_detector.py
  python training/scripts/validate_product_detector.py

See docs/CV_PRODUCT_RECOGNITION.md
"""
import os
import yaml
from ultralytics import YOLO

# 30 starter grocery products as requested
CLASSES = [
    "Maggi", "Lays", "Kurkure", "Parle G", "Dairy Milk", "Good Day", 
    "Coca Cola", "Pepsi", "Sprite", "Milk", "Bread", "Eggs", 
    "Sugar", "Salt", "Rice", "Wheat Flour", "Soap", "Shampoo", 
    "Toothpaste", "Detergent", "Oil", "Tea", "Coffee", "Biscuits", 
    "Juice", "Butter", "Cheese", "Paneer", "Onion", "Potato",
    "Tomato", "Chilly", "Coriander"
]

def create_data_yaml():
    base_dir = r"c:\Users\Samrudh\Downloads\aica-project\dataset".replace("\\", "/")
    data = {
        "path": base_dir,
        "train": "images/train",
        "val": "images/val",
        "test": "images/test",
        "names": {i: name for i, name in enumerate(CLASSES)}
    }
    
    yaml_path = r"c:\Users\Samrudh\Downloads\aica-project\training\data.yaml"
    os.makedirs(os.path.dirname(yaml_path), exist_ok=True)
    with open(yaml_path, "w") as f:
        yaml.safe_dump(data, f, default_flow_style=False)
    print(f"data.yaml created at {yaml_path}")
    return yaml_path

def train_model():
    yaml_path = create_data_yaml()
    
    print("Loading pretrained YOLOv8 model...")
    # Load a pretrained Nano model
    model = YOLO("yolov8n.pt")
    
    print("Starting training...")
    # Train the model for 1 epoch, with small image size for CPU-friendly quick runs
    # set workers=0 to prevent multiprocessing lock errors on Windows systems
    results = model.train(
        data=yaml_path,
        epochs=1,
        imgsz=320,
        batch=8,
        workers=0,
        project=r"c:\Users\Samrudh\Downloads\aica-project\training\runs",
        name="grocery_yolo",
        exist_ok=True
    )
    
    # Locate the best.pt weights
    best_weights_path = os.path.join(
        r"c:\Users\Samrudh\Downloads\aica-project\training\runs",
        "grocery_yolo",
        "weights",
        "best.pt"
    )
    
    # Copy best.pt to vision directory
    dest_dir = r"c:\Users\Samrudh\Downloads\aica-project\vision"
    os.makedirs(dest_dir, exist_ok=True)
    dest_path = os.path.join(dest_dir, "best.pt")
    
    if os.path.exists(best_weights_path):
        import shutil
        shutil.copy(best_weights_path, dest_path)
        print(f"Model trained successfully! Saved to {dest_path}")
    else:
        print("Error: best.pt weights not found!")

if __name__ == "__main__":
    train_model()
