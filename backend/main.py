from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from backend.routes import router, init_camera, handle_vision_detection
import backend.routes as routes
from backend.auth import is_public_path, session_from_request
from backend.runtime_paths import (
    load_runtime_env, static_dir, templates_dir, APP_VERSION, is_frozen,
)
from database.db import SessionLocal
from models.db_models import User, Organization

# Load .env / AppData config before other modules that read env at import time
load_runtime_env()

app = FastAPI(title="AICA POS AI Billing System", version=APP_VERSION)

# Mount Static Files — path resolved for web + PyInstaller desktop
_static = static_dir()
if _static.is_dir():
    app.mount("/static", StaticFiles(directory=str(_static)), name="static")

# Include Router endpoints
app.include_router(router)


@app.get("/health")
def health():
    """Desktop launcher readiness probe — public, no auth."""
    from backend.runtime_paths import app_release_info
    info = app_release_info()
    return {
        "ok": True,
        "app": "AICA",
        "version": info.get("version") or APP_VERSION,
        "build": info.get("build"),
        "desktop": is_frozen() or __import__("os").environ.get("AICA_DESKTOP") == "1",
    }

@app.middleware("http")
async def require_authentication(request: Request, call_next):
    path = request.url.path
    if is_public_path(path):
        return await call_next(request)
    payload = session_from_request(request)
    if not payload:
        if path.startswith("/api") or request.headers.get("accept", "").find("application/json") >= 0:
            return JSONResponse({"error": "Authentication required"}, status_code=401)
        return RedirectResponse("/login", status_code=303)
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == payload["uid"]).first()
        org = db.query(Organization).filter(Organization.id == payload["oid"]).first() if user else None
        if not user or not org or user.org_id != org.id:
            if path.startswith("/api"):
                return JSONResponse({"error": "Authentication required"}, status_code=401)
            return RedirectResponse("/login", status_code=303)
        request.state.user_id = user.id
        request.state.org_id = org.id
    finally:
        db.close()
    return await call_next(request)

def seed_database_products():
    print("Checking and seeding database products...")
    from database.db import SessionLocal
    from models.db_models import Product
    from vision.yolo_inference import CLASSES
    import sqlalchemy

    db = SessionLocal()
    try:
        # Get all existing product names in DB in lowercase
        existing_names = {p.name.strip().lower() for p in db.query(Product).all()}
        
        # List of all classes we want to ensure exist in the database
        required_products = list(CLASSES)
        # Add banana just in case (as it is in COCO_MAP)
        if "banana" not in [p.lower() for p in required_products]:
            required_products.append("Banana")
            
        # Add standard default prices and stocks for the products
        default_prices = {
            "Ketchup": 95.0,
            "Fevicol": 60.0,
            "Dairy Milk": 40.0,
            "Lipton Green Tea": 120.0,
            "Maggi": 15.0,
            "Lays": 20.0,
            "Kurkure": 20.0,
            "Parle G": 10.0,
            "Good Day": 25.0,
            "Coca Cola": 40.0,
            "Pepsi": 40.0,
            "Sprite": 40.0,
            "Milk": 25.0,
            "Bread": 30.0,
            "Eggs": 6.0,
            "Sugar": 45.0,
            "Salt": 20.0,
            "Rice": 60.0,
            "Wheat Flour": 50.0,
            "Soap": 35.0,
            "Shampoo": 120.0,
            "Toothpaste": 55.0,
            "Detergent": 90.0,
            "Oil": 140.0,
            "Tea": 80.0,
            "Coffee": 150.0,
            "Biscuits": 25.0,
            "Juice": 60.0,
            "Butter": 50.0,
            "Cheese": 110.0,
            "Paneer": 90.0,
            "Onion": 30.0,
            "Potato": 25.0,
            "Banana": 40.0,
            "Tomato": 20.0,
            "Chilly": 40.0,
            "Coriander": 15.0
        }
        
        seeded_count = 0
        for prod_name in required_products:
            normalized_name = prod_name.strip()
            if normalized_name.lower() not in existing_names:
                price = default_prices.get(normalized_name, 50.0)
                product = Product(name=normalized_name, price=price, stock=100.0)
                db.add(product)
                existing_names.add(normalized_name.lower())
                seeded_count += 1
                
        # Fix any spelling mismatches, e.g., if "Panner" exists, make sure "Paneer" exists
        if "panner" in existing_names and "paneer" not in existing_names:
            panner_db = db.query(Product).filter(sqlalchemy.func.lower(Product.name) == "panner").first()
            if panner_db:
                db.add(Product(name="Paneer", price=panner_db.price, stock=panner_db.stock))
                existing_names.add("paneer")
                seeded_count += 1
                
        if seeded_count > 0:
            db.commit()
            print(f"Database seeded: Added {seeded_count} missing products from model classes.")
        else:
            print("Database check complete. All products already seeded.")
    except Exception as e:
        print(f"Error seeding database products: {e}")
    finally:
        db.close()

@app.on_event("startup")
def startup_event():
    print("FastAPI starting up. Camera/YOLO deferred until POS camera is used.")
    # Do not seed demo inventory globally — each organisation starts empty.
    # Do not init_camera here — it blocked desktop startup with OpenCV/YOLO imports.


@app.on_event("shutdown")
def shutdown_event():
    print("FastAPI shutting down. Disleasing camera interfaces...")
    if routes.streamer:
        routes.streamer.stop()
