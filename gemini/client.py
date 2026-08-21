import os
import logging
import json
import re
import time
from datetime import datetime
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MODEL_NAME = "gemini-3.5-flash"

client = None
if GEMINI_API_KEY:
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
    except Exception as e:
        logging.error(f"Failed to initialize Gemini Client: {e}")

def generate_content_with_fallback(contents, config=None):
    """
    Generate content with automatic fallback to stable Gemini models if the primary model fails.
    """
    global client
    if not client:
        api_key = os.getenv("GEMINI_API_KEY")
        if api_key:
            try:
                from google import genai
                client = genai.Client(api_key=api_key)
            except Exception as init_err:
                logging.error(f"Failed to lazy-initialize Gemini Client: {init_err}")
        if not client:
            raise ValueError("Gemini API Client is not initialized. Please verify your GEMINI_API_KEY in the environment/.env.")

    # We list the models starting with the user-selected/configured MODEL_NAME
    models = [MODEL_NAME]
    
    # Add other common stable/active models as fallbacks
    fallbacks = ["gemini-3.6-flash", "gemini-3.5-flash-lite", "gemini-2.0-flash"]
    for m in fallbacks:
        if m not in models:
            models.append(m)
            
    last_err = None
    for model in models:
        try:
            logging.info(f"Attempting content generation using model: {model}")
            response = client.models.generate_content(
                model=model,
                contents=contents,
                config=config
            )
            return response
        except Exception as e:
            last_err = e
            logging.warning(f"Failed generate_content on model {model}: {e}. Retrying fallback...")
            
    if last_err:
        raise last_err


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

    except Exception as e:
        logging.exception("Gemini assistant failed")
        return "Sorry, I couldn't process that request right now. Please try again."


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
        "- 'taxable_value': total taxable value before taxes (float)\n"
        "- 'cgst': CGST amount (float, default 0.0)\n"
        "- 'sgst': SGST amount (float, default 0.0)\n"
        "- 'igst': IGST amount (float, default 0.0)\n"
        "- 'total_tax': total GST tax amount (float, default 0.0)\n"
        "- 'total_amount': grand total invoice amount including tax (float)\n"
        "- 'hsn_sac': HSN or SAC code if available (string)\n"
        "- 'description': brief product/service description (string)\n"
        "- 'anomalies': list of any identified anomalies or issues (e.g. missing GSTIN, suspicious amounts, mismatched totals) (list of strings)\n\n"
        "Constraints:\n"
        "- Output ONLY valid raw JSON. Do not write markdown tags (like ```json) or any conversational text."
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
        "- 'estimated_tax_impact': Approximate potential tax savings or cash-flow impact in Rs (float)\n"
        "- 'confidence_level': Your confidence percentage (0-100) (float)\n"
        "- 'severity': 'Critical', 'High', 'Medium', or 'Low' (string)\n"
        "- 'status': 'Potential eligibility' or 'Recommendation requiring verification' or 'Confirmed calculation' (string)\n\n"
        "Constraints:\n"
        "- Output ONLY a valid JSON list. Do not include codeblocks or explanations outside the JSON."
    )
    
    try:
        response = generate_content_with_fallback(contents=prompt)
        text = response.text.strip()
        text = re.sub(r"^```json\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
        data = json.loads(text)
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
        "- 'salary_cost_change': Change in employee costs (float)\n"
        "- 'asset_capital_cost': Change in asset capital cost (float)\n"
        "- 'revenue_change': Change in revenue/sales (float)\n"
        "- 'expense_change': Change in operating/other expenses (float)\n"
        "- 'gst_impact': Expected change in net GST payable or ITC available (float)\n"
        "- 'depreciation_deduction': Expected change in depreciation claim (float)\n"
        "- 'profit_impact': Change in net profit (float)\n"
        "- 'estimated_tax_impact': Estimated income tax liability impact (float)\n"
        "- 'cash_flow_impact': Net cash flow impact (positive/negative) (float)\n"
        "- 'eligible_incentives': Text list of potential deductions triggered (e.g. Section 80JJAA) (string)\n"
        "- 'ai_narrative': A professional explanation summarizing the operational, profit, tax, and cash flow implications (string)\n\n"
        "Constraints:\n"
        "- Output ONLY raw JSON without formatting or markdown code blocks."
    )
    
    try:
        response = generate_content_with_fallback(contents=prompt)
        text = response.text.strip()
        text = re.sub(r"^```json\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
        data = json.loads(text)
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
        "- 'revenue_actual_vs_forecast': List of 12 values representing forecasted revenue (list of floats)\n"
        "- 'expenses_actual_vs_forecast': List of 12 values representing forecasted expenses (list of floats)\n"
        "- 'profit_actual_vs_forecast': List of 12 values representing forecasted profit (list of floats)\n"
        "- 'tax_actual_vs_forecast': List of 12 values representing forecasted tax liability (list of floats)\n"
        "- 'cashflow_actual_vs_forecast': List of 12 values representing forecasted net cash flow (list of floats)\n"
        "- 'insights': List of 3-4 professional financial forecasting takeaways (list of strings)\n\n"
        "Constraints:\n"
        "- Output ONLY raw JSON."
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
