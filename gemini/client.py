import os
import logging
import json
import re
import time
import concurrent.futures
from datetime import datetime
from dotenv import load_dotenv
from google import genai
from google.genai import types
from backend.money import INR_UNIT_LOCK, sanitize_ai_amount, to_float

try:
    from backend.runtime_paths import load_runtime_env
    load_runtime_env()
except Exception:
    load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MODEL_NAME = os.getenv("AICA_GEMINI_MODEL", "gemini-3.5-flash-lite")

# Keep IRA/desktop responsive when Gemini is slow or quota-exhausted.
GEMINI_REQUEST_TIMEOUT_S = float(os.getenv("AICA_GEMINI_TIMEOUT_S", "25"))
GEMINI_MAX_RETRIES_PER_MODEL = int(os.getenv("AICA_GEMINI_RETRIES", "0"))
GEMINI_MAX_MODELS = int(os.getenv("AICA_GEMINI_MAX_MODELS", "4"))
# Google GenAI rejects HTTP deadlines under 10s.
GEMINI_HTTP_TIMEOUT_MS = max(10000, int(float(os.getenv("AICA_GEMINI_HTTP_TIMEOUT_MS", "20000"))))
IRA_UNAVAILABLE_MSG = "IRA is temporarily unavailable. Please try again later."
IRA_QUOTA_MSG = (
    "IRA hit Gemini API rate limits (free-tier quota). "
    "Wait about a minute and try again, or check billing/quota at https://ai.dev/rate-limit."
)

# Prefer lite first when primary Flash free-tier buckets are exhausted.
_FALLBACK_MODELS = (
    "gemini-3.5-flash-lite",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-flash-latest",
)

client = None


def _build_client(api_key: str):
    try:
        return genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(
                timeout=GEMINI_HTTP_TIMEOUT_MS,
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        )
    except Exception:
        return genai.Client(api_key=api_key)


if GEMINI_API_KEY:
    try:
        client = _build_client(GEMINI_API_KEY)
    except Exception as e:
        logging.error(f"Failed to initialize Gemini Client: {e}")


class GeminiUnavailableError(Exception):
    """Raised when Gemini cannot serve the request (quota, timeout, offline)."""


def _is_quota_or_rate_limit(exc: BaseException) -> bool:
    text = f"{type(exc).__name__} {exc}".upper()
    return any(
        token in text
        for token in ("429", "RESOURCE_EXHAUSTED", "RATE LIMIT", "QUOTA", "TOO MANY REQUESTS")
    )


def _is_model_missing(exc: BaseException) -> bool:
    text = f"{type(exc).__name__} {exc}".upper()
    return "404" in text or "NOT_FOUND" in text or "NO LONGER AVAILABLE" in text


