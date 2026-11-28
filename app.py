from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).with_name(".env"), override=True)
import os, re, json, uuid, hashlib, secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, redirect, url_for, abort, session, Response
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename
import google.generativeai as genai
from database import get_db, init_db, json_param, parse_json_field
from moderation import ModerationError, validate_no_abusive_content
from ppt_utils import compress_upload_for_processing, extract_text_from_file
from quiz_logic import QuizGenerationError, extract_existing_questions, generate_quiz
from storage_utils import (
    StorageError,
    create_signed_upload,
    download_storage_file,
    make_storage_path,
    public_storage_config,
    remove_storage_file,
    storage_enabled,
)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))
IS_VERCEL = os.environ.get("VERCEL") == "1"
UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER", "/tmp/uploads" if IS_VERCEL else "uploads")
COMPRESSED_UPLOAD_TARGET_BYTES = int(os.environ.get("COMPRESSED_UPLOAD_TARGET_BYTES", "3500000"))
DEFAULT_MAX_UPLOAD_BYTES = 50_000_000 if storage_enabled() else (4_000_000 if IS_VERCEL else 25_000_000)
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(DEFAULT_MAX_UPLOAD_BYTES)))
MAX_REQUEST_BYTES = int(os.environ.get("MAX_REQUEST_BYTES", str(MAX_UPLOAD_BYTES + 300_000)))
MAX_TEXT_CHARS = int(os.environ.get("MAX_TEXT_CHARS", "12000"))
EXTRACT_MAX_CHARS = int(os.environ.get("EXTRACT_MAX_CHARS", "60000"))
MIN_BANK_QUESTIONS = 3  # a file with at least this many written MCQs is treated as a question bank
LEARNER_SCREEN_CHANGE_BAN_MINUTES = 30
SCREEN_CHANGE_LIMIT = 3  # secured exams are auto-submitted on this many screen changes
CAPTURE_MAX_BYTES = 600_000        # one full-screen JPEG
CAPTURE_MAX_PER_STUDENT = 12       # per quiz and admission number
CAPTURE_MAX_PER_QUIZ = 3000
# reason -> (short title, plain-language explanation shown to the tutor with each screenshot)
CAPTURE_INFO = {
    'tab-hidden': ('Switched tab or minimised',
                   'The exam tab was hidden: the student switched to another browser tab, minimised the browser or moved to another desktop.'),
    'window-blur': ('Switched to another window or app',
                    'The exam window lost focus: the student clicked or alt-tabbed to another window or app (including a split-screen window), opened a browser extension popup, or changed desktop.'),
    'fullscreen-exit': ('Left full-screen mode',
                        'The student exited full-screen mode (for example by pressing Esc or F11) during the secured exam.'),
    'devtools': ('Developer tools or side panel open',
                 'The browser window was resized while in full screen, which usually means developer tools or a browser side panel (such as an extension or AI assistant) was opened.'),
    'extension': ('Browser extension active',
                  'A browser extension added content to the exam page (for example a writing assistant, translator or injected overlay).'),
    'page-change': ('Exam page changed on screen',
                    'The look of the exam page changed: resizing or zooming the window, translation, dark-mode or another extension altering the page.'),
    'share-stopped': ('Stopped screen sharing',
                      'The student stopped sharing their screen during the secured exam. The image (if any) is the last frame seen before sharing ended.'),
    'screenshot-key': ('Screenshot attempt detected',
                       'The student pressed a screenshot shortcut (Print Screen, Win+Shift+S or Cmd+Shift+3/4/5). The image is the exam screen at that moment.'),
    # older records
    'focus-change': ('Left the exam window', 'The exam window or tab lost focus.'),
    'screenshot': ('Exam page changed on screen', 'The look of the exam page changed.'),
    'limit': ('Final screen (auto-submitted)', 'Last screenshot before the test was submitted automatically.'),
}
CAPTURE_REASONS = set(CAPTURE_INFO)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST_BYTES
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

