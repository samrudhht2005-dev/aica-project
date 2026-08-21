import os
import json
import re
import logging
from google.genai import types
from gemini.client import client, MODEL_NAME, generate_content_with_fallback

def process_voice_billing(audio_data: bytes, mime_type: str = "audio/wav") -> dict:
    """
    Future-proof voice billing. Transcribes audio and returns a list of items to bill.
    Sends audio bytes directly to Gemini 3.5 Flash (which is multimodal).
    """
    if not client:
        return {"error": "Gemini client not initialized"}
        
    try:
        prompt = (
            "Analyze the voice command. Extract grocery items mentioned for purchase, along with their quantities. "
            "Return a JSON list of objects, each containing: 'product' (string, e.g. 'Maggi', 'Lays') and 'quantity' (float). "
            "Do not return any extra markdown formatting or explanations, return ONLY raw JSON."
        )
        response = generate_content_with_fallback(
            contents=[
                types.Part.from_bytes(data=audio_data, mime_type=mime_type),
                prompt
            ]
        )
        text = response.text.strip()
        # Clean markdown code block formatting
        text = re.sub(r"^```json\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
        items = json.loads(text)
        return {"success": True, "items": items}
    except Exception as e:
        logging.error(f"Voice billing processing error: {e}")
        return {"error": f"Voice billing processing failed: {str(e)}"}

def process_voice_search(audio_data: bytes, mime_type: str = "audio/wav") -> dict:
    """
    Future-proof voice search. Extracts search queries or product names from spoken commands.
    """
    if not client:
        return {"error": "Gemini client not initialized"}
        
    try:
        prompt = (
            "Analyze the voice command. What product is the user searching for? "
            "Return the name of the product as a single word or phrase. "
            "If no product name is clear, return 'unknown'."
        )
        response = generate_content_with_fallback(
            contents=[
                types.Part.from_bytes(data=audio_data, mime_type=mime_type),
                prompt
            ]
        )
        return {"success": True, "query": response.text.strip()}
    except Exception as e:
        logging.error(f"Voice search processing error: {e}")
        return {"error": f"Voice search processing failed: {str(e)}"}

def process_voice_checkout(audio_data: bytes, mime_type: str = "audio/wav") -> dict:
    """
    Future-proof voice checkout. Detects payment methods or finalization commands.
    """
    if not client:
        return {"error": "Gemini client not initialized"}
        
    try:
        prompt = (
            "Analyze the voice command. Detect user checkout preferences (e.g. cash, card). "
            "Return a JSON object containing: 'action' (e.g. 'checkout', 'cancel', 'none') and 'payment_method' (e.g. 'cash', 'card', 'unknown'). "
            "Do not return any extra markdown formatting or explanations, return ONLY raw JSON."
        )
        response = generate_content_with_fallback(
            contents=[
                types.Part.from_bytes(data=audio_data, mime_type=mime_type),
                prompt
            ]
        )
        text = response.text.strip()
        text = re.sub(r"^```json\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
        data = json.loads(text)
        return {"success": True, "data": data}
    except Exception as e:
        logging.error(f"Voice checkout processing error: {e}")
        return {"error": f"Voice checkout processing failed: {str(e)}"}
