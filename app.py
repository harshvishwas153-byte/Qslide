from dotenv import load_dotenv
load_dotenv()
import os, re, json, uuid, sqlite3, hashlib, secrets
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, redirect, url_for, abort, session
import google.generativeai as genai
from ppt_utils import extract_text_from_file
from quiz_logic import generate_quiz

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))
DB_PATH = os.environ.get("DATABASE_PATH", "qslide.db")
UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER", "uploads")
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
db_dir = os.path.dirname(DB_PATH)
if db_dir:
    os.makedirs(db_dir, exist_ok=True)
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
db_dir = os.path.dirname(DB_PATH)
if db_dir:
    os.makedirs(db_dir, exist_ok=True)

genai.configure(api_key=os.environ.get("GEMINI_API_KEY"))
explain_model = genai.GenerativeModel(os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"))

# ── DATABASE ──
def get_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db

def init_db():
    db = get_db()
    db.executescript('''
        CREATE TABLE IF NOT EXISTS tutors (
            id          TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            pin_hash    TEXT NOT NULL,
            created_at  TEXT
        );
        CREATE TABLE IF NOT EXISTS quizzes (
            id          TEXT PRIMARY KEY,
            tutor_id    TEXT NOT NULL,
            title       TEXT,
            questions   TEXT,
            time_limit  INTEGER,
            expires_at  TEXT,
            created_at  TEXT,
            FOREIGN KEY (tutor_id) REFERENCES tutors(id)
        );
        CREATE TABLE IF NOT EXISTS submissions (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            quiz_id      TEXT,
            student_name TEXT,
            answers      TEXT,
            score        INTEGER,
            total        INTEGER,
            submitted_at TEXT
        );
    ''')
    db.commit()
    db.close()

init_db()

def hash_pin(pin):
    return hashlib.sha256(pin.encode()).hexdigest()

def get_tutor_from_session():
    tutor_id = session.get('tutor_id')
    if not tutor_id:
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
    return render_template('index.html')

@app.route('/upload', methods=['POST'])
def upload():
    file = request.files.get('file')
    if not file:
        return "No file uploaded."
    try:
        num_questions = int(request.form.get('num_questions', 10))
        num_questions = max(1, min(50, num_questions))
    except:
        num_questions = 10
    try:
        time_limit = int(request.form.get('time_limit', 20))
        time_limit = max(1, min(180, time_limit))
    except:
        time_limit = 20

    allowed_ext = ('.ppt', '.pptx', '.pdf')
    if not file.filename.lower().endswith(allowed_ext):
        return "<h3>Unsupported file type</h3><p>Please upload a PPT, PPTX, or PDF file.</p>"

    filepath = os.path.join(app.config['UPLOAD_FOLDER'], file.filename)
    file.save(filepath)
    try:
        text     = extract_text_from_file(filepath)
        if not text.strip():
            return "<h3>No readable text found</h3><p>Please upload a text-based PPT, PPTX, or PDF file.</p>"
        quiz_raw = generate_quiz(text, num_questions)
        match    = re.search(r'\[.*\]', quiz_raw, re.DOTALL)
        if match:
            quiz_data = json.loads(match.group(0))
            return render_template('quiz.html', quiz_data=quiz_data, time_limit=time_limit)
        else:
            return f"<h3>Format Error</h3><pre>{quiz_raw}</pre>"
    except Exception as e:
        return f"<h3>Error:</h3><p>{str(e)}</p>"

@app.route('/explain', methods=['POST'])
def explain():
    data     = request.get_json()
    question = data.get('question', '')
    correct  = data.get('correctAns', '')
    your_ans = data.get('yourAns', '')
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

    tutor_id   = str(uuid.uuid4())
    pin_hash   = hash_pin(pin)
    created_at = datetime.now().isoformat()

    db = get_db()
    db.execute('INSERT INTO tutors VALUES (?,?,?,?)',
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
        return jsonify({'error': 'Not logged in'}), 401

    data         = request.get_json()
    title        = data.get('title', 'Quiz')
    questions    = [normalize_question(q) for q in data.get('questions', [])]
    time_limit   = int(data.get('time_limit', 10))
    validity_hrs = float(data.get('validity_hours', 24))

    quiz_id    = str(uuid.uuid4())[:8].upper()
    expires_at = (datetime.now() + timedelta(hours=validity_hrs)).isoformat()
    created_at = datetime.now().isoformat()

    db = get_db()
    db.execute('INSERT INTO quizzes VALUES (?,?,?,?,?,?,?)',
               (quiz_id, tutor['id'], title, json.dumps(questions),
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

    questions = json.loads(quiz['questions'])
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

    db   = get_db()
    quiz = db.execute('SELECT * FROM quizzes WHERE id=?', (quiz_id,)).fetchone()
    if not quiz:
        db.close()
        return jsonify({'error': 'Quiz not found'}), 404

    questions = [normalize_question(q) for q in json.loads(quiz['questions'])]
    score, total, correct_count, wrong_count = calculate_quiz_score(questions, answers)
    review = build_answer_review(questions, answers)

    db.execute(
        'INSERT INTO submissions (quiz_id,student_name,answers,score,total,submitted_at) VALUES (?,?,?,?,?,?)',
        (quiz_id, student_name, json.dumps(answers), score, total, datetime.now().isoformat())
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
        return redirect('/tutor?next=/tutor/results/' + quiz_id)

    db   = get_db()
    quiz = db.execute('SELECT * FROM quizzes WHERE id=? AND tutor_id=?',
                      (quiz_id, tutor['id'])).fetchone()
    if not quiz:
        db.close()
        abort(403)  # Forbidden — not their quiz

    questions  = [normalize_question(q) for q in json.loads(quiz['questions'])]
    subs = db.execute(
        'SELECT * FROM submissions WHERE quiz_id=? ORDER BY submitted_at DESC', (quiz_id,)
    ).fetchall()
    db.close()

    submissions = []
    for s in subs:
        try:
            submitted_answers = json.loads(s['answers'] or '{}')
        except (TypeError, json.JSONDecodeError):
            submitted_answers = {}
        review = build_answer_review(questions, submitted_answers)
        submissions.append({
            'student_name': s['student_name'],
            'score':        format_marks(s['score']),
            'total':        format_marks(s['total']),
            'pct':          round((s['score']/s['total'])*100) if s['total'] else 0,
            'bar_pct':      max(0, min(100, round((s['score']/s['total'])*100))) if s['total'] else 0,
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