genai.configure(api_key=os.environ.get("GEMINI_API_KEY"))
explain_model = genai.GenerativeModel(os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"))

def render_problem(title, message, status_code=400, detail=None, action_url="/learner", action_label="Try again"):
    return render_template(
        "error.html",
        title=title,
        message=message,
        detail=detail,
        action_url=action_url,
        action_label=action_label,
    ), status_code

@app.errorhandler(RequestEntityTooLarge)
def handle_large_request(_error):
    return render_problem(
        "PPT/PDF is too large",
        "Please upload a smaller PPT or PDF file.",
        status_code=413,
    )

# ── DATABASE ──
DB_INIT_DONE = False
DB_INIT_ERROR = None


def ensure_db_initialized():
    global DB_INIT_DONE, DB_INIT_ERROR
    if DB_INIT_DONE:
        return True

    try:
        init_db()
        DB_INIT_DONE = True
        DB_INIT_ERROR = None
        return True
    except Exception as exc:
        DB_INIT_ERROR = exc
        return False


def database_problem_response():
    detail = "Check DATABASE_URL in Vercel. Use the Supabase Transaction pooler URL, replace [YOUR-PASSWORD], and redeploy."
    if DB_INIT_ERROR:
        detail = public_error_message(DB_INIT_ERROR, detail)
    return render_problem(
        "Database connection failed",
        "Qslide could not connect to the production database.",
        status_code=500,
        detail=detail,
        action_url="/",
        action_label="Back home",
    )

def database_problem_json():
    detail = "Check DATABASE_URL in Vercel. Use the Supabase Transaction pooler URL, replace [YOUR-PASSWORD], and redeploy."
    if DB_INIT_ERROR:
        detail = public_error_message(DB_INIT_ERROR, detail)
    return jsonify({
        'error': 'Database connection failed: ' + detail
    }), 500

def _parse_iso_datetime(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None

def active_learner_ban_until():
    banned_until = _parse_iso_datetime(session.get('learner_banned_until'))
    if banned_until and banned_until > datetime.now():
        return banned_until
    if banned_until:
        session.pop('learner_banned_until', None)
    return None

def set_learner_screen_change_ban():
    banned_until = datetime.now() + timedelta(minutes=LEARNER_SCREEN_CHANGE_BAN_MINUTES)
    session['learner_banned_until'] = banned_until.isoformat()
    return banned_until

def ban_remaining_minutes(banned_until):
    remaining_seconds = max(0, int((banned_until - datetime.now()).total_seconds()))
    return max(1, (remaining_seconds + 59) // 60)

def learner_ban_message(banned_until):
    return (
        "Screen change detection ended your learner quiz. "
        f"You can use learner mode again in about {ban_remaining_minutes(banned_until)} minutes."
    )

def learner_ban_response(banned_until):
    return render_problem(
        "Learner mode temporarily blocked",
        learner_ban_message(banned_until),
        status_code=403,
        detail=f"Blocked until {banned_until.strftime('%Y-%m-%d %H:%M')}.",
        action_url="/",
        action_label="Back home",
    )

def learner_ban_json(banned_until):
    return jsonify({
        'error': learner_ban_message(banned_until),
        'banned_until': banned_until.isoformat(),
        'remaining_minutes': ban_remaining_minutes(banned_until),
    }), 403

def public_error_message(error, fallback="Something went wrong. Please try again."):
    message = str(error or "").strip()
    if not message:
        return fallback

    secret_values = [
        os.environ.get("GEMINI_API_KEY", ""),
        os.environ.get("SUPABASE_ANON_KEY", ""),
        os.environ.get("SUPABASE_SERVICE_ROLE_KEY", ""),
        os.environ.get("DATABASE_URL", ""),
        app.secret_key,
    ]
    for secret in secret_values:
        secret = str(secret or "").strip()
        if secret and secret in message:
            message = message.replace(secret, "[hidden]")

    return message[:240]

def quiz_is_secure(quiz):
    try:
        return bool(quiz['secure_exam'])
    except (KeyError, IndexError, TypeError):
        return False

ADMISSION_RE = re.compile(r'^[A-Z0-9]{5,20}$')
SECTION_RE = re.compile(r'^\d{1,3}$')

def clean_student_details(data):
    """Validate the details a student enters before the test. Returns (details, error)."""
    name = re.sub(r'\s+', ' ', str(data.get('student_name') or '')).strip()
    admission = re.sub(r'[\s\-]+', '', str(data.get('admission_no') or '')).upper()
    section = re.sub(r'\s+', ' ', str(data.get('section') or '')).strip().upper()
    if not name or len(name) > 100:
        return None, 'Please enter your full name.'
    if not ADMISSION_RE.match(admission):
        return None, 'Please enter a valid admission number (letters and numbers only, e.g. 20SCSE1410231).'
    if not SECTION_RE.match(section) or not 1 <= int(section) <= 100:
        return None, 'Please enter your section as a number from 1 to 100 (e.g. 01).'
    section = str(int(section))
    try:
        validate_no_abusive_content([name, section], "Your details")
    except ModerationError as e:
        return None, str(e)
    return {'student_name': name, 'admission_no': admission, 'section': section}, None

def hash_pin(pin):
    return hashlib.sha256(pin.encode()).hexdigest()

def get_tutor_from_session():
    tutor_id = session.get('tutor_id')
    if not tutor_id:
        return None
    if not ensure_db_initialized():
        return None
    db = get_db()
    tutor = db.execute('SELECT * FROM tutors WHERE id=?', (tutor_id,)).fetchone()
    db.close()
    return tutor

def _as_answer_list(value):
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if value is None:
        return []
    text = str(value).strip()
    return [text] if text else []

def normalize_question(q):
    correct_answers = _as_answer_list(q.get('answers', q.get('answer')))
    try:
        marks = float(q.get('marks', 1))
    except (TypeError, ValueError):
        marks = 1.0
    try:
        negative_marks = float(q.get('negative_marks', 0))
    except (TypeError, ValueError):
        negative_marks = 0.0
    return {
        **q,
        'answers': correct_answers,
        'answer': correct_answers[0] if correct_answers else '',
        'marks': max(0.0, marks),
        'negative_marks': max(0.0, negative_marks),
    }

def calculate_quiz_score(questions, answers):
    score = 0.0
    total = 0.0
    correct_count = 0
    wrong_count = 0

    for i, raw_q in enumerate(questions):
        q = normalize_question(raw_q)
        total += q['marks']
        submitted = _as_answer_list(answers.get(str(i)))
        if not submitted:
            continue

        submitted_set = set(submitted)
        correct_set = set(q['answers'])
        if submitted_set == correct_set:
            score += q['marks']
            correct_count += 1
        else:
            score -= q['negative_marks']
            wrong_count += 1

    return score, total, correct_count, wrong_count

def build_answer_review(questions, answers):
    review = []
    for i, raw_q in enumerate(questions):
        q = normalize_question(raw_q)
        submitted = _as_answer_list(answers.get(str(i)))
        submitted_set = set(submitted)
        correct_set = set(q['answers'])
        is_correct = bool(submitted) and submitted_set == correct_set
        review.append({
            'index': i + 1,
            'question': q.get('question', ''),
            'options': q.get('options', []),
            'selected': submitted,
            'correct_answers': q['answers'],
            'is_correct': is_correct,
            'is_unanswered': not submitted,
            'marks': format_marks(q['marks']),
            'negative_marks': format_marks(q['negative_marks']),
        })
    return review

def format_marks(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return value
    return int(value) if value.is_integer() else round(value, 2)

def safe_int(value, default=0, minimum=0, maximum=999):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, number))

# ══════════════════════════════════════════
#  LANDING PAGE
# ══════════════════════════════════════════
@app.route('/')
def landing():
    return render_template('landing.html')

# ══════════════════════════════════════════
#  LEARNER ROUTES
# ══════════════════════════════════════════
@app.route('/learner')
def learner():
    banned_until = active_learner_ban_until()
    if banned_until:
        return learner_ban_response(banned_until)

    return render_template(
        'index.html',
        max_upload_bytes=MAX_UPLOAD_BYTES,
        storage_config=public_storage_config(),
    )

@app.route('/learner/screen-change-ban', methods=['POST'])
def learner_screen_change_ban():
    banned_until = set_learner_screen_change_ban()
    return jsonify({
        'success': True,
        'banned_until': banned_until.isoformat(),
        'remaining_minutes': ban_remaining_minutes(banned_until),
    })

def _generation_settings(source):
    try:
        num_questions = int(source.get('num_questions', 10))
        num_questions = max(1, min(50, num_questions))
    except (TypeError, ValueError):
        num_questions = 10
    try:
        time_limit = int(source.get('time_limit', 20))
        time_limit = max(1, min(180, time_limit))
    except (TypeError, ValueError):
        time_limit = 20
    return num_questions, time_limit

def _render_quiz_from_file(filepath, num_questions, time_limit, enforce_processing_limit=True):
    if enforce_processing_limit:
        compression = compress_upload_for_processing(filepath, COMPRESSED_UPLOAD_TARGET_BYTES)
        if not compression.under_target:
            return render_problem(
                "PPT/PDF is too large",
                "Please upload a smaller PPT or PDF file.",
                status_code=413,
            )

    text = extract_text_from_file(filepath)
    text = re.sub(r'\s+', ' ', text).strip()
    if not text.strip():
        return render_problem(
            "No readable text found",
            "Please upload a text-based PPTX or PDF file.",
        )
    validate_no_abusive_content(text, "Uploaded file")
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS]
    quiz_raw = generate_quiz(text, num_questions)
    match    = re.search(r'\[.*\]', quiz_raw, re.DOTALL)
    if match:
        quiz_data = json.loads(match.group(0))
        validate_no_abusive_content(quiz_data, "Generated quiz")
        return render_template('quiz.html', quiz_data=quiz_data, time_limit=time_limit)

    return render_problem(
        "Quiz generation failed",
        "The quiz service returned an unexpected response. Please try again with a shorter, clearer file.",
        status_code=502,
    )

@app.route('/storage/sign-upload', methods=['POST'])
def sign_storage_upload():
    banned_until = active_learner_ban_until()
    if banned_until:
        return learner_ban_json(banned_until)

    if not storage_enabled():
        return jsonify({'error': 'Supabase Storage is not configured.'}), 503

    data = request.get_json() or {}
    filename = data.get('filename', '')
    try:
        size = int(data.get('size', 0))
    except (TypeError, ValueError):
        size = 0

    if size > MAX_UPLOAD_BYTES:
        return jsonify({'error': 'PPT/PDF is too large. Please choose a smaller file.'}), 413

    try:
        path = make_storage_path(filename)
        upload = create_signed_upload(path)
        return jsonify(upload)
    except StorageError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        app.logger.exception("Could not create Supabase signed upload URL")
        return jsonify({
            'error': 'Could not prepare the upload: ' + public_error_message(e),
        }), 500

@app.route('/upload/storage', methods=['POST'])
def upload_from_storage():
    banned_until = active_learner_ban_until()
    if banned_until:
        return learner_ban_response(banned_until)

    if not storage_enabled():
        return render_problem(
            "Upload storage is not configured",
            "Supabase Storage environment variables are missing.",
            status_code=503,
        )

    data = request.get_json() or {}
    storage_path = data.get('path', '')
    filename = secure_filename(data.get('filename', '') or os.path.basename(storage_path))
    num_questions, time_limit = _generation_settings(data)

    if not filename.lower().endswith(('.ppt', '.pptx', '.pdf')):
        return render_problem("Unsupported file type", "Please upload a PPTX or PDF file.")

    filepath = os.path.join(app.config['UPLOAD_FOLDER'], f"{uuid.uuid4().hex}_{filename}")
    try:
        file_bytes = download_storage_file(storage_path)
        if hasattr(file_bytes, "content"):
            file_bytes = file_bytes.content
        if isinstance(file_bytes, str):
            file_bytes = file_bytes.encode()

        with open(filepath, "wb") as output:
            output.write(file_bytes)

        return _render_quiz_from_file(
            filepath,
            num_questions,
            time_limit,
            enforce_processing_limit=False,
        )
    except QuizGenerationError as e:
        return render_problem(
            "Quiz generation failed",
            str(e),
            status_code=502,
            action_label="Upload another file",
        )
    except ModerationError as e:
        return render_problem(
            "Content blocked",
            str(e),
            status_code=400,
            action_label="Upload another file",
        )
    except StorageError as e:
        return render_problem("Upload error", str(e))
    except ValueError as e:
        return render_problem("Upload error", str(e))
    except Exception:
        return render_problem(
            "Something went wrong",
            "Please try again with a smaller text-based PPTX or PDF file.",
            status_code=500,
        )
    finally:
        try:
            os.remove(filepath)
        except OSError:
            pass
        remove_storage_file(storage_path)

@app.route('/upload', methods=['POST'])
def upload():
    banned_until = active_learner_ban_until()
    if banned_until:
        return learner_ban_response(banned_until)

    if request.content_length and request.content_length > MAX_REQUEST_BYTES:
        return render_problem(
            "PPT/PDF is too large",
            "Please upload a smaller PPT or PDF file.",
            status_code=413,
        )

    file = request.files.get('file')
    if not file or not file.filename:
        return render_problem("No file uploaded", "Please attach a PPTX or PDF file and try again.")
    num_questions, time_limit = _generation_settings(request.form)

    allowed_ext = ('.ppt', '.pptx', '.pdf')
    if not file.filename.lower().endswith(allowed_ext):
        return render_problem("Unsupported file type", "Please upload a PPTX or PDF file.")

    file.stream.seek(0, os.SEEK_END)
    upload_size = file.stream.tell()
    file.stream.seek(0)
    if upload_size > MAX_UPLOAD_BYTES:
        return render_problem(
            "PPT/PDF is too large",
            "Please upload a smaller PPT or PDF file.",
            status_code=413,
        )

    filename = secure_filename(file.filename)
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], f"{uuid.uuid4().hex}_{filename}")
    file.save(filepath)
    try:
        return _render_quiz_from_file(filepath, num_questions, time_limit)
    except QuizGenerationError as e:
        return render_problem(
            "Quiz generation failed",
            str(e),
            status_code=502,
            action_label="Upload another file",
        )
    except ModerationError as e:
        return render_problem(
            "Content blocked",
            str(e),
            status_code=400,
            action_label="Upload another file",
        )
    except ValueError as e:
        return render_problem("Upload error", str(e))
    except Exception:
        return render_problem(
            "Something went wrong",
            "Please try again with a smaller text-based PPTX or PDF file.",
            status_code=500,
        )
    finally:
        try:
            os.remove(filepath)
        except OSError:
            pass

