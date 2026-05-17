import os
import google.generativeai as genai

genai.configure(api_key=os.environ.get("GEMINI_API_KEY"))
model = genai.GenerativeModel(os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"))

def generate_quiz(text, num_questions=10):   # ✅ accepts num_questions
    prompt = f"""
You are a quiz generator. Create exactly {num_questions} multiple choice questions from the text below.
 
Return ONLY a raw JSON array. No explanation, no markdown, no code blocks.
 
The format must be exactly:
[
  {{
    "question": "Question text here?",
    "options": ["Option A", "Option B", "Option C", "Option D"],
    "answer": "Option A"
  }}
]
 
IMPORTANT RULES:
- Generate exactly {num_questions} questions.
- The "answer" value must be the EXACT full text of one of the options.
- Return only the JSON array, nothing else.
 
Text to use:
{text}
"""
    try:
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        return f"API Error: {str(e)}"
 