def _call_generate_content(model, contents, config, timeout_s: float):
    """Run SDK generate_content with a hard wall-clock timeout."""
    def _invoke():
        return client.models.generate_content(
            model=model,
            contents=contents,
            config=config,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        fut = pool.submit(_invoke)
        try:
            return fut.result(timeout=timeout_s)
        except concurrent.futures.TimeoutError as te:
            fut.cancel()
            raise TimeoutError(f"Gemini request timed out after {timeout_s:.0f}s (model={model})") from te


def generate_content_with_fallback(contents, config=None):
    """
    Generate content with limited model fallbacks, per-call timeout, and short exponential backoff.
    Does not retry indefinitely on quota errors.
    """
    global client
    if not client:
        api_key = os.getenv("GEMINI_API_KEY")
        if api_key:
            try:
                client = _build_client(api_key)
            except Exception as init_err:
                logging.error(f"Failed to lazy-initialize Gemini Client: {init_err}")
        if not client:
            raise ValueError(
                "Gemini API Client is not initialized. Please verify your GEMINI_API_KEY in the environment/.env."
            )

    models = [MODEL_NAME]
    for m in _FALLBACK_MODELS:
        if m not in models:
            models.append(m)
    models = models[:GEMINI_MAX_MODELS]

    last_err = None
    saw_quota = False

    for model in models:
        for attempt in range(GEMINI_MAX_RETRIES_PER_MODEL + 1):
            try:
                logging.info("Attempting content generation using model: %s (attempt %s)", model, attempt + 1)
                return _call_generate_content(model, contents, config, GEMINI_REQUEST_TIMEOUT_S)
            except Exception as e:
                last_err = e
                quota = _is_quota_or_rate_limit(e)
                timed_out = isinstance(e, TimeoutError) or "DEADLINE" in f"{e}".upper() or "504" in f"{e}"
                missing = _is_model_missing(e)
                if quota:
                    saw_quota = True
                logging.warning(
                    "Failed generate_content on model %s: %s%s",
                    model,
                    e,
                    " (quota/rate)" if quota else (
                        " (missing)" if missing else (" (timeout)" if timed_out else "")
                    ),
                )
                if missing:
                    break
                if quota:
                    # Brief pause then try next model (different free-tier buckets).
                    time.sleep(1.2)
                    break
                if timed_out:
                    break
                if attempt < GEMINI_MAX_RETRIES_PER_MODEL:
                    time.sleep(min(2 ** attempt, 2))
                    continue
                break

    raise GeminiUnavailableError(IRA_QUOTA_MSG if saw_quota else IRA_UNAVAILABLE_MSG) from last_err


def query_gemini_assistant(question: str, get_db_schema_callback, run_db_query_callback, rag_rules: str = "", extra_system: str = "", history: list | None = None) -> str:
    """
    Queries Gemini with explicit, simple tool schemas and a manual allowlisted
    function-calling loop. Callables are never passed to the SDK for automatic
    schema parsing (lambdas like ``lambda q: ...`` break AFC).
    """
    if not client:
        return (
            "The AI assistant is not configured. Please set GEMINI_API_KEY in the environment "
            "and restart AICA."
        )

    try:
        # Explicit simple OpenAPI-style schemas only — no Python callables / lambdas.
        tool_decls = types.Tool(
            function_declarations=[
                types.FunctionDeclaration(
                    name="get_db_schema",
                    description="Return the AICA SQL table and column reference for read-only analysis.",
                    parameters={
                        "type": "OBJECT",
                        "properties": {},
                    },
                ),
                types.FunctionDeclaration(
                    name="run_db_query",
                    description=(
                        "Run one read-only SQL SELECT against the organisation's books. "
                        "Use a single SELECT statement only."
                    ),
                    parameters={
                        "type": "OBJECT",
                        "properties": {
                            "query": {
                                "type": "STRING",
                                "description": "A single read-only SQL SELECT statement.",
                            }
                        },
                        "required": ["query"],
                    },
                ),
            ]
        )

        allowed_tools = {
            "get_db_schema": lambda _args: get_db_schema_callback(),
            "run_db_query": lambda args: run_db_query_callback(str((args or {}).get("query") or "")),
        }

        system_instruction = (
            "You are AICA (AI Chartered Accountant), an intelligent assistant for financial audits, corporate tax planning, "
            "warehouse stock management, and business transaction analysis. You have access to tools that query the database. "
            "Always use the schema tool first if you are unsure of tables or columns, and then run read-only SELECT queries to "
            "fetch facts, calculate metrics, and answer questions. Note: DO NOT perform SQL updates or inserts, only SELECT queries. "
            "Be professional, accurate, and concise. Return responses formatted in clean markdown.\n\n"
            f"{INR_UNIT_LOCK}\n"
            "Database numeric columns are already absolute INR. Never reinterpret them as lakhs/crores. "
            "Tax liability is money owed (not a 'benefit'). Tax saving/credit/refund are separate concepts.\n\n"
        )

        if rag_rules:
            system_instruction += (
                "RAG TAX RULES CONTEXT:\n"
                "Here are the relevant Indian tax & GST rules retrieved for this query. "
                "You MUST reference these rules and cite specific sections (e.g. Section 80JJAA, Section 17(5) Blocked Credit) "
                "when generating recommendations, calculations, or explanations.\n"
                f"{rag_rules}\n\n"
            )
        if extra_system:
            system_instruction += extra_system + "\n\n"

        contents = []
        if history:
            for turn in history[-8:]:
                role = "user" if turn.get("role") == "user" else "model"
                text = (turn.get("text") or "").strip()
                if text:
                    contents.append(types.Content(role=role, parts=[types.Part.from_text(text=text)]))
        contents.append(
            types.Content(
                role="user",
                parts=[types.Part.from_text(text=question)]
            )
        )

        config = types.GenerateContentConfig(
            tools=[tool_decls],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            system_instruction=system_instruction,
        )

        for _ in range(5):
            response = generate_content_with_fallback(
                contents=contents,
                config=config
            )

            if not response.function_calls:
                return response.text or "No response text was generated."

            if response.candidates:
                contents.append(response.candidates[0].content)

            tool_parts = []
            for function_call in response.function_calls:
                name = function_call.name
                args = dict(function_call.args or {})
                handler = allowed_tools.get(name)
                if handler is None:
                    logging.warning("Gemini requested unknown tool: %s", name)
                    result = f"Error: Tool '{name}' is not available."
                else:
                    try:
                        result = handler(args)
                    except Exception as tool_err:
                        logging.exception("Tool '%s' failed", name)
                        result = f"Error running tool '{name}': {tool_err}"

                tool_parts.append(
                    types.Part.from_function_response(
                        name=name,
                        response={"result": result}
                    )
                )

            contents.append(
                types.Content(
                    role="user",
                    parts=tool_parts
                )
            )

        return "I need a simpler question to finish that lookup. Please try again with a more specific request."

    except GeminiUnavailableError:
        return IRA_UNAVAILABLE_MSG
    except TimeoutError:
        return IRA_UNAVAILABLE_MSG
    except Exception as e:
        if _is_quota_or_rate_limit(e):
            logging.warning("Gemini assistant unavailable (quota/rate): %s", e)
            return IRA_UNAVAILABLE_MSG
        logging.exception("Gemini assistant failed")
        return IRA_UNAVAILABLE_MSG


def classify_product_image(image_bytes: bytes) -> str:
    """
    Classifies a product image bytes into one of the registered database product classes.
    """
    global client
    if not client:
        api_key = os.getenv("GEMINI_API_KEY")
        if api_key:
            try:
                from google import genai
                client = genai.Client(api_key=api_key)
            except Exception as init_err:
                logging.error(f"Failed to lazy-initialize Gemini Client for image: {init_err}")
        if not client:
            return "unknown"
            
    # Get curated POS product classes from database options
    from database.db import SessionLocal
    from models.db_models import Product
    from vision.product_classes import PRODUCT_CLASSES
    db = SessionLocal()
    try:
        products = db.query(Product).all()
        valid_classes = sorted({p.name for p in products if p.name} | set(PRODUCT_CLASSES))
    except Exception as e:
        logging.error(f"Error fetching product list for classification: {e}")
        valid_classes = list(PRODUCT_CLASSES)
    finally:
        db.close()
        
    prompt = (
        "Identify the grocery product or item shown in the image. "
        f"You must classify it into exactly one of these allowed classes: {valid_classes}. "
        "Choose the closest match only if you are confident. "
        "Return a JSON object containing: 'product' (string value representing the matched class name). "
        "If the image does not contain any recognizable grocery product from the list, return 'unknown'. "
        "Do not return any extra formatting or explanation, return ONLY raw JSON."
    )
    
    try:
        response = generate_content_with_fallback(
            contents=[
                types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                prompt
            ]
        )
        text = response.text.strip()
        text = re.sub(r"^```json\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
        data = json.loads(text)
        return data.get("product", "unknown")
    except Exception as e:
        logging.error(f"Gemini product classification failed: {e}")
        return "unknown"


def ocr_and_analyze_invoice(file_bytes: bytes, filename: str) -> dict:
    """
    Multimodal OCR using Gemini 3.5 Flash.
    Extracts vendor, invoice number, date, tax amounts (CGST, SGST, IGST), HSN/SAC, product description, and total value.
    Detects potential duplicate warnings or suspicious items.
    """
    global client
    if not client:
        return {"error": "Gemini API client not initialized."}
        
    mime_type = "application/pdf" if filename.lower().endswith(".pdf") else "image/jpeg"
    
    prompt = (
        "You are an expert financial auditor OCR assistant. Analyze this invoice document. "
        "Extract the following fields and return ONLY a raw JSON object with the keys:\n"
        "- 'vendor': name of the vendor (string)\n"
        "- 'gstin': GSTIN of the vendor if present (string, e.g. 29AAAAA0000A1Z1)\n"
        "- 'invoice_number': invoice identification number (string)\n"
        "- 'invoice_date': date of invoice (string in YYYY-MM-DD format)\n"
        "- 'taxable_value': total taxable value before taxes as absolute INR float (not lakhs)\n"
        "- 'cgst': CGST amount as absolute INR float\n"
        "- 'sgst': SGST amount as absolute INR float\n"
        "- 'igst': IGST amount as absolute INR float\n"
        "- 'total_tax': total GST tax amount as absolute INR float\n"
        "- 'total_amount': grand total including tax as absolute INR float\n"
        "- 'hsn_sac': HSN or SAC code if available (string)\n"
        "- 'description': brief product/service description (string)\n"
        "- 'anomalies': list of any identified anomalies or issues (e.g. missing GSTIN, suspicious amounts, mismatched totals) (list of strings)\n\n"
        "Constraints:\n"
        f"- {INR_UNIT_LOCK}\n"
        "- Output ONLY valid raw JSON. Do not write markdown tags or conversational text."
    )
    
    try:
        response = generate_content_with_fallback(
            contents=[
                types.Part.from_bytes(data=file_bytes, mime_type=mime_type),
                prompt
            ]
        )
        text = response.text.strip()
        text = re.sub(r"^```json\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
        data = json.loads(text)
        for k in ("taxable_value", "cgst", "sgst", "igst", "total_tax", "total_amount"):
            if k in data:
                data[k] = sanitize_ai_amount(data.get(k, 0))
        return data
    except Exception as e:
        logging.error(f"Gemini Invoice OCR failed: {e}")
        return {
            "vendor": "Unknown Vendor",
            "gstin": "",
            "invoice_number": f"INV-ERR-{int(time.time())}",
            "invoice_date": datetime.now().strftime("%Y-%m-%d"),
            "taxable_value": 0.0,
            "cgst": 0.0,
            "sgst": 0.0,
            "igst": 0.0,
            "total_tax": 0.0,
            "total_amount": 0.0,
            "description": "Failed to parse invoice",
            "anomalies": [f"OCR system error: {str(e)}"]
        }


def classify_expense_ai(description: str) -> dict:
    """
    Uses Gemini to classify an expense description into proper categories & subcategories,
    as well as capital vs revenue, and explains why.
    """
    prompt = (
        f"Analyze this expense details or item description: '{description}'. "
        "Determine the appropriate accounting classifications and return ONLY a raw JSON object with the keys:\n"
        "- 'category': Major category (e.g. Rent, Technology, Marketing, Travel, Professional Fees, Office Supplies, Salaries, Utilities, Insurance, Capital Asset, repairs, Employee Welfare)\n"
        "- 'subcategory': Sub-category (e.g. Cloud Services, Office Equipment, Electricity, Advertising, Legal Fees, Internet, Laptop, Taxi, Hotel)\n"
        "- 'is_business': Whether it is a legitimate Business expense (boolean, true/false)\n"
        "- 'classification': 'Revenue' (running costs) or 'Capital' (asset creation/depreciable) (string)\n"
        "- 'estimated_gst_percent': Expected standard GST rate (0, 5, 12, 18, 28) (integer)\n"
        "- 'potential_itc_eligible': 'Yes', 'No', or 'Blocked (Section 17(5))' (string)\n"
        "- 'explanation': Brief, professional, audit-defensible explanation of why you classified it this way under Indian accounting/GST standards (string)\n\n"
        "Constraints:\n"
        "- Return ONLY valid raw JSON without code blocks or extra text."
    )
    
    try:
        response = generate_content_with_fallback(contents=prompt)
        text = response.text.strip()
        text = re.sub(r"^```json\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
        data = json.loads(text)
        return data
    except Exception as e:
        logging.error(f"Expense classification failed: {e}")
        return {
            "category": "Other business expenses",
            "subcategory": "Miscellaneous",
            "is_business": True,
            "classification": "Revenue",
            "estimated_gst_percent": 18,
            "potential_itc_eligible": "Yes",
            "explanation": f"Default fallback classification. Error: {str(e)}"
        }


def generate_tax_recommendations(org_data: dict, financial_summary: dict, rules_context: dict) -> list:
    """
    RAG-powered optimization engine. Compiles organization data, financial data, and relevant tax rules.
    Invokes Gemini to identify legitimate tax saving opportunities.
    """
    prompt = (
        "You are AICA (AI Chartered Accountant), a high-end tax optimization system. "
        "Evaluate the following organizational profile, financial status, and official Indian tax rules. "
        "Identify legal tax saving and optimization opportunities. Do NOT recommend illegal tax evasion.\n\n"
        f"Organization Data:\n{json.dumps(org_data, indent=2)}\n\n"
        f"Financial Summary:\n{json.dumps(financial_summary, indent=2)}\n\n"
        f"Tax Rules Context:\n{json.dumps(rules_context, indent=2)}\n\n"
        "For each opportunity found, generate a detailed recommendation. Return ONLY a raw JSON list of objects, "
        "where each object contains the following keys:\n"
        "- 'title': Short descriptive title of the opportunity (string)\n"
        "- 'detected_item': What you detected in the data (string)\n"
        "- 'reason': Reasoning based on the rules and data (string)\n"
        "- 'rule_section': Specific Section/Rule Reference (string, e.g. Section 80JJAA, Section 32, Section 17(5) ITC)\n"
        "- 'eligibility_conditions': Conditions the organization must satisfy to claim it (string)\n"
        "- 'required_documents': Documents or evidence required to support the claim (string)\n"
        "- 'estimated_tax_impact': Approximate potential TAX SAVING in absolute INR rupees as a bare float "
        "(e.g. 18000.00 for eighteen thousand rupees). NEVER lakhs/crores. NEVER call a tax liability a benefit.\n"
        "- 'confidence_level': Your confidence percentage (0-100) (float)\n"
        "- 'severity': 'Critical', 'High', 'Medium', or 'Low' (string)\n"
        "- 'status': 'Potential eligibility' or 'Recommendation requiring verification' or 'Confirmed calculation' (string)\n"
        "- 'impact_type': One of tax_saving | tax_credit | risk_avoided | liability_delta (string)\n\n"
        "Constraints:\n"
        f"- {INR_UNIT_LOCK}\n"
        "- CRITICAL EXAMPLE: turnover 1670.80 means ₹1,670.80 (one thousand six hundred seventy rupees). "
        "Writing '₹1,670.8 Lakhs' is WRONG and forbidden. estimated_tax_impact for that turnover cannot be crores.\n"
        "- estimated_tax_impact must be a plausible tax SAVING in absolute INR, typically far below turnover. "
        "If expenses are present in the financial summary, do not claim 'zero expenses'.\n"
        "- status must NOT be 'Confirmed calculation' — use 'Requires Verification' or 'Potential eligibility'.\n"
        "- Output ONLY a valid JSON list. Do not include codeblocks or explanations outside the JSON."
    )
    
    try:
        response = generate_content_with_fallback(contents=prompt)
        text = response.text.strip()
        text = re.sub(r"^```json\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
        data = json.loads(text)
        if isinstance(data, list):
            for row in data:
                if isinstance(row, dict) and "estimated_tax_impact" in row:
                    row["estimated_tax_impact"] = sanitize_ai_amount(row.get("estimated_tax_impact", 0))
        return data
    except Exception as e:
        logging.error(f"Generate tax recommendations failed: {e}")
        return []


def simulate_what_if_scenario(scenario_type: str, params: dict, financial_summary: dict) -> dict:
    """
    Simulates a 'What-If' scenario. Runs financial calculations and projects impact on turnover, profit, GST, tax, and cash flow.
    """
    prompt = (
        f"You are a corporate financial analyst. Simulate a '{scenario_type}' scenario for the organization.\n"
        f"Scenario Details / Parameters:\n{json.dumps(params, indent=2)}\n\n"
        f"Current Financial Summary:\n{json.dumps(financial_summary, indent=2)}\n\n"
        "Compute the changes and return a raw JSON object containing keys:\n"
        "- 'salary_cost_change': Change in employee costs in absolute INR (float)\n"
        "- 'asset_capital_cost': Change in asset capital cost in absolute INR (float)\n"
        "- 'revenue_change': Change in revenue/sales in absolute INR (float)\n"
        "- 'expense_change': Change in operating/other expenses in absolute INR (float)\n"
        "- 'gst_impact': Change in net GST payable (positive = more payable) in absolute INR (float)\n"
        "- 'depreciation_deduction': Change in depreciation claim in absolute INR (float)\n"
        "- 'profit_impact': Change in net profit in absolute INR (float)\n"
        "- 'estimated_tax_impact': Change in estimated income-tax LIABILITY in absolute INR "
        "(positive = higher liability). Do NOT label this as a benefit.\n"
        "- 'cash_flow_impact': Net cash flow impact in absolute INR (float)\n"
        "- 'eligible_incentives': Text list of potential deductions triggered (e.g. Section 80JJAA) (string)\n"
        "- 'ai_narrative': Professional explanation. When mentioning money, use absolute INR "
        "(e.g. ₹1,607.80). Never write '₹1,607.80 lakh'.\n\n"
        "Constraints:\n"
        f"- {INR_UNIT_LOCK}\n"
        "- Output ONLY raw JSON without formatting or markdown code blocks."
    )
    
    try:
        response = generate_content_with_fallback(contents=prompt)
        text = response.text.strip()
        text = re.sub(r"^```json\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
        data = json.loads(text)
        money_keys = (
            "salary_cost_change", "asset_capital_cost", "revenue_change", "expense_change",
            "gst_impact", "depreciation_deduction", "profit_impact", "estimated_tax_impact",
            "cash_flow_impact",
        )
        for k in money_keys:
            if k in data:
                data[k] = sanitize_ai_amount(data.get(k, 0))
        return data
    except Exception as e:
        logging.error(f"What-if simulation failed: {e}")
        return {
            "salary_cost_change": 0.0,
            "asset_capital_cost": 0.0,
            "revenue_change": 0.0,
            "expense_change": 0.0,
            "gst_impact": 0.0,
            "depreciation_deduction": 0.0,
            "profit_impact": 0.0,
            "estimated_tax_impact": 0.0,
            "cash_flow_impact": 0.0,
            "eligible_incentives": "None",
            "ai_narrative": f"Simulation failed: {str(e)}"
        }


def generate_forecasting_data(historical_data: dict) -> dict:
    """
    Generates forecasting data for the next 12 months for Revenue, Expenses, Profit, Tax, and Cash Flow.
    """
    prompt = (
        "You are an AI financial forecasting engine. Analyze the historical financial data of this organization:\n"
        f"{json.dumps(historical_data, indent=2)}\n\n"
        "Project financial trends for the next 12 months. Return a raw JSON object containing:\n"
        "- 'months': List of next 12 month names (list of strings, e.g. ['Jan', 'Feb', ...])\n"
        "- 'revenue_actual_vs_forecast': List of 12 forecasted revenue values in absolute INR (floats)\n"
        "- 'expenses_actual_vs_forecast': List of 12 forecasted expense values in absolute INR (floats)\n"
        "- 'profit_actual_vs_forecast': List of 12 forecasted profit values in absolute INR (floats)\n"
        "- 'tax_actual_vs_forecast': List of 12 forecasted tax LIABILITY values in absolute INR (floats)\n"
        "- 'cashflow_actual_vs_forecast': List of 12 forecasted cash-flow values in absolute INR (floats)\n"
        "- 'insights': Short bullet insights (list of strings). Money in prose must stay absolute INR — never 'lakh' as the unit of an already-rupee number.\n\n"
        f"Constraints:\n- {INR_UNIT_LOCK}\n"
        "- Output ONLY raw JSON without markdown."
    )
    
    try:
        response = generate_content_with_fallback(contents=prompt)
        text = response.text.strip()
        text = re.sub(r"^```json\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
        data = json.loads(text)
        return data
    except Exception as e:
        logging.error(f"Financial forecasting failed: {e}")
        return {
            "months": ["Month 1", "Month 2", "Month 3", "Month 4", "Month 5", "Month 6", "Month 7", "Month 8", "Month 9", "Month 10", "Month 11", "Month 12"],
            "revenue_actual_vs_forecast": [10000] * 12,
            "expenses_actual_vs_forecast": [8000] * 12,
            "profit_actual_vs_forecast": [2000] * 12,
            "tax_actual_vs_forecast": [500] * 12,
            "cashflow_actual_vs_forecast": [1500] * 12,
            "insights": ["Forecast default fallback due to system issue."]
        }


def detect_financial_anomalies(data: dict) -> list:
    """
    Scans organization transaction records and expenses for duplicates, extreme changes, or GST inconsistencies.
    """
    prompt = (
        "You are an AI financial auditor. Scan the following transactional and expense data for anomalies. "
        "Look for duplicates, unexpected category spikes, incorrect tax rates, or suspicious amounts:\n"
        f"{json.dumps(data, indent=2)}\n\n"
        "Return ONLY a raw JSON list of objects, where each object contains:\n"
        "- 'severity': 'Critical', 'High', 'Medium', or 'Low' (string)\n"
        "- 'reason': Clear, detailed explanation of why this was flagged as an anomaly (string)\n"
        "- 'historical_comparison': Short description of average or expected baseline (string)\n"
        "- 'details': Specific details of transaction or invoice (string)\n\n"
        "Constraints:\n"
        "- Return ONLY valid raw JSON."
    )
    
    try:
        response = generate_content_with_fallback(contents=prompt)
        text = response.text.strip()
        text = re.sub(r"^```json\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
        data = json.loads(text)
        return data
    except Exception as e:
        logging.error(f"Anomaly detection failed: {e}")
        return []