@app.route('/explain', methods=['POST'])
def explain():
    data     = request.get_json()
    question = data.get('question', '')
    correct  = data.get('correctAns', '')
    your_ans = data.get('yourAns', '')
    try:
        validate_no_abusive_content([question, correct, your_ans], "Explanation request")
    except ModerationError:
        return jsonify({'explanation': 'This explanation could not be generated because the request contains abusive language.'}), 400
    prompt   = f'Question: "{question}"\nCorrect: "{correct}"\nStudent chose: "{your_ans}"\nExplain in 2-3 friendly sentences why the correct answer is right. No bullet points.'
    try:
        response = explain_model.generate_content(prompt)
        return jsonify({'explanation': response.text})
    except:
        return jsonify({'explanation': f'The correct answer is: {correct}'})

# ══════════════════════════════════════════
#  TUTOR AUTH ROUTES
# ══════════════════════════════════════════

# Tutor registration — create account with PIN
@app.route('/tutor/register', methods=['POST'])
def tutor_register():
    data = request.get_json()
    name = data.get('name', '').strip()
    pin  = data.get('pin', '').strip()

    if not name or not pin:
        return jsonify({'error': 'Name and PIN are required'}), 400
    if len(pin) < 4:
        return jsonify({'error': 'PIN must be at least 4 digits'}), 400

    if not ensure_db_initialized():
        return database_problem_json()

    tutor_id   = str(uuid.uuid4())
    pin_hash   = hash_pin(pin)
    created_at = datetime.now().isoformat()

    db = get_db()
    db.execute('INSERT INTO tutors (id,name,pin_hash,created_at) VALUES (?,?,?,?)',
               (tutor_id, name, pin_hash, created_at))
    db.commit()
    db.close()

    session['tutor_id']   = tutor_id
    session['tutor_name'] = name
    return jsonify({'success': True, 'tutor_id': tutor_id, 'name': name})

