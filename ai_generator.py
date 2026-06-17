"""
AI question generator using Ollama HTTP API.
"""
import json
import re
import requests

OLLAMA_BASE_URL = "http://localhost:11434"


_BLOOM_DESCRIPTIONS = {
    "Remember":   "recall and recognise facts, definitions, and basic concepts (e.g. list, identify, name)",
    "Understand": "explain ideas or concepts in their own words (e.g. describe, summarise, classify)",
    "Apply":      "use knowledge in new situations or to solve problems (e.g. calculate, demonstrate, solve)",
    "Analyze":    "draw connections, break down information, identify patterns (e.g. compare, distinguish, examine)",
    "Evaluate":   "justify a decision or course of action, make judgements (e.g. argue, defend, critique)",
    "Create":     "produce new or original work, combine ideas in a new way (e.g. design, formulate, construct)",
}


def generate_questions(topic: str, count: int, difficulty: str, bloom_level: str, model: str):
    """
    Generate quiz questions via Ollama.

    Returns a list of validated question dicts (up to `count`), or None on failure.
    Each dict has: text, type, options, correct_answer, suggested_time.
    """
    if bloom_level and bloom_level != "Mixed" and bloom_level in _BLOOM_DESCRIPTIONS:
        bloom_instruction = (
            f"All questions must target the Bloom's Taxonomy level: "
            f"**{bloom_level}** — {_BLOOM_DESCRIPTIONS[bloom_level]}."
        )
    else:
        levels = ", ".join(_BLOOM_DESCRIPTIONS.keys())
        bloom_instruction = (
            f"Distribute questions across all Bloom's Taxonomy levels ({levels}) "
            f"so the set tests a range of cognitive skills."
        )

    prompt = f"""Generate exactly {count} quiz questions about "{topic}" at {difficulty} difficulty level.

{bloom_instruction}

Return ONLY a valid JSON array with no additional text, markdown, or explanation.

Each question must have these exact fields:
- "text": the question string
- "type": either "mcq" or "truefalse"
- "options": for mcq, a list of exactly 4 distinct strings; for truefalse, exactly ["True", "False"]
- "correct_answer": must be one of the options strings (exact match, case-sensitive)
- "suggested_time": integer seconds between 15 and 120

Example format:
[
  {{
    "text": "What is the capital of France?",
    "type": "mcq",
    "options": ["Paris", "London", "Berlin", "Madrid"],
    "correct_answer": "Paris",
    "suggested_time": 20
  }},
  {{
    "text": "The Earth is flat.",
    "type": "truefalse",
    "options": ["True", "False"],
    "correct_answer": "False",
    "suggested_time": 15
  }}
]

Generate {count} questions about "{topic}" at {difficulty} difficulty ({bloom_level} Bloom's level). Return ONLY the JSON array:"""

    try:
        response = requests.post(
            f"{OLLAMA_BASE_URL}/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "temperature": 0.7,
                    "num_predict": 4096,
                },
            },
            timeout=120,
        )
        response.raise_for_status()
        data = response.json()
        raw_text = data.get("response", "")
    except Exception as exc:
        print(f"[ai_generator] Ollama request failed: {exc}")
        return None

    # Extract JSON array from the response text using regex
    json_array = _extract_json_array(raw_text)
    if json_array is None:
        print(f"[ai_generator] Could not extract JSON array from response:\n{raw_text[:500]}")
        return None

    try:
        questions_raw = json.loads(json_array)
    except json.JSONDecodeError as exc:
        print(f"[ai_generator] JSON decode error: {exc}\nText: {json_array[:500]}")
        return None

    if not isinstance(questions_raw, list):
        print("[ai_generator] Parsed JSON is not a list")
        return None

    validated = []
    for i, q in enumerate(questions_raw):
        valid = _validate_question(q, i)
        if valid:
            validated.append(valid)
        if len(validated) >= count:
            break

    if not validated:
        print("[ai_generator] No valid questions after validation")
        return None

    return validated


def _extract_json_array(text: str):
    """Extract the first JSON array from a text string."""
    # Try to find a JSON array directly
    match = re.search(r'\[[\s\S]*\]', text)
    if not match:
        return None

    candidate = match.group(0)

    # Sometimes the model adds trailing garbage — try to find balanced brackets
    depth = 0
    end_idx = None
    for i, ch in enumerate(candidate):
        if ch == '[':
            depth += 1
        elif ch == ']':
            depth -= 1
            if depth == 0:
                end_idx = i + 1
                break

    if end_idx is not None:
        return candidate[:end_idx]
    return candidate


