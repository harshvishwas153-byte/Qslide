from dotenv import load_dotenv
load_dotenv()
import os, re, json, uuid, hashlib, secrets
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, redirect, url_for, abort, session
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename
import google.generativeai as genai
from database import get_db, init_db, json_param, parse_json_field
from moderation import ModerationError, validate_no_abusive_content
from ppt_utils import compress_upload_for_processing, extract_text_from_file
from quiz_logic import QuizGenerationError, generate_quiz
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
    detail = None
    if DB_INIT_ERROR:
        detail = (
            "Check DATABASE_URL in Vercel. Use the Supabase Transaction pooler URL, "
            "replace [YOUR-PASSWORD], and redeploy."
        )
    return render_problem(
        "Database connection failed",
        "Qslide could not connect to the production database.",
        status_code=500,
        detail=detail,
        action_url="/",
        action_label="Back home",
    )

def database_problem_json():
    return jsonify({
        'error': (
            'Database connection failed. Check DATABASE_URL in Vercel, use the '
            'Supabase Transaction pooler URL, replace [YOUR-PASSWORD], and redeploy.'
        )
    }), 500

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
    return render_template(
        'index.html',
        max_upload_bytes=MAX_UPLOAD_BYTES,
        storage_config=public_storage_config(),
    )

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
    except Exception:
        return jsonify({'error': 'Could not prepare the upload. Please try again.'}), 500

@app.route('/upload/storage', methods=['POST'])
def upload_from_storage():
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
    return render_template('tutor_create.html', tutor=tutor)

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

    try:
        validate_no_abusive_content({'title': title, 'questions': questions}, "Quiz")
    except ModerationError as e:
        return jsonify({'error': str(e)}), 400

    quiz_id    = str(uuid.uuid4())[:8].upper()
    expires_at = (datetime.now() + timedelta(hours=validity_hrs)).isoformat()
    created_at = datetime.now().isoformat()

    db = get_db()
    db.execute('INSERT INTO quizzes (id,tutor_id,title,questions,time_limit,expires_at,created_at) VALUES (?,?,?,?,?,?,?)',
               (quiz_id, tutor['id'], title, json_param(questions),
                time_limit, expires_at, created_at))
    db.commit()
    db.close()

    link = request.host_url + 'quiz/' + quiz_id
    return jsonify({'quiz_id': quiz_id, 'link': link})

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
        time_limit = quiz['time_limit']
    )

@app.route('/quiz/<quiz_id>/submit', methods=['POST'])
def submit_quiz(quiz_id):
    data         = request.get_json()
    student_name = data.get('student_name', 'Anonymous')
    answers      = data.get('answers', {})

    if not ensure_db_initialized():
        return database_problem_json()

    db   = get_db()
    quiz = db.execute('SELECT * FROM quizzes WHERE id=?', (quiz_id,)).fetchone()
    if not quiz:
        db.close()
        return jsonify({'error': 'Quiz not found'}), 404

    questions = [normalize_question(q) for q in parse_json_field(quiz['questions'], [])]
    score, total, correct_count, wrong_count = calculate_quiz_score(questions, answers)
    review = build_answer_review(questions, answers)

    db.execute(
        'INSERT INTO submissions (quiz_id,student_name,answers,score,total,submitted_at) VALUES (?,?,?,?,?,?)',
        (quiz_id, student_name, json_param(answers), score, total, datetime.now().isoformat())
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
    db.close()

    submissions = []
    for s in subs:
        try:
            submitted_answers = parse_json_field(s['answers'], {})
        except (TypeError, json.JSONDecodeError):
            submitted_answers = {}
        review = build_answer_review(questions, submitted_answers)
        raw_score = float(s['score'] or 0)
        raw_total = float(s['total'] or 0)
        submissions.append({
            'student_name': s['student_name'],
            'score':        format_marks(raw_score),
            'total':        format_marks(raw_total),
            'pct':          round((raw_score/raw_total)*100) if raw_total else 0,
            'bar_pct':      max(0, min(100, round((raw_score/raw_total)*100))) if raw_total else 0,
            'submitted_at': s['submitted_at'][:16].replace('T', ' '),
            'review':       review,
            'wrong_items':  [item for item in review if not item['is_correct']],
        })

    expires_at = datetime.fromisoformat(quiz['expires_at'])
    is_expired = datetime.now() > expires_at

    return render_template('tutor_results.html',
        quiz       = dict(quiz),
        tutor      = dict(tutor),
        questions  = questions,
        submissions= submissions,
        is_expired = is_expired,
        quiz_link  = request.host_url + 'quiz/' + quiz_id
    )

if __name__ == '__main__':
    app.run(debug=os.environ.get('FLASK_DEBUG', 'true').lower() == 'true')