# Tutor login with PIN
@app.route('/tutor/login', methods=['POST'])
def tutor_login():
    data     = request.get_json()
    tutor_id = data.get('tutor_id', '').strip()
    pin      = data.get('pin', '').strip()

    if not ensure_db_initialized():
        return database_problem_json()

    db    = get_db()
    tutor = db.execute('SELECT * FROM tutors WHERE id=?', (tutor_id,)).fetchone()
    db.close()

    if not tutor:
        return jsonify({'error': 'Tutor ID not found'}), 404
    if tutor['pin_hash'] != hash_pin(pin):
        return jsonify({'error': 'Incorrect PIN'}), 401

    session['tutor_id']   = tutor_id
    session['tutor_name'] = tutor['name']
    return jsonify({'success': True, 'name': tutor['name']})

# Tutor logout
@app.route('/tutor/logout')
def tutor_logout():
    session.clear()
    return redirect('/')

# ══════════════════════════════════════════
#  TUTOR DASHBOARD
# ══════════════════════════════════════════
@app.route('/tutor')
def tutor():
    tutor = get_tutor_from_session()
    return render_template('tutor_dashboard.html', tutor=tutor)

@app.route('/tutor/create')
def tutor_create_page():
    tutor = get_tutor_from_session()
    if not tutor:
        return redirect('/tutor')
    return render_template(
        'tutor_create.html',
        tutor=tutor,
        max_upload_bytes=MAX_UPLOAD_BYTES,
        storage_config=public_storage_config(),
    )

# Create quiz API
@app.route('/tutor/create', methods=['POST'])
def tutor_create():
    tutor = get_tutor_from_session()
    if not tutor:
        if session.get('tutor_id') and DB_INIT_ERROR:
            return database_problem_json()
        return jsonify({'error': 'Not logged in'}), 401

    data         = request.get_json()
    title        = data.get('title', 'Quiz')
    questions    = [normalize_question(q) for q in data.get('questions', [])]
    time_limit   = int(data.get('time_limit', 10))
    validity_hrs = float(data.get('validity_hours', 24))
    secure_exam  = 1 if data.get('secure_exam') in (True, 1, '1', 'true') else 0

    try:
        validate_no_abusive_content({'title': title, 'questions': questions}, "Quiz")
    except ModerationError as e:
        return jsonify({'error': str(e)}), 400

    quiz_id    = str(uuid.uuid4())[:8].upper()
    expires_at = (datetime.now() + timedelta(hours=validity_hrs)).isoformat()
    created_at = datetime.now().isoformat()

    db = get_db()
    db.execute('INSERT INTO quizzes (id,tutor_id,title,questions,time_limit,expires_at,created_at,secure_exam) VALUES (?,?,?,?,?,?,?,?)',
               (quiz_id, tutor['id'], title, json_param(questions),
                time_limit, expires_at, created_at, secure_exam))
    db.commit()
    db.close()

    link = request.host_url + 'quiz/' + quiz_id
    return jsonify({'quiz_id': quiz_id, 'link': link})

