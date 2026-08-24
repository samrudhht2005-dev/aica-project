# Vision package.
# Do NOT import yolo_inference/ultralytics here — that pulls torch at package
# import time and breaks packaged desktop camera endpoints when torch is absent.
# Import YOLOProductDetector lazily from vision.yolo_inference where needed.