def _validate_question(q, index: int):
    """
    Validate a single question dict.
    Returns a cleaned dict or None if invalid.
    """
    if not isinstance(q, dict):
        print(f"[ai_generator] Question {index} is not a dict")
        return None

    text = q.get("text", "").strip()
    if not text:
        print(f"[ai_generator] Question {index} missing text")
        return None

    q_type = q.get("type", "").strip().lower()
    if q_type not in ("mcq", "truefalse"):
        # Try to infer type
        options = q.get("options", [])
        if isinstance(options, list) and len(options) == 2:
            q_type = "truefalse"
        else:
            q_type = "mcq"

    options = q.get("options", [])
    if not isinstance(options, list):
        print(f"[ai_generator] Question {index} options is not a list")
        return None

    if q_type == "mcq":
        if len(options) != 4:
            print(f"[ai_generator] MCQ question {index} has {len(options)} options (need 4)")
            return None
        options = [str(o).strip() for o in options]
    else:
        # truefalse — normalise
        options = ["True", "False"]

    correct_answer = str(q.get("correct_answer", "")).strip()
    if correct_answer not in options:
        # Attempt case-insensitive match
        match = next((o for o in options if o.lower() == correct_answer.lower()), None)
        if match:
            correct_answer = match
        else:
            print(f"[ai_generator] Question {index} correct_answer '{correct_answer}' not in options {options}")
            return None

    try:
        suggested_time = int(q.get("suggested_time", 30))
    except (TypeError, ValueError):
        suggested_time = 30
    suggested_time = max(15, min(120, suggested_time))

    return {
        "text": text,
        "type": q_type,
        "options": options,
        "correct_answer": correct_answer,
        "suggested_time": suggested_time,
    }


def ai_chat_questions(user_message: str, existing_questions: list, topic: str, model: str):
    """
    Natural-language AI assistant for question bank editing.
    Faculty sends a freeform request; returns a list of new question dicts, or None on failure.
    existing_questions is passed as context so the AI avoids duplicates.
    """
    # Summarise existing bank (text + answer only, to keep prompt compact)
    existing_summary = [
        {"text": q.get("text", ""), "correct_answer": q.get("correct_answer", "")}
        for q in (existing_questions or [])[:30]
    ]

    prompt = f"""You are assisting a teacher who is building a quiz question bank about "{topic}".

Existing questions already in the bank (avoid duplicating these):
{json.dumps(existing_summary, indent=2)}

Teacher's request: {user_message}

Based on the request, generate new quiz questions. Return ONLY a valid JSON array — no other text.
Each question must follow this exact format:
[
  {{
    "text": "Question text here?",
    "type": "mcq",
    "options": ["Option A", "Option B", "Option C", "Option D"],
    "correct_answer": "Option A",
    "suggested_time": 30
  }}
]

Rules:
- "type" must be "mcq" or "truefalse"
- For "truefalse": options must be exactly ["True", "False"]
- For "mcq": exactly 4 distinct options; correct_answer must exactly match one option
- "suggested_time" is seconds, between 15 and 120
- Do not repeat existing questions
- Return ONLY the JSON array, nothing else"""

    try:
        response = requests.post(
            f"{OLLAMA_BASE_URL}/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.75, "num_predict": 3000},
            },
            timeout=120,
        )
        response.raise_for_status()
        raw_text = response.json().get("response", "")
    except Exception as exc:
        print(f"[ai_generator] ai_chat_questions request failed: {exc}")
        return None

    json_array = _extract_json_array(raw_text)
    if json_array is None:
        print(f"[ai_generator] ai_chat: no JSON array found in: {raw_text[:300]}")
        return None

    try:
        raw_list = json.loads(json_array)
    except json.JSONDecodeError:
        return None

    if not isinstance(raw_list, list):
        return None

    validated = [_validate_question(q, i) for i, q in enumerate(raw_list)]
    return [q for q in validated if q] or None


def get_available_models():
    """
    Return list of model name strings from Ollama, or None on failure.
    """
    try:
        response = requests.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=10)
        response.raise_for_status()
        data = response.json()
        models = data.get("models", [])
        return [m["name"] for m in models if "name" in m]
    except Exception as exc:
        print(f"[ai_generator] Failed to fetch models: {exc}")
        return None