# ── Full-screen screenshots sent by secured exams ──
@app.route('/quiz/<quiz_id>/capture', methods=['POST'])
def quiz_capture(quiz_id):
    if not ensure_db_initialized():
        return database_problem_json()

    image = request.files.get('image')
    admission = re.sub(r'[\s\-]+', '', request.form.get('admission_no', '')).upper()
    name = re.sub(r'\s+', ' ', request.form.get('student_name', '')).strip()[:100]
    reason = request.form.get('reason', '')
    if reason not in CAPTURE_REASONS:
        reason = 'window-blur'
    count = safe_int(request.form.get('count'), default=0)
    # Sharing can end before a picture is possible, so that one event may arrive without an image.
    if not name or not ADMISSION_RE.match(admission) or (not image and reason != 'share-stopped'):
        return jsonify({'error': 'Invalid capture.'}), 400

    data = None
    if image:
        data = image.read(CAPTURE_MAX_BYTES + 1)
        if len(data) > CAPTURE_MAX_BYTES or not data.startswith(b'\xff\xd8\xff'):
            return jsonify({'error': 'Invalid image.'}), 400

    db = get_db()
    try:
        quiz = db.execute('SELECT id, secure_exam FROM quizzes WHERE id=?', (quiz_id,)).fetchone()
        if not quiz or not quiz_is_secure(quiz):
            return jsonify({'error': 'Not found.'}), 404
        per_student = db.execute(
            'SELECT COUNT(*) AS n FROM screen_captures WHERE quiz_id=? AND admission_no=?',
            (quiz_id, admission)).fetchone()['n']
        per_quiz = db.execute(
            'SELECT COUNT(*) AS n FROM screen_captures WHERE quiz_id=?', (quiz_id,)).fetchone()['n']
        if per_student >= CAPTURE_MAX_PER_STUDENT or per_quiz >= CAPTURE_MAX_PER_QUIZ:
            return jsonify({'ok': True, 'stored': False})
        db.execute(
            'INSERT INTO screen_captures (quiz_id,admission_no,student_name,reason,detection_count,image,created_at) '
            'VALUES (?,?,?,?,?,?,?)',
            (quiz_id, admission, name, reason, count, data, datetime.now().isoformat(timespec='seconds')))
        db.commit()
    finally:
        db.close()
    return jsonify({'ok': True, 'stored': True})

@app.route('/tutor/capture/<int:capture_id>')
def tutor_capture_image(capture_id):
    tutor = get_tutor_from_session()
    if not tutor:
        abort(401)
    if not ensure_db_initialized():
        abort(503)
    db = get_db()
    row = db.execute(
        'SELECT c.image AS image FROM screen_captures c JOIN quizzes q ON q.id = c.quiz_id '
        'WHERE c.id=? AND q.tutor_id=?', (capture_id, tutor['id'])).fetchone()
    db.close()
    if not row or not row['image']:
        abort(404)
    resp = Response(bytes(row['image']), mimetype='image/jpeg')
    resp.headers['Cache-Control'] = 'private, max-age=3600'
    return resp

# ── AI quiz from an uploaded PDF/PPT (tutor "already created" option) ──
def _clean_generated_questions(quiz_data):
    """Turn raw model output into the editor's question format, dropping malformed items."""
    cleaned = []
    if not isinstance(quiz_data, list):
        return cleaned
    for item in quiz_data:
        if not isinstance(item, dict):
            continue
        question = str(item.get('question', '')).strip()
        options = item.get('options')
        if not question or not isinstance(options, list):
            continue
        options = [str(o).strip() for o in options if str(o).strip()]
        if len(options) != 4 or len(set(options)) != 4:
            continue
        answers = [a for a in _as_answer_list(item.get('answers', item.get('answer'))) if a in options]
        if not answers:
            continue
        cleaned.append({
            'question': question,
            'options': options,
            'answers': answers,
            'answer': answers[0],
            'marks': 1,
            'negative_marks': 0,
        })
    return cleaned

