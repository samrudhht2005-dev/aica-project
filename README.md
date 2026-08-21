# AICA — AI Chartered Accountant

**AICA V1.0** is a full-stack web application that combines **point-of-sale billing**, **organisation accounting**, **GST / income-tax workflows**, **computer-vision product scanning**, and a **Gemini-powered AI assistant** into one system for small and mid-size Indian businesses.

This repository is the first stable web release (`v1.0.0`).

---

## What AICA does

AICA is built around two connected interfaces:

| Interface | Purpose |
|-----------|---------|
| **POS** | Scan or search products, build a cart, apply GST, take payment, and save sales to the organisation’s books |
| **Organization** | Manage inventory, expenses, payroll, fixed assets, GST & ITC, income tax, compliance, forecasting, reports, and AI optimisation |

Every completed POS sale updates the same PostgreSQL database that powers dashboards, tax views, and analytics — there is **one set of books**, not a separate billing silo.

---

## Key features

### Point of Sale
- Camera-based product detection (YOLO) — **camera stays OFF until you turn it on**
- Manual product search / barcode-style lookup from warehouse stock
- Cart with live GST (CGST / SGST split for display)
- Cash / card checkout with PDF invoice download
- Stable invoice numbers (`INV-000001` style) from the backend
- Sales overview, analytics charts, sales history, invoices, and product performance — all from **real SQL data**

### Organisation management
- Multi-user auth with organisation isolation
- Warehouse / inventory (stock levels, low-stock alerts)
- Expenses with OCR-assisted invoice upload and ITC classification
- Payroll (employees, CTC, Section 80JJAA cues)
- Fixed assets & depreciation-oriented ledger
- GST & ITC working from live sales and purchases
- Income-tax estimates from books + regime settings
- Compliance calendar, forecasting, what-if simulator, reports export

### AI assistant
- Floating chat + optional voice (STT/TTS) on app screens
- Context-aware help for the current page
- Powered by Google Gemini (`GEMINI_API_KEY`)

### Localisation
- English (default), **Kannada**, and **Hindi**
- Language preference persists across navigation and refresh
- Covers navigation, POS, organisation UI, and assistant chrome

### Profile & organisation settings
- Edit profile, edit organisation details, log out from the sidebar menu
- Organisation master data (GSTIN, PAN, address, regime, etc.) drives tax and invoices

---

## Tech stack

| Layer | Technology |
|-------|------------|
| Backend | Python, FastAPI, Uvicorn, SQLAlchemy |
| Frontend | Jinja2 templates, Bootstrap 5, Chart.js, vanilla JS |
| Database | PostgreSQL |
| AI | Google Gemini (`google-genai`) |
| Computer vision | Ultralytics YOLO, OpenCV |
| Invoices | FPDF |

---

## Repository layout

```
aica-project/
├── main.py                 # App entry (loads FastAPI from backend/)
├── backend/                # Routes, auth, session cookies, UI mode
├── frontend/               # Templates + static JS/CSS + i18n JSON
├── database/               # SQLAlchemy engine, schema upgrades, tax rules
├── models/                 # ORM models (User, Org, Product, Transaction, …)
├── billing/                # Cart helpers
├── camera/                 # Webcam stream + detection events
├── gemini/                 # Gemini client, tools, voice assist
├── vision/                 # Product detector inference + weights
│   └── weights/
│       └── aica_product_detector.pt
├── training/               # Dataset / train / validate scripts (not runs/)
├── docs/                   # Extra documentation (e.g. CV guide)
├── utils/                  # Shared helpers
├── requirements.txt
├── .env.example
└── .gitignore
```

**Not in Git (by design):** `.env`, `venv/`, training `dataset/`, `training/runs/`, session secrets, and regenerable base YOLO checkpoints.

---

## Prerequisites

- **Python 3.10+** (3.11 recommended)
- **PostgreSQL** with a database (e.g. `aica_db`)
- **Webcam** (optional — only for POS camera scanning)
- A **Gemini API key** from Google AI Studio

---

## Setup

### 1. Clone

```bash
git clone https://github.com/samrudhht2005-dev/aica-project.git
cd aica-project
```

### 2. Virtual environment

```bash
python -m venv venv

# Windows
.\venv\Scripts\activate

# macOS / Linux
source venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Environment variables

Copy the example file and fill in real values:

```bash
cp .env.example .env
```

```env
GEMINI_API_KEY="your_gemini_api_key"
DATABASE_URL="postgresql://USER:PASSWORD@localhost:5432/aica_db"
```

Never commit `.env`.

### 5. Database

Create an empty PostgreSQL database matching `DATABASE_URL`. On first run, AICA creates / upgrades tables via the app’s schema logic.  
Organisation and user records are created through the **Sign up** flow in the UI.

---

## Run

From the project root (with venv active):

```bash
uvicorn main:app --reload
```

Open: [http://127.0.0.1:8000](http://127.0.0.1:8000)

Typical first steps:

1. **Sign up** — create organisation + admin user  
2. Choose **POS** or **Organization** interface  
3. Add products in **Warehouse**  
4. Complete a sale in **POS → Checkout**  
5. Review **Sales**, **GST & ITC**, and dashboards in the organisation UI  

Switch interfaces anytime with **Switch to POS / Switch to Organization** in the sidebar.

---

## Computer vision (POS scanner)

Production detector weights live at:

```text
vision/weights/aica_product_detector.pt
```

Default trained starter classes (extend only with new photos + retrain):

| Class |
|-------|
| Ketchup |
| Fevicol |
| Dairy Milk |
| Lipton Green Tea |

- Accept threshold and related settings: `vision/detector_config.json`  
- Capture / train / validate scripts: `training/scripts/`  
- Full CV notes: [`docs/CV_PRODUCT_RECOGNITION.md`](docs/CV_PRODUCT_RECOGNITION.md)

The camera does **not** start until the user enables it on the POS checkout screen.

---

## Configuration notes

| Setting | Where |
|---------|--------|
| Gemini API | `.env` → `GEMINI_API_KEY` |
| Database | `.env` → `DATABASE_URL` |
| UI language | Language selector (persists) + optional profile language |
| Theme | Light / dark toggle |
| Session cookie secret | Auto-generated under `database/.session_secret` (local only, gitignored) |

---

## Version

| Tag | Meaning |
|-----|---------|
| **v1.0.0** | First stable web version — POS + organisation books + Gemini assistant + CV scanner |

---

## Safety / secrets

This project expects secrets **only** in environment variables (or gitignored local files).

Do **not** put API keys, database passwords, or session secrets in source control.

---

## License / ownership

Private project repository for AICA development. All rights reserved by the repository owner unless otherwise stated.

---

## Quick troubleshooting

| Issue | Check |
|-------|--------|
| App won’t start | venv active? `pip install -r requirements.txt`? |
| DB connection errors | PostgreSQL running? `DATABASE_URL` correct in `.env`? |
| Assistant unavailable | Valid `GEMINI_API_KEY`? Quota not exhausted? |
| Camera blank | Toggle camera **ON** in POS; try another camera index |
| Empty sales analytics | Complete at least one checkout in POS |

---

Built as **AICA V1.0 — First Stable Web Version**.
