"""
LEGACY synthetic dataset generator (coloured boxes + printed labels).

Do NOT use this for production product recognition. It does not resemble
real supermarket packaging and caused the previous CV failures.

Use training/scripts/capture_dataset.py + real annotation instead.
See docs/CV_PRODUCT_RECOGNITION.md
"""
import os
import cv2
import numpy as np
import random

# 30 starter grocery products as requested
CLASSES = [
    "Maggi", "Lays", "Kurkure", "Parle G", "Dairy Milk", "Good Day", 
    "Coca Cola", "Pepsi", "Sprite", "Milk", "Bread", "Eggs", 
    "Sugar", "Salt", "Rice", "Wheat Flour", "Soap", "Shampoo", 
    "Toothpaste", "Detergent", "Oil", "Tea", "Coffee", "Biscuits", 
    "Juice", "Butter", "Cheese", "Paneer", "Onion", "Potato",
    "Tomato", "Chilly", "Coriander"
]

def generate_synthetic_data(num_train_per_class=10, num_val_per_class=3):
    base_dir = r"c:\Users\Samrudh\Downloads\aica-project\dataset"
    
    # Create directories for training, validation, and testing
    for split in ["train", "val", "test"]:
        os.makedirs(os.path.join(base_dir, "images", split), exist_ok=True)
        os.makedirs(os.path.join(base_dir, "labels", split), exist_ok=True)
        
    img_size = 320  # Fast CPU training size
    
    for class_idx, class_name in enumerate(CLASSES):
        print(f"Generating synthetic images for: {class_name} (class {class_idx})")
        
        # We generate train and validation datasets.
        # We also put a few test images.
        splits_info = [("train", num_train_per_class), ("val", num_val_per_class), ("test", 2)]
        for split, count in splits_info:
            for i in range(count):
                # 1. Create a random colored background
                bg_color = (random.randint(40, 220), random.randint(40, 220), random.randint(40, 220))
                img = np.zeros((img_size, img_size, 3), dtype=np.uint8)
                img[:] = bg_color
                
                # 2. Determine random shape/location for the grocery item
                box_w = random.randint(120, 200)
                box_h = random.randint(80, 140)
                
                max_x = img_size - box_w
                max_y = img_size - box_h
                
                x = random.randint(0, max_x)
                y = random.randint(0, max_y)
                
                # Draw mock packaging box
                prod_color = (random.randint(20, 240), random.randint(20, 240), random.randint(20, 240))
                cv2.rectangle(img, (x, y), (x + box_w, y + box_h), prod_color, -1)
                cv2.rectangle(img, (x, y), (x + box_w, y + box_h), (255, 255, 255), 2)
                
                # Draw some visual texture on the box
                cv2.line(img, (x, y + 20), (x + box_w, y + 20), (255, 255, 255), 1)
                cv2.line(img, (x, y + box_h - 20), (x + box_w, y + box_h - 20), (255, 255, 255), 1)
                
                # Write product name inside the box
                font = cv2.FONT_HERSHEY_SIMPLEX
                font_scale = 0.55
                thickness = 2
                text_size = cv2.getTextSize(class_name, font, font_scale, thickness)[0]
                
                text_x = x + (box_w - text_size[0]) // 2
                text_y = y + (box_h + text_size[1]) // 2
                # Drop shadow
                cv2.putText(img, class_name, (text_x + 1, text_y + 1), font, font_scale, (0, 0, 0), thickness, cv2.LINE_AA)
                # Front text
                cv2.putText(img, class_name, (text_x, text_y), font, font_scale, (255, 255, 255), thickness, cv2.LINE_AA)
                
                # 3. Add random noise circles/rectangles to act as distractor features in background
                for _ in range(5):
                    cx = random.randint(0, img_size)
                    cy = random.randint(0, img_size)
                    r = random.randint(4, 15)
                    cv2.circle(img, (cx, cy), r, (random.randint(50, 255), random.randint(50, 255), random.randint(50, 255)), -1)
                    
                # 4. Save image
                filename = f"{class_name.lower().replace(' ', '_')}_{i}.jpg"
                img_path = os.path.join(base_dir, "images", split, filename)
                cv2.imwrite(img_path, img)
                
                # 5. Save matching label in YOLO format
                # Label format: class_id center_x center_y width height (normalized)
                center_x = (x + box_w / 2.0) / img_size
                center_y = (y + box_h / 2.0) / img_size
                norm_w = box_w / img_size
                norm_h = box_h / img_size
                
                label_filename = f"{class_name.lower().replace(' ', '_')}_{i}.txt"
                label_path = os.path.join(base_dir, "labels", split, label_filename)
                with open(label_path, "w") as f:
                    f.write(f"{class_idx} {center_x:.6f} {center_y:.6f} {norm_w:.6f} {norm_h:.6f}\n")
                    
    print(f"Starter dataset containing {len(CLASSES)} classes generated successfully!")

if __name__ == "__main__":
    generate_synthetic_data()