def extract_question_bank(text, requested):
    """If the file already contains written MCQs, return {'total', 'questions'}; otherwise None."""
    raw = extract_existing_questions(text, requested)
    match = re.search(r'\{.*\}', raw or '', re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    try:
        total = int(data.get('total_existing') or 0)
    except (TypeError, ValueError):
        return None
    questions = _clean_generated_questions(data.get('questions') or [])
    if total < MIN_BANK_QUESTIONS or not questions:
        return None
    return {'total': max(total, len(questions)), 'questions': questions[:requested]}

QUESTION_BATCH_SIZE = 10

def _parse_raw_questions(raw):
    match = re.search(r'\[.*\]', raw or '', re.DOTALL)
    if not match:
        return []
    try:
        return _clean_generated_questions(json.loads(match.group(0)))
    except json.JSONDecodeError:
        return []

def _question_key(q):
    return re.sub(r'\W+', ' ', q['question'].lower()).strip()

def _merge_unique(existing, new):
    seen = {_question_key(q) for q in existing}
    for q in new:
        key = _question_key(q)
        if key not in seen:
            seen.add(key)
            existing.append(q)
    return existing

def generate_tutor_questions(text, total):
    """Generate `total` questions in parallel batches, then top up anything missing.

    The text is split between batches so they don't repeat each other; a batch that
    fails or returns unusable questions is covered by the top-up rounds.
    """
    sizes = []
    left = total
    while left > 0:
        sizes.append(min(QUESTION_BATCH_SIZE, left))
        left -= sizes[-1]

    n = len(sizes)
    if len(text) >= 600 * n:
        step = -(-len(text) // n)
        chunks = [text[i * step:(i + 1) * step] for i in range(n)]
    else:
        chunks = [text] * n

    def run(chunk, count, avoid=None):
        return _parse_raw_questions(generate_quiz(chunk, count, avoid))

    questions, first_error = [], None
    with ThreadPoolExecutor(max_workers=min(n, 4)) as pool:
        futures = [pool.submit(run, c, s) for c, s in zip(chunks, sizes)]
        for future in futures:
            try:
                _merge_unique(questions, future.result())
            except QuizGenerationError as e:
                first_error = first_error or e

    for _ in range(2):
        missing = total - len(questions)
        if missing <= 0:
            break
        try:
            extra = run(text, min(50, missing + 2), [q['question'] for q in questions])
        except QuizGenerationError as e:
            first_error = first_error or e
            break
        before = len(questions)
        _merge_unique(questions, extra)
        if len(questions) == before:
            break

    if not questions and first_error:
        raise first_error
    return questions[:total]

@app.route('/tutor/storage/sign-upload', methods=['POST'])
def tutor_sign_storage_upload():
    if not get_tutor_from_session():
        return jsonify({'error': 'Not logged in'}), 401
    if not storage_enabled():
        return jsonify({'error': 'Supabase Storage is not configured.'}), 503

    data = request.get_json(silent=True) or {}
    try:
        size = int(data.get('size', 0))
    except (TypeError, ValueError):
        size = 0
    if size > MAX_UPLOAD_BYTES:
        return jsonify({'error': 'PPT/PDF is too large. Please choose a smaller file.'}), 413

    try:
        return jsonify(create_signed_upload(make_storage_path(data.get('filename', ''))))
    except StorageError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        app.logger.exception("Could not create Supabase signed upload URL")
        return jsonify({'error': 'Could not prepare the upload: ' + public_error_message(e)}), 500

@app.route('/tutor/generate', methods=['POST'])
def tutor_generate():
    tutor = get_tutor_from_session()
    if not tutor:
        if session.get('tutor_id') and DB_INIT_ERROR:
            return database_problem_json()
        return jsonify({'error': 'Please log in again to generate a quiz.'}), 401

    filepath = None
    storage_path = None
    try:
        if request.is_json:
            # File was uploaded straight to Supabase Storage; we only get its path.
            data = request.get_json(silent=True) or {}
            if not storage_enabled():
                return jsonify({'error': 'Supabase Storage is not configured.'}), 503
            storage_path = data.get('path', '')
            filename = secure_filename(data.get('filename', '') or os.path.basename(storage_path))
            num_questions, _ = _generation_settings(data)
            if not filename.lower().endswith(('.ppt', '.pptx', '.pdf')):
                return jsonify({'error': 'Please upload a PPTX or PDF file.'}), 400
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], f"{uuid.uuid4().hex}_{filename}")
            file_bytes = download_storage_file(storage_path)
            if hasattr(file_bytes, "content"):
                file_bytes = file_bytes.content
            if isinstance(file_bytes, str):
                file_bytes = file_bytes.encode()
            with open(filepath, "wb") as output:
                output.write(file_bytes)
            enforce_limit = False
        else:
            file = request.files.get('file')
            if not file or not file.filename:
                return jsonify({'error': 'Please attach a PPTX or PDF file.'}), 400
            num_questions, _ = _generation_settings(request.form)
            if not file.filename.lower().endswith(('.ppt', '.pptx', '.pdf')):
                return jsonify({'error': 'Please upload a PPTX or PDF file.'}), 400
            file.stream.seek(0, os.SEEK_END)
            if file.stream.tell() > MAX_UPLOAD_BYTES:
                return jsonify({'error': 'PPT/PDF is too large. Please choose a smaller file.'}), 413
            file.stream.seek(0)
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], f"{uuid.uuid4().hex}_{secure_filename(file.filename)}")
            file.save(filepath)
            enforce_limit = True

        if enforce_limit:
            compression = compress_upload_for_processing(filepath, COMPRESSED_UPLOAD_TARGET_BYTES)
            if not compression.under_target:
                return jsonify({'error': 'PPT/PDF is too large. Please upload a smaller file.'}), 413

        text = re.sub(r'\s+', ' ', extract_text_from_file(filepath)).strip()
        if not text:
            return jsonify({'error': 'No readable text found. Please upload a text-based PPTX or PDF.'}), 400
        text_too_long = len(text) > EXTRACT_MAX_CHARS
        text = text[:EXTRACT_MAX_CHARS]
        validate_no_abusive_content(text, "Uploaded file")

        # 1) Is this already a set of written questions? Then import them (and report how many exist).
        bank = extract_question_bank(text, num_questions)
        if bank:
            validate_no_abusive_content(bank['questions'], "Imported quiz")
            app.logger.info("Tutor quiz import: requested=%s in_file=%s imported=%s",
                            num_questions, bank['total'], len(bank['questions']))
            return jsonify({
                'mode': 'existing',
                'questions': bank['questions'],
                'requested': num_questions,
                'total_existing': bank['total'],
                'truncated': text_too_long,
            })

        # 2) Otherwise it is study material: write new questions from it.
        truncated = len(text) > MAX_TEXT_CHARS
        text = text[:MAX_TEXT_CHARS]
        questions = generate_tutor_questions(text, num_questions)
        app.logger.info("Tutor quiz generation: requested=%s got=%s", num_questions, len(questions))
        if not questions:
            return jsonify({'error': 'The AI returned an unexpected response. Please try again.'}), 502
        validate_no_abusive_content(questions, "Generated quiz")

        return jsonify({'mode': 'generated', 'questions': questions, 'requested': num_questions, 'truncated': truncated})
    except QuizGenerationError as e:
        return jsonify({'error': str(e)}), 502
    except ModerationError as e:
        return jsonify({'error': str(e)}), 400
    except (StorageError, ValueError) as e:
        return jsonify({'error': str(e)}), 400
    except Exception:
        app.logger.exception("Tutor quiz generation failed")
        return jsonify({'error': 'Something went wrong. Please try again with a smaller text-based PPTX or PDF.'}), 500
    finally:
        if filepath:
            try:
                os.remove(filepath)
            except OSError:
                pass
        if storage_path:
            remove_storage_file(storage_path)

# Tutor's all quizzes
@app.route('/tutor/dashboard')
def tutor_dashboard():
    tutor = get_tutor_from_session()
    if not tutor:
        if session.get('tutor_id') and DB_INIT_ERROR:
            return database_problem_response()
        return redirect('/tutor')

    db      = get_db()
    quizzes = db.execute(
        'SELECT * FROM quizzes WHERE tutor_id=? ORDER BY created_at DESC',
        (tutor['id'],)
    ).fetchall()
    db.close()

    quiz_list = []
    for q in quizzes:
        expires_at = datetime.fromisoformat(q['expires_at'])
        is_expired = datetime.now() > expires_at
        db2 = get_db()
        sub_count = db2.execute(
            'SELECT COUNT(*) as c FROM submissions WHERE quiz_id=?', (q['id'],)
        ).fetchone()['c']
        db2.close()
        quiz_list.append({
            'id':         q['id'],
            'title':      q['title'],
            'time_limit': q['time_limit'],
            'created_at': q['created_at'][:10],
            'expires_at': q['expires_at'][:16].replace('T', ' '),
            'is_expired': is_expired,
            'sub_count':  sub_count,
            'link':       request.host_url + 'quiz/' + q['id']
        })

    return render_template('tutor_dashboard.html',
                           tutor=dict(tutor), quizzes=quiz_list)

# ══════════════════════════════════════════
#  STUDENT QUIZ ROUTES
# ══════════════════════════════════════════
@app.route('/quiz/<quiz_id>')
def take_quiz(quiz_id):
    if not ensure_db_initialized():
        return database_problem_response()

    db   = get_db()
    quiz = db.execute('SELECT * FROM quizzes WHERE id=?', (quiz_id,)).fetchone()
    tutor = None
    if quiz:
        tutor = db.execute('SELECT name FROM tutors WHERE id=?', (quiz['tutor_id'],)).fetchone()
    db.close()

    if not quiz:
        abort(404)

    expires_at = datetime.fromisoformat(quiz['expires_at'])
    if datetime.now() > expires_at:
        return render_template('expired.html',
                               title=quiz['title'],
                               tutor=tutor['name'] if tutor else 'Tutor')

    questions = parse_json_field(quiz['questions'], [])
    return render_template('student_quiz.html',
        quiz_id    = quiz_id,
        title      = quiz['title'],
        tutor_name = tutor['name'] if tutor else 'Tutor',
        questions  = questions,
        time_limit = quiz['time_limit'],
        secure_exam = quiz_is_secure(quiz)
    )

ATTEMPT_USED_MESSAGE = 'This admission number has already submitted this test. Only one attempt is allowed.'

def admission_has_submitted(db, quiz_id, admission_no):
    return db.execute(
        'SELECT 1 AS x FROM submissions WHERE quiz_id=? AND UPPER(admission_no)=? LIMIT 1',
        (quiz_id, admission_no)).fetchone() is not None

@app.route('/quiz/<quiz_id>/check-attempt', methods=['POST'])
def check_attempt(quiz_id):
    if not ensure_db_initialized():
        return database_problem_json()
    admission = re.sub(r'[\s\-]+', '', (request.get_json(silent=True) or {}).get('admission_no', '')).upper()
    if not ADMISSION_RE.match(admission):
        return jsonify({'error': 'Invalid admission number.'}), 400
    db = get_db()
    try:
        used = admission_has_submitted(db, quiz_id, admission)
    finally:
        db.close()
    if used:
        return jsonify({'allowed': False, 'error': ATTEMPT_USED_MESSAGE}), 409
    return jsonify({'allowed': True})

@app.route('/quiz/<quiz_id>/submit', methods=['POST'])
def submit_quiz(quiz_id):
    data         = request.get_json()
    details, details_error = clean_student_details(data)
    if details_error:
        return jsonify({'error': details_error}), 400
    student_name = details['student_name']
    answers      = data.get('answers', {})
    reported_screen_changes = safe_int(data.get('screen_change_count'), default=0)
    reported_auto_submit = bool(data.get('auto_submitted'))

    if not ensure_db_initialized():
        return database_problem_json()

    db   = get_db()
    quiz = db.execute('SELECT * FROM quizzes WHERE id=?', (quiz_id,)).fetchone()
    if not quiz:
        db.close()
        return jsonify({'error': 'Quiz not found'}), 404

    if admission_has_submitted(db, quiz_id, details['admission_no']):
        db.close()
        return jsonify({'error': ATTEMPT_USED_MESSAGE}), 409

    # Screen-change rules only apply to secured exams; unsecured quizzes record none.
    if quiz_is_secure(quiz):
        screen_change_count = reported_screen_changes
        auto_submitted = reported_auto_submit and screen_change_count >= SCREEN_CHANGE_LIMIT
        if auto_submitted:
            proctoring_status = 'auto_submitted'
        else:
            proctoring_status = 'red_tag' if screen_change_count > 2 else 'clear'
    else:
        screen_change_count, auto_submitted, proctoring_status = 0, False, 'clear'

    questions = [normalize_question(q) for q in parse_json_field(quiz['questions'], [])]
    score, total, correct_count, wrong_count = calculate_quiz_score(questions, answers)
    review = build_answer_review(questions, answers)

    db.execute(
        (
            'INSERT INTO submissions '
            '(quiz_id,student_name,answers,score,total,submitted_at,screen_change_count,proctoring_status,admission_no,section) '
            'VALUES (?,?,?,?,?,?,?,?,?,?)'
        ),
        (
            quiz_id,
            student_name,
            json_param(answers),
            score,
            total,
            datetime.now().isoformat(),
            screen_change_count,
            proctoring_status,
            details['admission_no'],
            details['section'],
        )
    )
    db.commit()
    db.close()
    return jsonify({
        'score': format_marks(score),
        'total': format_marks(total),
        'correct': correct_count,
        'wrong': wrong_count,
        'questions': len(questions),
        'review': review,
        'screen_change_count': screen_change_count,
        'proctoring_status': proctoring_status,
        'auto_submitted': auto_submitted,
    })

# Tutor results — protected by session
@app.route('/tutor/results/<quiz_id>')
def tutor_results(quiz_id):
    tutor = get_tutor_from_session()
    if not tutor:
        if session.get('tutor_id') and DB_INIT_ERROR:
            return database_problem_response()
        return redirect('/tutor?next=/tutor/results/' + quiz_id)

    if not ensure_db_initialized():
        return database_problem_response()

    db   = get_db()
    quiz = db.execute('SELECT * FROM quizzes WHERE id=? AND tutor_id=?',
                      (quiz_id, tutor['id'])).fetchone()
    if not quiz:
        db.close()
        abort(403)  # Forbidden — not their quiz

    questions  = [normalize_question(q) for q in parse_json_field(quiz['questions'], [])]
    subs = db.execute(
        'SELECT * FROM submissions WHERE quiz_id=? ORDER BY submitted_at DESC', (quiz_id,)
    ).fetchall()
    cap_rows = db.execute(
        'SELECT id, admission_no, student_name, reason, detection_count, created_at, '
        'CASE WHEN image IS NULL THEN 0 ELSE 1 END AS has_image '
        'FROM screen_captures WHERE quiz_id=? ORDER BY id', (quiz_id,)).fetchall()
    db.close()

    captures_by_admission = {}
    for c in cap_rows:
        title, why = CAPTURE_INFO.get(c['reason'], ('Screen change', 'A screen change was detected.'))
        captures_by_admission.setdefault(c['admission_no'], []).append({
            'id': c['id'],
            'label': title,
            'why': why,
            'has_image': bool(c['has_image']),
            'final': safe_int(c['detection_count'], default=0) >= SCREEN_CHANGE_LIMIT,
            'time': (c['created_at'] or '')[11:19],
            'student_name': c['student_name'],
        })

    submissions = []
    for s in subs:
        try:
            submitted_answers = parse_json_field(s['answers'], {})
        except (TypeError, json.JSONDecodeError):
            submitted_answers = {}
        review = build_answer_review(questions, submitted_answers)
        raw_score = float(s['score'] or 0)
        raw_total = float(s['total'] or 0)
        screen_change_count = safe_int(s['screen_change_count'], default=0)
        proctoring_status = s['proctoring_status'] or ('red_tag' if screen_change_count > 2 else 'clear')
        submissions.append({
            'student_name': s['student_name'],
            'admission_no': s['admission_no'] or '',
            'section':      s['section'] or '',
            'score':        format_marks(raw_score),
            'total':        format_marks(raw_total),
            'pct':          round((raw_score/raw_total)*100) if raw_total else 0,
            'bar_pct':      max(0, min(100, round((raw_score/raw_total)*100))) if raw_total else 0,
            'submitted_at': s['submitted_at'][:16].replace('T', ' '),
            'captures':     captures_by_admission.get(s['admission_no'] or '', []),
            'review':       review,
            'wrong_items':  [item for item in review if not item['is_correct']],
            'screen_change_count': screen_change_count,
            'proctoring_status': proctoring_status,
            'is_red_tag': proctoring_status in ('red_tag', 'auto_submitted'),
            'is_auto_submitted': proctoring_status == 'auto_submitted',
        })

    # Screenshots from students who started a secured exam but have not submitted.
    submitted_admissions = {s['admission_no'] for s in submissions}
    unsubmitted_captures = [
        {'admission_no': adm, 'student_name': caps[0]['student_name'], 'captures': caps}
        for adm, caps in captures_by_admission.items() if adm not in submitted_admissions
    ]

    # Names in ascending A-Z order (case-insensitive); admission number breaks ties.
    submissions.sort(key=lambda s: (s['student_name'].casefold(), s['admission_no']))
    # Sections for the filter dropdown, in natural order (2 before 10, A before B).
    natural = lambda text: [int(p) if p.isdigit() else p.casefold() for p in re.split(r'(\d+)', text)]
    sections = sorted({s['section'] for s in submissions if s['section']}, key=natural)
    has_no_section = any(not s['section'] for s in submissions)

    expires_at = datetime.fromisoformat(quiz['expires_at'])
    is_expired = datetime.now() > expires_at

    return render_template('tutor_results.html',
        quiz       = dict(quiz),
        tutor      = dict(tutor),
        questions  = questions,
        submissions= submissions,
        unsubmitted_captures = unsubmitted_captures,
        sections   = sections,
        has_no_section = has_no_section,
        red_tag_count = len([s for s in submissions if s['is_red_tag']]),
        is_expired = is_expired,
        quiz_link  = request.host_url + 'quiz/' + quiz_id
    )

if __name__ == '__main__':
    app.run(debug=os.environ.get('FLASK_DEBUG', 'true').lower() == 'true')
