from flask import Flask, redirect, render_template, request, url_for, session, jsonify, flash
from werkzeug.utils import secure_filename
import requests
import re
import difflib
from db import connect_db
import time
import os
from datetime import datetime
import io
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from flask import send_file
import bcrypt
import json
import math
import uuid
from otp import send_otp_sms
from profanity import check_profanity

app =Flask(__name__)
app.secret_key = 'actbayan_secret_key_2026'



def hash_password_bcrypt(password: str) -> str:
    if password is None:
        password = ""
    pw_bytes = password.encode('utf-8')
    hashed = bcrypt.hashpw(pw_bytes, bcrypt.gensalt())
    return hashed.decode('utf-8')


def check_password_bcrypt(stored_hash, password: str) -> bool:
    if stored_hash is None or password is None:
        return False

    # stored_hash may be stored as TEXT/VARCHAR (string), or bytes (legacy)
    if isinstance(stored_hash, (bytes, bytearray)):
        hash_bytes = bytes(stored_hash)
    else:
        hash_bytes = str(stored_hash).encode('utf-8')

    return bcrypt.checkpw(password.encode('utf-8'), hash_bytes)


# ===== OPENROUTER AI (duplicate-block security) =====
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "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")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "apodex/apodex-1.1-mini:free")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


def openrouter_chat(messages, temperature=0.4, max_tokens=800, timeout=60):
    """Single helper for all OpenRouter chat calls. Returns content string or None."""
    if not OPENROUTER_API_KEY:
        print("[OpenRouter] Missing OPENROUTER_API_KEY env var - using fallback.")
        return None
    try:
        resp = requests.post(
            OPENROUTER_URL,
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
                "HTTP-Referer": "http://localhost:5000",
                "X-Title": "ActBayan",
            },
            json={
                "model": OPENROUTER_MODEL,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            },
            timeout=timeout,
        )
        if resp.status_code != 200:
            print(f"[OpenRouter] HTTP {resp.status_code}: {resp.text[:500]}")
            return None
        data = resp.json()
        return (data["choices"][0]["message"]["content"] or "").strip()
    except Exception as e:
        print(f"[OpenRouter] chat error: {e}")
        return None


# ===== OPENROUTER DUPLICATE DETECTION HELPERS (copied from old_app.py) =====
def llm_judge_duplicates(new_text, candidates, timeout=45):
    """Ask OpenRouter to score each candidate 0-1. Returns {report_id: score}."""
    if not candidates or not OPENROUTER_API_KEY:
        return {}
    lines = []
    for c in candidates:
        txt = c.get('_text', '')[:400].replace(chr(10), ' ')
        lines.append(f"- id={c['report_id']} | cat={c.get('category','')} | loc={c.get('locname','')} | text={txt}")
    prompt = (
        "You are a duplicate-report detector for a barangay concern system.\n"
        f"NEW REPORT: {new_text[:600]}\n\n"
        "CANDIDATES:\n" + "\n".join(lines) + "\n\n"
        "Score each candidate 0.0 to 1.0 for describing the SAME underlying issue "
        "(same problem + same location/area = high; different problem = low).\n"
        'Reply ONLY with a valid JSON array of objects. Example:\n'
        '[{"id": 1, "score": 0.85}]'
    )
    content = openrouter_chat(
        [{"role": "user", "content": prompt}],
        temperature=0.1, max_tokens=800, timeout=timeout,
    )
    if not content:
        return {}
    try:
        m = re.search(r'\[.*\]', content, re.DOTALL)
        raw_json = m.group(0) if m else content
        arr = json.loads(raw_json)
        out = {}
        for item in arr:
            if not isinstance(item, dict):
                continue
            cand_id = item.get('id') or item.get('report_id') or item.get('candidate_id')
            score = item.get('score') if item.get('score') is not None else item.get('similarity')
            if cand_id is not None and score is not None:
                try:
                    out[int(cand_id)] = max(0.0, min(1.0, float(score)))
                except (TypeError, ValueError):
                    continue
        return out
    except Exception as e:
        print(f"[OpenRouter] Duplicate parse error: {e} | Raw output: {content[:300]}")
        return {}


# ===== DUPLICATE DETECTION HELPERS (OpenRouter judge + offline fallback) =====
def haversine_distance(lat1, lng1, lat2, lng2):
    """Compute distance in meters between two GPS coordinates"""
    R = 6371000  # Earth radius in meters
    phi1 = math.radians(float(lat1))
    phi2 = math.radians(float(lat2))
    delta_phi = math.radians(float(lat2) - float(lat1))
    delta_lambda = math.radians(float(lng2) - float(lng1))
    a = math.sin(delta_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c

def text_similarity(text1, text2):
    """Offline fallback: cheap word-overlap score 0-1 (used when OpenRouter is unreachable)."""
    if not text1 or not text2:
        return 0.0
    w1 = set(re.findall(r'[a-z0-9]+', text1.lower()))
    w2 = set(re.findall(r'[a-z0-9]+', text2.lower()))
    if not w1 or not w2:
        return 0.0
    return len(w1 & w2) / max(len(w1), len(w2))

def coordinate_proximity(lat1, lng1, lat2, lng2, threshold_meters=100):
    """Check if two coordinates are within threshold meters of each other"""
    try:
        lat1_f = float(lat1)
        lng1_f = float(lng1)
        lat2_f = float(lat2)
        lng2_f = float(lng2)
        if lat1_f == 0 and lng1_f == 0:
            return False, float('inf')
        if lat2_f == 0 and lng2_f == 0:
            return False, float('inf')
        distance = haversine_distance(lat1_f, lng1_f, lat2_f, lng2_f)
        return distance <= threshold_meters, distance
    except (ValueError, TypeError):
        return False, float('inf')


# ===== OFFLINE ANALYTICS (5-AI dictionary, website-model only) =====
from analytics_templates import render as _ai_render

_ANALYTICS_CACHE_V2 = {
    'time': 0,
    'monthly_summary': None,
    'sample_summary': None,
    'status_summary': None,
    'location_summary': None,
    'category_summary': None,
    'executive_summary': None,
    'is_generating': False
}

def _analytics_seed(monthly_labels, monthly_counts, status_counts, top_locations, category_data):
    """Seed so Regenerate cycles the 5 AI dictionary versions."""
    try:
        return "|".join([
            ",".join(str(x) for x in (monthly_labels or [])),
            ",".join(str(x) for x in (monthly_counts or [])),
            str(status_counts or {}),
            str([(l.get("locname"), l.get("count")) for l in (top_locations or [])]),
            str([(c.get("name"), c.get("count")) for c in (category_data or [])]),
            str(_ANALYTICS_CACHE_V2.get("regen", 0)),
        ])
    except Exception:
        return str(_ANALYTICS_CACHE_V2.get("regen", 0))


def generate_monthly_analytics_summary(monthly_labels, monthly_counts, monthly_resolved, sample_report=None, status_counts=None, top_locations=None, category_data=None):
    """Offline website-model: 5-AI dictionary, no Ollama/network/threads. Same 5-tuple return."""
    global _ANALYTICS_CACHE_V2
    seed = _analytics_seed(monthly_labels, monthly_counts, status_counts, top_locations, category_data)
    s_counts = status_counts or {}
    total = int(s_counts.get("total", 0) or 0)
    resolved = int(s_counts.get("resolved", 0) or 0)
    pending = int(s_counts.get("pending", 0) or 0)
    inprog = int(s_counts.get("in_progress", 0) or 0)
    res_pct = round((resolved / total) * 100) if total else 0
    next_target = min(100, res_pct + 10 if res_pct else 50)
    pipeline_word = "healthy and moving" if res_pct >= 50 else ("steady" if res_pct >= 25 else "building momentum")
    months = len(monthly_labels or [])
    total_monthly = sum(monthly_counts or [])
    avg_monthly = round(total_monthly / months, 1) if months else 0
    peak_count = max(monthly_counts) if monthly_counts else 0
    peak_label = (monthly_labels or ["N/A"])[list(monthly_counts or [0]).index(peak_count)] if monthly_counts else "N/A"
    latest_label = (monthly_labels or ["N/A"])[-1] if monthly_labels else "N/A"
    latest_count = (monthly_counts or [0])[-1] if monthly_counts else 0
    latest_resolved = (monthly_resolved or [0])[-1] if monthly_resolved else 0
    trend_word = "stable"
    try:
        if months >= 2 and (monthly_counts or []):
            trend_word = "rising" if monthly_counts[-1] > monthly_counts[0] else ("easing" if monthly_counts[-1] < monthly_counts[0] else "stable")
    except Exception:
        pass
    top_loc = (top_locations or [{}])[0].get("locname", "N/A") if top_locations else "N/A"
    top_loc_count = (top_locations or [{}])[0].get("count", 0) if top_locations else 0
    second_loc = (top_locations or [{}, {}])[1].get("locname", "N/A") if len(top_locations or []) > 1 else "N/A"
    second_count = (top_locations or [{}, {}])[1].get("count", 0) if len(top_locations or []) > 1 else 0
    third_loc = (top_locations or [{}, {}, {}])[2].get("locname", "N/A") if len(top_locations or []) > 2 else "N/A"
    third_count = (top_locations or [{}, {}, {}])[2].get("count", 0) if len(top_locations or []) > 2 else 0
    top_cat = (category_data or [{}])[0].get("name", "N/A") if category_data else "N/A"
    top_cat_count = (category_data or [{}])[0].get("count", 0) if category_data else 0
    second_cat = (category_data or [{}, {}])[1].get("name", "N/A") if len(category_data or []) > 1 else "N/A"
    second_count_c = (category_data or [{}, {}])[1].get("count", 0) if len(category_data or []) > 1 else 0
    third_cat = (category_data or [{}, {}, {}])[2].get("name", "N/A") if len(category_data or []) > 2 else "N/A"
    third_count_c = (category_data or [{}, {}, {}])[2].get("count", 0) if len(category_data or []) > 2 else 0
    if sample_report:
        try:
            when = sample_report.get("created_at")
            sample_when = when.strftime("%b %d, %Y") if hasattr(when, "strftime") else str(when or "recently")
        except Exception:
            sample_when = "recently"
        sample_slots = dict(sample_title=sample_report.get("title", "Untitled"), sample_category=sample_report.get("category", "General"), sample_status=sample_report.get("status", "Pending"), sample_location=sample_report.get("locname", "N/A"), sample_when=sample_when, sample_excerpt=(sample_report.get("description", "") or "")[:220])
    else:
        sample_slots = dict(sample_title="No reports yet", sample_category="N/A", sample_status="N/A", sample_location="N/A", sample_when="N/A", sample_excerpt="No reports filed yet.")
    _, v_exec, executive_summary = _ai_render("executive_summary", "exec|" + seed, total=total, resolved=resolved, pending=pending, res_pct=res_pct, next_target=next_target, top_cat=top_cat, top_cat_count=top_cat_count, top_loc=top_loc, top_loc_count=top_loc_count, months=months, avg_monthly=avg_monthly, latest_label=latest_label, latest_count=latest_count, peak_label=peak_label, peak_count=peak_count)
    _, v_mon, monthly_summary = _ai_render("monthly_summary", "mon|" + seed, months=months, total_monthly=total_monthly, avg_monthly=avg_monthly, peak_label=peak_label, peak_count=peak_count, latest_label=latest_label, latest_count=latest_count, latest_resolved=latest_resolved, trend_word=trend_word)
    _, v_samp, sample_summary = _ai_render("sample_summary", "samp|" + seed, **sample_slots)
    _, v_stat, status_summary = _ai_render("status_summary", "stat|" + seed, total=total, resolved=resolved, pending=pending, inprogress=inprog, pending_pct=round((pending / total) * 100) if total else 0, inprogress_pct=round((inprog / total) * 100) if total else 0, resolved_pct=res_pct, pipeline_word=pipeline_word, next_target=next_target)
    _, v_loc, location_summary = _ai_render("location_summary", "loc|" + seed, total=total, n_locations=len(top_locations or []), top_loc=top_loc, top_loc_count=top_loc_count, second_loc=second_loc, second_count=second_count, third_loc=third_loc, third_count=third_count)
    _, v_cat, category_summary = _ai_render("category_summary", "cat|" + seed, total=total, n_cats=len(category_data or []), top_cat=top_cat, top_cat_count=top_cat_count, second_cat=second_cat, second_count=second_count_c, third_cat=third_cat, third_count=third_count_c)
    _ANALYTICS_CACHE_V2.update({"time": time.time(), "executive_summary": executive_summary, "monthly_summary": monthly_summary, "sample_summary": sample_summary, "status_summary": status_summary, "location_summary": location_summary, "category_summary": category_summary, "ai_version": {"executive": v_exec, "monthly": v_mon, "sample": v_samp, "status": v_stat, "location": v_loc, "category": v_cat}})
    return monthly_summary, sample_summary, status_summary, location_summary, category_summary


# ===== API: Get Analytics Summary =====
@app.route('/api/analytics_summary')
def api_analytics_summary():
    if 'user_id' not in session or session.get('user_role') != 'lgu':
        return {'success': False, 'message': 'Unauthorized'}, 401
    return {
        'success': True,
        'monthly_summary': _ANALYTICS_CACHE_V2.get('monthly_summary', ''),
        'sample_summary': _ANALYTICS_CACHE_V2.get('sample_summary', ''),
        'status_summary': _ANALYTICS_CACHE_V2.get('status_summary', ''),
        'location_summary': _ANALYTICS_CACHE_V2.get('location_summary', ''),
        'category_summary': _ANALYTICS_CACHE_V2.get('category_summary', ''),
        'executive_summary': _ANALYTICS_CACHE_V2.get('executive_summary', ''),
        'ai_version': _ANALYTICS_CACHE_V2.get('ai_version', {}),
        'is_generating': False
    }

# ===== API: Force Regenerate Analytics Summary =====
@app.route('/api/analytics_summary/regenerate', methods=['POST'])
def api_analytics_summary_regenerate():
    """Cycle to the next of the 5 offline AI dictionary versions (website-model only)."""
    if 'user_id' not in session or session.get('user_role') != 'lgu':
        return {'success': False, 'message': 'Unauthorized'}, 401
    global _ANALYTICS_CACHE_V2
    _ANALYTICS_CACHE_V2['regen'] = (_ANALYTICS_CACHE_V2.get('regen', 0) + 1) % 5
    _ANALYTICS_CACHE_V2['time'] = 0
    return {'success': True, 'message': f"Switched to AI version {(_ANALYTICS_CACHE_V2['regen'] % 5) + 1} of 5. Refresh the analytics page."}

# ===== API: Get Reports by Category =====
@app.route('/api/reports')
def api_reports():
    if 'user_id' not in session or session.get('user_role') != 'lgu':
        return {'success': False, 'message': 'Unauthorized'}, 401
    
    category = request.args.get('category')
    if not category:
        return {'success': False, 'message': 'Category required'}, 400
        
    con = connect_db()
    cursor = con.cursor(dictionary=True)
    
    query = """
        SELECT r.report_id, r.title, r.category, r.status, r.created_at,
               a.first_name, a.last_name, a.profile_photo, l.locname
        FROM reports r
        JOIN accounts a ON r.account_id = a.account_id
        LEFT JOIN location l ON r.location_id = l.location_id
        WHERE r.category = %s
        ORDER BY r.created_at DESC
        LIMIT 50
    """
    cursor.execute(query, (category,))
    reports = cursor.fetchall()
    con.close()
    
    for r in reports:
        if r.get('created_at'):
            r['created_at_str'] = r['created_at'].strftime('%b %d')
        else:
            r['created_at_str'] = '—'
            
        if r.get('profile_photo'):
            photo_str = str(r['profile_photo']).replace("bytearray(b'", "").replace("b'", "").replace("')", "").replace("'", "")
            r['profile_photo'] = photo_str.replace('static/', '', 1) if photo_str.startswith('static/') else photo_str
            
        r['first_name'] = r.get('first_name') or '?'
        r['last_name'] = r.get('last_name') or ''
        r['locname'] = r.get('locname') or '—'
        r['status'] = r.get('status') or 'Pending'
        
    return {'success': True, 'reports': reports}



# ===== API: Check for duplicate concerns =====
@app.route('/api/check_duplicate', methods=['POST'])
def api_check_duplicate():
    """Check if a concern with similar title/description and nearby coordinates already exists"""
    if 'user_id' not in session:
        return {'success': False, 'message': 'Not logged in'}, 401
    
    data = request.get_json()
    if not data:
        return {'success': False, 'message': 'No data provided'}, 400
    
    title = (data.get('title') or '').strip()
    description = (data.get('description') or '').strip()
    geox = data.get('geox')
    geoy = data.get('geoy')
    
    if not title and not description:
        return {'success': False, 'message': 'No content to compare'}, 400
    
    # Score candidates with ONE OpenRouter call (batched LLM judge). Pre-filter cheaply first.
    combined_text = f"{title}. {description}" if title and description else (title or description)
    
    con = connect_db()
    cursor = con.cursor(dictionary=True)
    
    # Fetch all existing reports with their locations
    cursor.execute("""
        SELECT r.report_id, r.public_key, r.title, r.description, r.category, r.status, l.x, l.y, l.locname
        FROM reports r
        JOIN location l ON r.location_id = l.location_id
        WHERE l.x IS NOT NULL AND l.y IS NOT NULL AND r.public_key IS NOT NULL
        ORDER BY r.created_at DESC
        LIMIT 100
    """)
    existing_reports = [dict(r) for r in cursor.fetchall()]
    con.close()
    
    duplicates = []

    # Attach helper fields + cheap pre-filter so only top 12 reach the AI model
    for _r in existing_reports:
        _t = _r.get('title') or ''
        _d = _r.get('description') or ''
        _r['_text'] = f"{_t}. {_d}" if _t and _d else (_t or _d)
        _n, _dist = coordinate_proximity(geox, geoy, _r.get('x'), _r.get('y'))
        _r['_near'] = _n
        _r['_distance'] = _dist
        _ov = text_similarity(combined_text, _r['_text'])
        _b = 0.0
        if _r.get('category') and _r.get('category').lower() in combined_text.lower():
            _b += 0.3
        if _n:
            _b += 0.3
        _r['_pref'] = _ov + _b
    existing_reports.sort(key=lambda r: r.get('_pref', 0), reverse=True)
    shortlist = existing_reports[:12]
    # ONE OpenRouter call; {} when key missing/offline -> per-item fallback below
    llm_scores = llm_judge_duplicates(combined_text, shortlist)

    for report in shortlist:
        old_title = report.get('title') or ''
        old_text = report.get('_text', '')
        # OpenRouter LLM score first; offline word-overlap fallback when it fails
        sim = llm_scores.get(report['report_id'])
        if sim is None:
            sim = text_similarity(combined_text, old_text)
        near, distance = report.get('_near', False), report.get('_distance', float('inf'))
        
        # Consider duplicate if EITHER condition is strong:
        # - sim >= 0.80 (high text similarity regardless of location)
        # - sim >= 0.70 AND near (moderate text + nearby)
        # - sim >= 0.90 (very high text similarity)
        if sim >= 0.80 or (sim >= 0.70 and near) or sim >= 0.90:
            duplicates.append({
                'report_id': report['report_id'],
                'public_key': report.get('public_key', ''),
                'title': old_title,
                'category': report.get('category', ''),
                'location': report.get('locname', ''),
                'similarity': sim,  # Return as float 0-1
                'distance_m': round(distance, 1) if near else None,
                'status': report.get('status', 'Pending')
            })
    
    # Limit to top 5 most similar
    duplicates.sort(key=lambda d: d['similarity'], reverse=True)
    duplicates = duplicates[:5]
    
    return {
        'success': True,
        'duplicates': duplicates,
        'has_duplicates': len(duplicates) > 0
    }


    
@app.route('/admin', methods=['GET', 'POST'])
def admin(): 
   if 'user_id' not in session or session.get('user_role') != "admin":
       return redirect(url_for("login"))
   
   return redirect(url_for('activities'))


@app.route('/users', methods=['GET', 'POST'])
def users():

    if 'user_id' not in session or session.get('user_role') != 'admin':
        return redirect(url_for('login'))

    con = connect_db()
    cursor = con.cursor(dictionary=True)

    # One row per account/person.
    # Representative credential selection:
    # - Prefer Gmail credential (u.emorph LIKE '%gmail.com') when available
    # - Otherwise fall back to any credential for that account
    #
    # We still show: fullname, profile photo, email, phone number, and current role.
    # Actions must be executed using account_id (not user_id) so the update affects all
    # usercreds under that account/contact.
    cursor.execute(
        """
        SELECT
            a.account_id,
            CONCAT(a.first_name, ' ', a.last_name) AS fullname,
            a.profile_photo,
            c.email,
            c.phone_number,
            r.role,
            -- Representative user_id for display/debug (UI should not use it as identifier)
            rep.user_id AS representative_user_id
        FROM accounts a
        JOIN contacts c ON c.account_id = a.account_id
        JOIN usercreds u_rep_base ON u_rep_base.contact_id = c.contact_id
        -- Pick exactly one credential row per account: prefer Gmail by sorting
        JOIN (
            SELECT
                u.user_id,
                u.contact_id,
                ROW_NUMBER() OVER (
                    PARTITION BY ct.account_id
                    ORDER BY CASE WHEN u.emorph LIKE '%gmail.com' THEN 0 ELSE 1 END,
                             u.user_id ASC
                ) AS rn
            FROM usercreds u
            JOIN contacts ct ON ct.contact_id = u.contact_id
        ) rep ON rep.user_id = u_rep_base.user_id AND rep.rn = 1
        JOIN user_role ur ON ur.user_id = rep.user_id
        JOIN role r ON r.role_id = ur.role_id
        ORDER BY a.account_id ASC
        """
    )
    rows = cursor.fetchall()

    if request.method == 'POST':
        con = connect_db()
        cursor = con.cursor()
        acttype = request.form.get('act')
        account_id = request.form.get('account_id')

        if account_id:
            if acttype == "remove":
                try:
                    cursor.execute("SET FOREIGN_KEY_CHECKS=0")
                    
                    # 1. Delete reports and related data by this account
                    cursor.execute("DELETE FROM reports WHERE account_id = %s", (account_id,))
                    
                    # 2. Get all associated contact_ids
                    cursor.execute("SELECT contact_id FROM contacts WHERE account_id = %s", (account_id,))
                    contacts = cursor.fetchall()
                    
                    for c_row in contacts:
                        c_id = c_row[0]
                        # 3. Get all user_ids for this contact
                        cursor.execute("SELECT user_id FROM usercreds WHERE contact_id = %s", (c_id,))
                        users_list = cursor.fetchall()
                        
                        for u_row in users_list:
                            u_id = u_row[0]
                            cursor.execute("DELETE FROM user_role WHERE user_id = %s", (u_id,))
                            cursor.execute("DELETE FROM credentials WHERE user_id = %s", (u_id,))
                            cursor.execute("DELETE FROM usercreds WHERE user_id = %s", (u_id,))
                        
                        cursor.execute("DELETE FROM contacts WHERE contact_id = %s", (c_id,))
                        
                    # 4. Finally delete the account itself
                    cursor.execute("DELETE FROM accounts WHERE account_id = %s", (account_id,))
                    
                    cursor.execute("SET FOREIGN_KEY_CHECKS=1")
                    con.commit()
                except Exception as e:
                    print(f"Error removing account {account_id}: {e}")
                    con.rollback()
            else:
                # Update all credentials belonging to the selected account/contact.
                lock_val = None
                if acttype in ("ban", "restrict"):
                    lock_val = 'Y'
                elif acttype == "unban":
                    lock_val = 'N'

                if lock_val is not None:
                    # SQLite: no UPDATE..JOIN — resolve user_ids first, then update
                    cursor.execute("""
                        SELECT u.user_id FROM usercreds u
                        JOIN contacts ct ON ct.contact_id = u.contact_id
                        WHERE ct.account_id = %s
                    """, (account_id,))
                    _uids = [r[0] for r in cursor.fetchall()]
                    for _uid in _uids:
                        cursor.execute("UPDATE credentials SET is_locked = %s WHERE user_id = %s", (lock_val, _uid))
                    con.commit()

        con.close()
        return redirect(url_for("users"))

    con.close()
    return render_template('users.html', users=rows, active_page='users')
    

    

@app.route('/activities')
def activities():
    if 'user_id' not in session or session.get('user_role') != 'admin':
        return redirect(url_for('login'))

    # Filters from query string
    q = (request.args.get('q') or '').strip()
    action_type = (request.args.get('action_type') or '').strip().lower()

    from_date = (request.args.get('from_date') or '').strip()
    to_date = (request.args.get('to_date') or '').strip()

    con = connect_db()
    cursor = con.cursor(dictionary=True)

    activities = []

    # Common WHERE fragments
    def build_where(alias_dt_field: str, alias_title: str, alias_fullname: str, alias_type_literal: str):
        clauses = []
        params = []

        # action_type filter (works as an overall filter on the activity type)
        if action_type:
            clauses.append(f"{alias_type_literal} = %s")
            params.append(action_type)

        if q:
            # Match either title OR fullname using a single search box.
            clauses.append(f"(LOWER({alias_title}) LIKE %s OR LOWER({alias_fullname}) LIKE %s)")
            needle = f"%{q.lower()}%"
            params.append(needle)
            params.append(needle)


        # Date range: compare against the activity datetime field
        if from_date:
            clauses.append(f"DATE({alias_dt_field}) >= %s")
            params.append(from_date)

        if to_date:
            clauses.append(f"DATE({alias_dt_field}) <= %s")
            params.append(to_date)

        if not clauses:
            return "", []

        return "WHERE " + " AND ".join(clauses), params

    # 1) Reports created (type='report')
    where_report, params_report = build_where('r.created_at', 'r.title', "CONCAT(a.first_name, ' ', a.last_name)", "'report'")
    cursor.execute(
        f"""
        SELECT
            r.created_at AS dt,
            'report' AS type,
            r.title AS title,
            CONCAT(a.first_name, ' ', a.last_name) AS fullname,
            NULL AS text
        FROM reports r
        JOIN accounts a ON r.account_id = a.account_id
        {where_report}
        """,
        tuple(params_report)
    )
    activities.extend(cursor.fetchall())

    # 2) Report updates (type='update')
    where_update, params_update = build_where('rp.submitted_at', 'r.title', "CONCAT(a.first_name, ' ', a.last_name)", "'update'")
    cursor.execute(
        f"""
        SELECT
            rp.submitted_at AS dt,
            'update' AS type,
            r.title AS title,
            CONCAT(a.first_name, ' ', a.last_name) AS fullname,
            rp.description AS text
        FROM report_update rp
        JOIN reports r ON rp.report_id = r.report_id
        JOIN accounts a ON rp.account_id = a.account_id
        {where_update}
        """,
        tuple(params_update)
    )
    activities.extend(cursor.fetchall())

    # 3) Feedback uploads (type='feedback')
    where_feedback, params_feedback = build_where('f.uploaded_at', 'r.title', "CONCAT(a.first_name, ' ', a.last_name)", "'feedback'")
    cursor.execute(
        f"""
        SELECT
            f.uploaded_at AS dt,
            'feedback' AS type,
            r.title AS title,
            CONCAT(a.first_name, ' ', a.last_name) AS fullname,
            f.feedback AS text
        FROM feedbacks f
        JOIN reports r ON f.report_id = r.report_id
        JOIN accounts a ON f.account_id = a.account_id
        {where_feedback}
        """,
        tuple(params_feedback)
    )
    activities.extend(cursor.fetchall())

    con.close()

    # Sort descending by datetime
    activities.sort(key=lambda x: x.get('dt') or datetime.min, reverse=True)

    # Format dt for template
    for a in activities:
        dt = a.get('dt')
        a['dt'] = dt.strftime('%Y-%m-%d %H:%M:%S') if dt else ''

    return render_template('activities.html', activities=activities, active_page='activities')

# --- EXPORT ROUTES ---
@app.route('/api/export/users')
def export_users():
    if 'user_id' not in session or session.get('user_role') != 'admin':
        return redirect(url_for('login'))

    con = connect_db()
    cursor = con.cursor(dictionary=True)
    cursor.execute("""
        SELECT a.account_id, CONCAT(a.first_name, " ", a.last_name) AS fullname, 
               c.email, c.phone_number, r.role 
        FROM accounts a
        JOIN contacts c ON c.account_id = a.account_id 
        JOIN usercreds u ON c.contact_id = u.contact_id
        JOIN user_role ur ON u.user_id = ur.user_id
        JOIN role r ON ur.role_id = r.role_id
        ORDER BY a.account_id ASC
    """)
    users = cursor.fetchall()
    con.close()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "User Accounts"

    # Headers
    headers = ["Account ID", "Full Name", "Email Address", "Phone Number", "Role"]
    ws.append(headers)

    # Styling
    header_fill = PatternFill(start_color="8B0000", end_color="8B0000", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True)
    thin_border = Border(left=Side(style='thin'), right=Side(style='thin'), top=Side(style='thin'), bottom=Side(style='thin'))

    for col in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = thin_border

    # Data
    for i, u in enumerate(users, start=2):
        ws.cell(row=i, column=1, value=u['account_id'])
        ws.cell(row=i, column=2, value=u['fullname'])
        ws.cell(row=i, column=3, value=u['email'])
        ws.cell(row=i, column=4, value=u['phone_number'])
        ws.cell(row=i, column=5, value=u['role'])
        
        for col in range(1, 6):
            cell = ws.cell(row=i, column=col)
            cell.border = thin_border
            if col in [1, 5]:
                cell.alignment = Alignment(horizontal="center")

    # Column widths
    ws.column_dimensions['A'].width = 12
    ws.column_dimensions['B'].width = 30
    ws.column_dimensions['C'].width = 35
    ws.column_dimensions['D'].width = 20
    ws.column_dimensions['E'].width = 15

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    filename = f"ActBayan_Users_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
    return send_file(output, as_attachment=True, download_name=filename, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route('/api/export/activities')
def export_activities():
    if 'user_id' not in session or session.get('user_role') != 'admin':
        return redirect(url_for('login'))

    # Filters (same as activities route)
    q = (request.args.get('q') or '').strip()
    action_type = (request.args.get('action_type') or '').strip().lower()
    from_date = (request.args.get('from_date') or '').strip()
    to_date = (request.args.get('to_date') or '').strip()

    con = connect_db()
    cursor = con.cursor(dictionary=True)
    activities = []

    def build_where(alias_dt_field, alias_title, alias_fullname, alias_type_literal):
        clauses = []
        params = []
        if action_type:
            clauses.append(f"{alias_type_literal} = %s")
            params.append(action_type)
        if q:
            clauses.append(f"(LOWER({alias_title}) LIKE %s OR LOWER({alias_fullname}) LIKE %s)")
            needle = f"%{q.lower()}%"
            params.extend([needle, needle])
        if from_date:
            clauses.append(f"DATE({alias_dt_field}) >= %s")
            params.append(from_date)
        if to_date:
            clauses.append(f"DATE({alias_dt_field}) <= %s")
            params.append(to_date)
        
        where_sql = " AND ".join(clauses)
        if where_sql:
            where_sql = "WHERE " + where_sql
        return where_sql, params

    # Reports
    w_sql, w_params = build_where('r.created_at', 'r.title', 'CONCAT(a.first_name," ",a.last_name)', "'report'")
    q1 = f"""
        SELECT r.created_at AS dt, 'Report' AS type, r.title, CONCAT(a.first_name,' ',a.last_name) AS fullname, '' AS text
        FROM reports r
        LEFT JOIN accounts a ON r.account_id = a.account_id
        {w_sql}
    """
    cursor.execute(q1, tuple(w_params))
    activities.extend(cursor.fetchall())

    # Feedbacks
    w_sql2, w_params2 = build_where('f.uploaded_at', 'r.title', 'CONCAT(a.first_name," ",a.last_name)', "'feedback'")
    q2 = f"""
        SELECT f.uploaded_at AS dt, 'Feedback' AS type, r.title, CONCAT(a.first_name,' ',a.last_name) AS fullname, f.feedback AS text
        FROM feedbacks f
        LEFT JOIN reports r ON f.report_id = r.report_id
        LEFT JOIN accounts a ON f.account_id = a.account_id
        {w_sql2}
    """
    cursor.execute(q2, tuple(w_params2))
    activities.extend(cursor.fetchall())

    # Updates
    w_sql3, w_params3 = build_where('rp.submitted_at', 'r.title', 'CONCAT(a.first_name," ",a.last_name)', "'update'")
    q3 = f"""
        SELECT rp.submitted_at AS dt, 'Update' AS type, r.title, CONCAT(a.first_name,' ',a.last_name) AS fullname, rp.description AS text
        FROM report_update rp
        LEFT JOIN reports r ON rp.report_id = r.report_id
        LEFT JOIN accounts a ON rp.account_id = a.account_id
        {w_sql3}
    """
    cursor.execute(q3, tuple(w_params3))
    activities.extend(cursor.fetchall())

    con.close()

    activities.sort(key=lambda x: x['dt'] if x['dt'] else datetime.min, reverse=True)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "User Activities"

    headers = ["Date & Time", "Action Type", "Title/Report", "Full Name", "Description"]
    ws.append(headers)

    header_fill = PatternFill(start_color="8B0000", end_color="8B0000", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True)
    thin_border = Border(left=Side(style='thin'), right=Side(style='thin'), top=Side(style='thin'), bottom=Side(style='thin'))

    for col in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = thin_border

    for i, act in enumerate(activities, start=2):
        dt_val = act['dt'].strftime('%Y-%m-%d %H:%M:%S') if act.get('dt') else ''
        ws.cell(row=i, column=1, value=dt_val)
        ws.cell(row=i, column=2, value=act.get('type', ''))
        ws.cell(row=i, column=3, value=act.get('title', ''))
        ws.cell(row=i, column=4, value=act.get('fullname', ''))
        ws.cell(row=i, column=5, value=act.get('text', ''))
        
        for col in range(1, 6):
            cell = ws.cell(row=i, column=col)
            cell.border = thin_border
            if col in [1, 2]:
                cell.alignment = Alignment(horizontal="center")
            else:
                cell.alignment = Alignment(horizontal="left", wrap_text=True if col == 5 else False)

    ws.column_dimensions['A'].width = 22
    ws.column_dimensions['B'].width = 15
    ws.column_dimensions['C'].width = 30
    ws.column_dimensions['D'].width = 30
    ws.column_dimensions['E'].width = 60

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    filename = f"ActBayan_Activities_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
    return send_file(output, as_attachment=True, download_name=filename, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route('/api/comments/<int:report_id>')
def api_comments(report_id):
    con = connect_db()
    cursor = con.cursor(dictionary=True)
    user_id = session.get('user_id', 0)
    
    cursor.execute("""
        SELECT
            f.feedback_id,
            f.feedback AS comment_text,
            f.uploaded_at,
            f.reactions_data,
            a.first_name,
            a.last_name
        FROM feedbacks f
        LEFT JOIN accounts a ON a.account_id = f.account_id
        WHERE f.report_id = %s
        ORDER BY f.uploaded_at ASC
    """, (report_id,))
    comments = cursor.fetchall()
    con.close()
    
    # Format the dates and process reactions
    for c in comments:
        if c.get('uploaded_at'):
            c['created_at_formatted'] = c['uploaded_at'].strftime('%b %d, %Y - %I:%M %p')
            del c['uploaded_at']
            
        # Parse reaction data JSON
        try:
            rx_data = json.loads(c.get('reactions_data') or '{}')
        except:
            rx_data = {}
            
        counts = {}
        user_reaction = None
        for r_type, users in rx_data.items():
            if isinstance(users, list):
                counts[r_type] = len(users)
                if str(user_id) in [str(u) for u in users]:
                    user_reaction = r_type
                    
        c['reaction_counts'] = counts
        c['user_reaction'] = user_reaction
        if 'reactions_data' in c:
            del c['reactions_data']

    from flask import jsonify
    return jsonify(comments)

@app.route('/api/react_comment', methods=['POST'])
def react_comment():
    if 'user_id' not in session:
        from flask import jsonify
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401
        
    user_id = session.get('user_id')
    from flask import request, jsonify
    data = request.json or request.form
    feedback_id = data.get('feedback_id')
    reaction_type = data.get('reaction_type')
    
    if not feedback_id or not reaction_type:
        return jsonify({'success': False, 'message': 'Missing data'}), 400
        
    con = connect_db()
    cursor = con.cursor(dictionary=True)
    
    cursor.execute("SELECT reactions_data FROM feedbacks WHERE feedback_id = %s", (feedback_id,))
    row = cursor.fetchone()
    if not row:
        con.close()
        return jsonify({'success': False, 'message': 'Comment not found'}), 404
        
    try:
        rx_data = json.loads(row.get('reactions_data') or '{}')
    except:
        rx_data = {}
        
    user_str = str(user_id)
    prev_reaction = None
    
    # Remove user from any existing reactions and record previous reaction
    for r_type in list(rx_data.keys()):
        if isinstance(rx_data[r_type], list):
            if user_str in [str(u) for u in rx_data[r_type]]:
                prev_reaction = r_type
            rx_data[r_type] = [u for u in rx_data[r_type] if str(u) != user_str]
            
    if prev_reaction == reaction_type:
        action = 'removed'
    else:
        # Add new reaction
        if reaction_type not in rx_data or not isinstance(rx_data[reaction_type], list):
            rx_data[reaction_type] = []
        rx_data[reaction_type].append(user_str)
        action = 'added'
        
    # clean up empty lists
    rx_data = {k: v for k, v in rx_data.items() if v}
    
    cursor.execute("UPDATE feedbacks SET reactions_data = %s WHERE feedback_id = %s", (json.dumps(rx_data), feedback_id))
    con.commit()
    con.close()
    
    return jsonify({'success': True, 'action': action, 'reaction': reaction_type if action == 'added' else None})

@app.route('/api/report_updates/<int:report_id>')
def api_report_updates(report_id):
    con = connect_db()
    cursor = con.cursor(dictionary=True)
    cursor.execute("""
        SELECT description AS update_text, image_url, submitted_at
        FROM report_update
        WHERE report_id = %s
        ORDER BY submitted_at ASC
    """, (report_id,))
    updates = cursor.fetchall()
    con.close()
    
    # Format dates for frontend
    for u in updates:
        if u['submitted_at']:
            u['created_at_formatted'] = u['submitted_at'].strftime('%B %d, %Y - %I:%M %p')
            # delete datetime object since it cannot be serialized directly by flask jsonify (unless configured)
            del u['submitted_at']
            
    from flask import jsonify
    return jsonify(updates)
    
@app.route('/admin/login_attempts')
def admin_login_attempts():
    if 'user_id' not in session or session.get('user_role') != 'admin':
        return redirect(url_for('login'))
        
    con = connect_db()
    cursor = con.cursor(dictionary=True)
    cursor.execute("SELECT * FROM login_logs ORDER BY attempt_time DESC")
    logs = cursor.fetchall()
    
    cursor.execute("SELECT c.user_id, c.failed_attempts, c.is_locked, c.lock_duration, a.first_name, a.last_name, a.profile_photo FROM credentials c JOIN usercreds u ON c.user_id = u.user_id JOIN contacts ct ON u.contact_id = ct.contact_id JOIN accounts a ON ct.account_id = a.account_id WHERE c.failed_attempts > 0 OR c.is_locked = 'Y'")
    banned_users = cursor.fetchall()
    
    con.close()
    return render_template('admin_login_attempts.html', logs=logs, banned_users=banned_users, active_page='login_attempts')

@app.route('/admin/reports')
def admin_reports():
    if 'user_id' not in session or session.get('user_role') != 'admin':
        return redirect(url_for('login'))
        
    con = connect_db()
    cursor = con.cursor(dictionary=True)
    cursor.execute("""
        SELECT r.*, a.first_name, a.last_name, a.profile_photo AS reporter_photo, l.locname 
        FROM reports r 
        LEFT JOIN accounts a ON r.account_id = a.account_id 
        LEFT JOIN location l ON r.location_id = l.location_id
        ORDER BY r.created_at DESC
    """)
    reports = cursor.fetchall()
    con.close()
    return render_template('admin_reports.html', reports=reports, active_page='reports')

@app.route('/admin/settings', methods=['GET', 'POST'])
def admin_settings():
    if 'user_id' not in session or session.get('user_role') != 'admin':
        return redirect(url_for('login'))
        
    con = connect_db()
    cursor = con.cursor(dictionary=True)
    
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'change_role':
            target_user_id = request.form.get('usercreds_user_id')
            new_role_id = request.form.get('role_id')
            if target_user_id and new_role_id:
                # Need to update or insert?
                cursor.execute("SELECT 1 FROM user_role WHERE user_id = %s", (target_user_id,))
                exists = cursor.fetchone()
                if exists:
                    contactlist = """
                    SELECT contact_id FROM usercreds WHERE user_id = %s
                    """
                    
                    cursor.execute(contactlist, (target_user_id,))
                    c = cursor.fetchone()
                    
                    cursor.execute("SELECT user_id FROM usercreds WHERE contact_id = %s", (c["contact_id"],))
                    us = cursor.fetchall()
                    
                    for users in us:
                        cursor.execute("UPDATE user_role SET role_id = %s WHERE user_id = %s", (new_role_id, users["user_id"]))
                else:
                    cursor.execute("INSERT INTO user_role (user_id, role_id) VALUES (%s, %s)", (target_user_id, new_role_id))
                con.commit()
    
    cursor.execute("""
        SELECT 
    u.user_id,
    a.first_name,
    a.last_name,
    a.profile_photo,
    r.role_id,
    r.role
    FROM usercreds u
    JOIN contacts ct ON u.contact_id = ct.contact_id
    JOIN accounts a ON ct.account_id = a.account_id
    LEFT JOIN user_role ur ON u.user_id = ur.user_id
    LEFT JOIN role r ON ur.role_id = r.role_id
    WHERE u.emorph LIKE '%gmail.com';
    """)
    users = cursor.fetchall()
    
    cursor.execute("SELECT * FROM role")
    roles = cursor.fetchall()
    con.close()
    return render_template('admin_settings.html', users=users, roles=roles, active_page='settings')

@app.route('/')
def home():
    
    db = connect_db()
    cursor = db.cursor(dictionary=True)
    user_data = None
    if 'user_id' in session:
        try:
            con = connect_db()
            inner_cursor = con.cursor(dictionary=True)
            inner_cursor.execute("SELECT first_name, last_name, profile_photo FROM accounts WHERE account_id = %s", (session['user_id'],))
            rows = inner_cursor.fetchall()
            if rows:
                user_data = rows[0]
            con.close()
        except Exception:
            pass
        
    reps = "SELECT title, image_url, description, status, category FROM reports"
    cursor.execute(reps)
    reports = cursor.fetchall()
    
    return render_template('index.html', user=user_data, reports=reports)

@app.route('/lgu', methods=['GET', 'POST'])
def lgu():
    if 'user_id' not in session or session.get('user_role') != 'lgu':
        return redirect(url_for('login'))


    account_id = session.get('user_id')

    # Fetch data for GET (and for re-render if needed)
    sql_reps = """
        SELECT r.*, a.first_name, a.last_name, a.profile_photo
        FROM reports r
        JOIN accounts a ON r.account_id = a.account_id
        ORDER BY r.created_at DESC
    """

    if request.method == 'POST':
        data = request.form
        report_id = data.get('report_id')
        description = data.get('update_description', '').strip()

        # Basic validation
        if not report_id or not description:
            return redirect(url_for('lgu'))

        # Optional image upload
        image_url = None
        update_photo = request.files.get('update_photo')
        if update_photo and update_photo.filename:
            os.makedirs('static/uploads', exist_ok=True)
            filename = f"update_{report_id}_{int(datetime.now().timestamp())}_{update_photo.filename}"
            filepath = f"static/uploads/{filename}"
            update_photo.save(filepath)
            image_url = filepath

        con = connect_db()
        cursor = con.cursor()
        query = """
            INSERT INTO report_update (account_id, report_id, image_url, description, submitted_at)
            VALUES (%s, %s, %s, %s, %s)
        """
        cursor.execute(query, (
            account_id,
            report_id,
            image_url,
            description,
            datetime.now()
        ))
        
        status = data.get('update_status')
        if status:
            cursor.execute("UPDATE reports SET status = %s WHERE report_id = %s", (status, report_id))
            
        con.commit()
        con.close()
        return redirect(url_for('lgu'))

    # GET
    con = connect_db()
    cursor = con.cursor(dictionary=True)
    cursor.execute(sql_reps)
    reps = cursor.fetchall()

    # Extra media (2nd..Nth files from reports_media) for FB-style collage grid.
    # Try/except so LGU page still renders if reports_media is missing.
    media_by_report = {}
    try:
        _lgu_ids = [r.get('report_id') for r in reps if r.get('report_id') is not None]
        if _lgu_ids:
            _lgu_ph = ",".join(["%s"] * len(_lgu_ids))
            cursor.execute(
                f"SELECT report_id, path FROM reports_media WHERE report_id IN ({_lgu_ph}) ORDER BY report_id",
                tuple(_lgu_ids)
            )
            for mrow in cursor.fetchall():
                media_by_report.setdefault(mrow.get('report_id'), []).append(mrow.get('path'))
    except Exception:
        media_by_report = {}

    report_ids = [r.get('report_id') for r in reps if r.get('report_id') is not None]

    # ===== FEEDBACKS / COMMENTS (same query as file_concern) =====
    feedbacks_by_report = {}
    feedback_counts_by_report = {}

    if report_ids:
        placeholders = ','.join(['%s'] * len(report_ids))
        feedback_query = f"""
            SELECT
                f.feedback_id,
                f.report_id,
                f.account_id,
                f.feedback,
                f.uploaded_at,
                a.profile_photo,
                CONCAT(a.first_name, ' ', a.last_name) AS fullname
            FROM feedbacks f
            LEFT JOIN accounts a ON a.account_id = f.account_id
            WHERE f.report_id IN ({placeholders})
            ORDER BY f.feedback_id DESC
        """
        cursor.execute(feedback_query, tuple(report_ids))
        all_feedbacks = cursor.fetchall()
        now = datetime.now()
        for fb in all_feedbacks:
            if fb.get('uploaded_at'):
                diff = now - fb['uploaded_at']
                if diff.days > 7:
                    fb['time_ago'] = fb['uploaded_at'].strftime('%b %d, %Y')
                elif diff.days > 0:
                    fb['time_ago'] = f"{diff.days}d ago"
                else:
                    seconds = diff.seconds
                    if seconds >= 3600:
                        fb['time_ago'] = f"{seconds // 3600}h ago"
                    elif seconds >= 60:
                        fb['time_ago'] = f"{seconds // 60}m ago"
                    else:
                        fb['time_ago'] = "Just now"
            else:
                fb['time_ago'] = ""
            feedbacks_by_report.setdefault(fb.get('report_id'), []).append(fb)

    for rid, fbs in feedbacks_by_report.items():
        feedback_counts_by_report[rid] = len(fbs)

    # ===== UPVOTES (same query as file_concern) =====
    upvote_counts = {}
    user_upvoted = set()

    if report_ids:
        placeholders = ','.join(['%s'] * len(report_ids))
        cursor.execute(
            f"SELECT report_id, COUNT(*) as count FROM upvotes WHERE report_id IN ({placeholders}) GROUP BY report_id",
            tuple(report_ids)
        )
        for row in cursor.fetchall():
            upvote_counts[row['report_id']] = row['count']

        # LGU also tracks their own upvotes
        if account_id:
            cursor.execute(
                f"SELECT report_id FROM upvotes WHERE account_id = %s AND report_id IN ({placeholders})",
                (account_id,) + tuple(report_ids)
            )
            for row in cursor.fetchall():
                user_upvoted.add(row['report_id'])

    # ===== LGU USER PROFILE (for sidebar) =====
    cursor.execute(
        "SELECT first_name, last_name, profile_photo FROM accounts WHERE account_id = %s",
        (account_id,)
    )
    user_data = cursor.fetchone()
    con.close()

    return render_template(
        "lgu.html",
        reps=reps,
        media_by_report=media_by_report,
        feedbacks_by_report=feedbacks_by_report,
        feedback_counts_by_report=feedback_counts_by_report,
        upvote_counts=upvote_counts,
        user_upvoted=user_upvoted,
        user=user_data
    )



@app.route('/dashboard', methods=['GET', 'POST'])
def dashboard():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    con = connect_db()
    cursor = con.cursor(dictionary=True)

    account_id = session.get('user_id')
    user_name = session.get('user_name')

    sql_recent = """
        SELECT r.*, a.first_name, a.last_name 
        FROM reports r
        JOIN accounts a ON r.account_id = a.account_id
        ORDER BY r.created_at DESC
    """
    cursor.execute(sql_recent)
    all_concerns = cursor.fetchall()

    # Fetch real user data for sidebar
    cursor.execute(
        "SELECT first_name, last_name, profile_photo, account_status FROM accounts WHERE account_id = %s",
        (account_id,)
    )
    user_rows = cursor.fetchall()
    user_data = user_rows[0] if user_rows else None
    
    cursor.execute("SELECT * FROM announcements ORDER BY submitted_at DESC LIMIT 1")
    ann_rows = cursor.fetchall()
    latest_ann = ann_rows[0] if ann_rows else None
    if latest_ann and latest_ann['submitted_at']:
        latest_ann['formatted_date'] = latest_ann['submitted_at'].strftime('%b %d, %Y')
        
    con.close()

    return render_template(
        'dashboard.html',
        recent=all_concerns,
        ngalan=user_name,
        user=user_data,
        latest_ann=latest_ann
    )


@app.route('/login', methods=['GET', 'POST'])
def login():
    con = connect_db()
    cursor = con.cursor(buffered=True)

    if request.method == 'POST':
        data = request.form
        u = data.get('e', '').strip()
        p = data.get('pass', '').strip()
        
        if not u or not p:
            return render_template('login.html', error="Please enter email/phone and password")
            
        u = u.replace(' ', '')
        if u.startswith('+63'):
            u = '0' + u[3:]

        
        # Fetch registration record
        query_reg = "SELECT registration_id, email_address, phone_number, password FROM registration WHERE email_address = %s OR phone_number = %s"
        cursor.execute(query_reg, (u, u))
        reg_record = cursor.fetchone()
        
        if reg_record and check_password_bcrypt(reg_record[3], p):

            reg_id = reg_record[0]
            if reg_id:
                session['reg_id'] = reg_id
                return redirect(url_for('cd'))
        
        credquery = """SELECT u.emorph, p.password FROM credentials AS c
                    JOIN usercreds AS u ON u.user_id = c.user_id
                    JOIN password AS p ON p.pass_id = c.pass_id
                    WHERE u.emorph = %s
                    """
        cursor.execute(credquery, (u,))
        creds = cursor.fetchone()
        if creds and check_password_bcrypt(creds[1], p):

            emorph = creds[0]

            neym = """SELECT c.account_id, a.first_name
              FROM accounts a
              JOIN contacts c ON a.account_id = c.account_id
              WHERE c.email = %s OR c.phone_number = %s
           """
            cursor.execute(neym, (emorph, emorph))
            nem = cursor.fetchone()

            if nem:
                account_id = nem[0]
                pangalan = nem[1]
                
                # SELECT u.user_id, u.emorph, r.role FROM usercreds u JOIN contacts c ON u.contact_id = c.contact_id JOIN user_role ur ON u.user_id = ur.user_id JOIN role r ON ur.role_id = r.role_id WHERE u.emorph = %s;
                
                # Role verification (check using email/phone first -> emorph -> usercreds.user_id)
                rowl = """
                    SELECT u.user_id, u.emorph, r.role
                    FROM usercreds u
                    JOIN contacts c ON u.contact_id = c.contact_id
                    JOIN user_role ur ON u.user_id = ur.user_id
                    JOIN role r ON ur.role_id = r.role_id
                    WHERE u.emorph = %s
                """
                cursor.execute(rowl, (emorph,))
                rul = cursor.fetchone()

                # If user is found in user_role table, check banned status from credentials
                usercreds_user_id = None
                role_value = ""
                if rul:
                    usercreds_user_id = rul[0]
                    if len(rul) > 2 and rul[2] is not None:
                        role_value = str(rul[2]).strip().casefold()

                if usercreds_user_id is not None:
                    ban_check_sql = "SELECT is_locked, lock_duration FROM credentials WHERE user_id = %s"
                    cursor.execute(ban_check_sql, (usercreds_user_id,))
                    ban_row = cursor.fetchone()
                    is_locked = ban_row[0] if ban_row else None
                    lock_duration = ban_row[1] if ban_row and len(ban_row) > 1 else None
                    # ===== BAN CHECK: permanent (is_locked='Y') OR temp (lock_duration still active) =====
                    ban_reason = None
                    if is_locked == 'Y':
                        ban_reason = "This account is banned."
                    elif lock_duration is not None and str(lock_duration).strip() not in ('', '0', 'None', 'N'):
                        try:
                            now = datetime.now()
                            if isinstance(lock_duration, datetime):
                                if lock_duration > now:
                                    secs = int((lock_duration - now).total_seconds())
                                    mins = max(1, (secs + 59) // 60)
                                    ban_reason = f"Temporarily banned for profanity. Try again in {mins} minute(s)."
                            elif isinstance(lock_duration, (int, float)):
                                s = str(int(lock_duration))
                                # INT column holding YYYYMMDDHHMMSS number (DATE_ADD written into INT)
                                if len(s) >= 12 and s.startswith('20'):
                                    try:
                                        expiry = datetime.strptime(s[:14], '%Y%m%d%H%M%S')
                                        if expiry > now:
                                            secs = int((expiry - now).total_seconds())
                                            mins = max(1, (secs + 59) // 60)
                                            ban_reason = f"Temporarily banned for profanity. Try again in {mins} minute(s)."
                                    except Exception:
                                        ban_reason = "Temporarily banned for profanity. Try again later."
                                elif int(lock_duration) > 0:
                                    ban_reason = "Temporarily banned for profanity. Try again later."
                            else:
                                s = str(lock_duration).strip()
                                expiry = None
                                for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M:%S.%f', '%Y%m%d%H%M%S'):
                                    try:
                                        expiry = datetime.strptime(s[:19] if '.' not in fmt and len(s) > 19 else s, fmt)
                                        break
                                    except Exception:
                                        continue
                                if expiry is not None:
                                    if expiry > now:
                                        secs = int((expiry - now).total_seconds())
                                        mins = max(1, (secs + 59) // 60)
                                        ban_reason = f"Temporarily banned for profanity. Try again in {mins} minute(s)."
                                else:
                                    ban_reason = "Temporarily banned for profanity. Try again later."
                        except Exception:
                            ban_reason = "Temporarily banned for profanity. Try again later."
                    if ban_reason:
                        cursor.execute("INSERT INTO login_logs (emorph, status, attempt_time) VALUES (%s, %s, %s)", (u, 'Failed (Banned)', datetime.now()))
                        con.commit()
                        con.close()
                        return render_template('login.html', error=ban_reason)

                cursor.execute("INSERT INTO login_logs (emorph, status, attempt_time) VALUES (%s, %s, %s)", (u, 'Success', datetime.now()))
                con.commit()

                session['user_id'] = account_id
                session['user_name'] = pangalan

                if role_value == 'lgu official':
                    # IMPORTANT: `/lgu_announcements` expects session['user_role'] == 'lgu'
                    session['user_role'] = 'lgu'
                    con.close()
                    return redirect(url_for("lgu"))
                elif role_value == "admin":
                    session['user_role'] = 'admin'
                    con.close()
                    return redirect(url_for("admin"))
                
                # Critical check: prevent incomplete accounts from accessing the dashboard
                cursor.execute("SELECT account_status FROM accounts WHERE account_id = %s", (account_id,))
                status_row = cursor.fetchone()
                if status_row and status_row[0] == 'Incomplete':
                    session['pending_user_id'] = account_id
                    session.pop('user_id', None)
                    session.pop('user_name', None)
                    con.close()
                    return redirect(url_for("id_verification"))

                session['user_role'] = 'resident'
                con.close()
                return redirect(url_for("dashboard"))
            

        cursor.execute("INSERT INTO login_logs (emorph, status, attempt_time) VALUES (%s, %s, %s)", (u, 'Failed', datetime.now()))
        con.commit()
        con.close()
        return render_template('login.html', error="Invalid credentials")
    return render_template('login.html')


@app.route('/file_concern', methods=['GET', 'POST'])
def file_concern():

    if 'user_id' not in session:
        return redirect(url_for('login'))

    con = connect_db()
    cursor = con.cursor(dictionary=True)

    if request.method == 'POST':
        con.autocommit = False
        data = request.form
        fn = request.form.get("fn")
        
        category = data.get('category')
        title = data.get('title')
        # Template sends location text (locname)
        location = data.get('location')
        description = data.get('description')

        # Optional coordinates (template currently only sends location text;
        # if geox/geoy are missing, we try to default to 0 so location_id is not NULL)
        geox = data.get('geox')
        geoy = data.get('geoy')

        if geox is None or str(geox).strip() == '':
            geox = 0
        if geoy is None or str(geoy).strip() == '':
            geoy = 0
        
        cursor.execute("SELECT account_id, account_status FROM accounts WHERE account_id = %s", (session['user_id'],))
        user_rows = cursor.fetchall()
        user_record = user_rows[0] if user_rows else None

        if not user_record:
            return redirect(url_for('login'))
            
        if user_record.get('account_status') != 'Verified':
            con.close()
            return redirect(url_for('file_concern'))
            
        account_id = user_record['account_id']

        # ===== MULTI-FILE UPLOAD (Step 3 revamp: images + any video, no size limit) =====
        # Rule: 1st uploaded file -> reports.image_url (cover), 2nd..Nth -> reports_media(report_id, path)
        ALLOWED_IMG_EXTS = {'jpg', 'jpeg', 'png', 'gif', 'bmp', 'webp', 'svg', 'tif', 'tiff', 'ico', 'heic', 'heif'}
        ALLOWED_VIDEO_EXTS = {'mp4', 'mov', 'avi', 'mkv', 'webm', 'flv', 'wmv', 'm4v', '3gp',
                              '3g2', 'ogv', 'ogg', 'mts', 'm2ts', 'ts', 'mxf', 'vob', 'rm',
                              'rmvb', 'asf', 'f4v', 'mpg', 'mpeg', 'mpe', 'm1v', 'm2v'}
        ALLOWED_EXTS = ALLOWED_IMG_EXTS | ALLOWED_VIDEO_EXTS
        MAX_FILES = 5  # count cap only; no byte-size cap per user request

        def _ext_ok(filename):
            return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTS

        files = request.files.getlist('concern_photos')
        # Backward compat: dashboard.html / lgu.html still post single `concern_photo`
        if not files or all(not (f and f.filename) for f in files):
            legacy = request.files.get('concern_photo')
            files = [legacy] if legacy and legacy.filename else []

        saved_paths = []
        if files:
            os.makedirs('static/uploads/concerns', exist_ok=True)
            ts = int(datetime.now().timestamp())
            for i, f in enumerate(files[:MAX_FILES]):
                if not f or not f.filename:
                    continue
                if not _ext_ok(f.filename):
                    continue  # skip unsupported types silently
                safe_name = secure_filename(f.filename)
                filename = f"concern_{account_id}_{ts}_{i}_{safe_name}"
                filepath = f"static/uploads/concerns/{filename}"
                f.save(filepath)
                saved_paths.append(filepath)

        img_url = saved_paths[0] if saved_paths else None
        extra_paths = saved_paths[1:] if len(saved_paths) > 1 else []
            
        # For file_concern: insert geolocation into `location` first,
        # then insert report using the resulting `location_id`.
        # Expected form fields: location (locname), geox, geoy.
        insert_location_sql = """
            INSERT INTO location (locname, x, y)
            VALUES (%s, %s, %s)
        """
        get_location_id_sql = "SELECT MAX(location_id) FROM location"  # Get the last inserted location_id for association with report
        
        query = """
            INSERT INTO reports (account_id, category, title, location_id, image_url, description, status, public_key)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """

        # For fn==pc (posting feedback), do NOT insert a new report
        if fn == "pc":
            feedback = (data.get('comment') or '').strip()
            report_id = data.get('report_id')

            # ===== PROFANITY CHECK (profanity.py) =====
            # Function-level import so Flask auto-reload picks up
            # profanity.py / profanitylist.txt edits without full restart.
            from profanity import check_profanity as _check_profanity
            is_profane, matched_word = _check_profanity(feedback)
            # Detect AJAX (fetch) vs plain form post (full page reload)
            wants_json = (
                request.headers.get('X-Requested-With') == 'XMLHttpRequest'
                or 'application/json' in (request.headers.get('Accept') or '')
                or request.args.get('format') == 'json'
            )
            if is_profane:
                # ===== STRIKE WINDOW: 3 strikes inside 2 minutes -> 5-min ban =====
                # Strikes older than 2 minutes expire (sliding window).
                from datetime import timedelta
                now = datetime.now()
                try:
                    strikes = int(session.get('profanity_strikes', 0) or 0)
                except Exception:
                    strikes = 0
                first_ts_raw = session.get('profanity_first_at')
                first_ts = None
                if first_ts_raw:
                    try:
                        first_ts = datetime.fromisoformat(str(first_ts_raw))
                    except Exception:
                        first_ts = None
                # Window expired (or missing) -> restart at strike 1
                if not first_ts or (now - first_ts) > timedelta(minutes=2):
                    strikes = 0
                    first_ts = now
                    session['profanity_first_at'] = now.isoformat()
                strikes += 1
                session['profanity_strikes'] = strikes
                con.rollback()  # never pass profanity to the query

                # Remaining window seconds (for playful countdown in toast)
                try:
                    window_left = max(0, int((timedelta(minutes=2) - (now - first_ts)).total_seconds()))
                except Exception:
                    window_left = 120

                # ===== PLAYFUL WARNINGS: 1. dont you dare / 2. you try again huh =====
                if strikes == 1:
                    warn_msg = "Hey! Don't you dare. Watch your words! (1/3)"
                elif strikes == 2:
                    warn_msg = f"You trying again huh? Last warning! (2/3, {window_left}s left)"
                else:
                    warn_msg = None

                if strikes >= 3:
                    # 3rd strike -> ban 5 minutes (SQLite-compatible: no UPDATE..JOIN / DATE_ADD)
                    try:
                        cursor.execute("""
                            SELECT uc.user_id FROM usercreds uc
                            JOIN contacts co ON uc.contact_id = co.contact_id
                            WHERE co.account_id = %s
                        """, (account_id,))
                        _ban_uids = [r[0] for r in cursor.fetchall()]
                        for _bu in _ban_uids:
                            cursor.execute("UPDATE credentials SET lock_duration = datetime('now','+5 minutes') WHERE user_id = %s", (_bu,))
                        con.commit()
                    except Exception as e:
                        print(f"Profanity ban failed: {e}")
                        con.rollback()
                    session['profanity_strikes'] = 0
                    session.pop('profanity_first_at', None)
                    con.close()
                    ban_msg = 'Banned for 5 minutes: 3x profanity in 2 minutes. Time-out for you!'
                    if wants_json:
                        return jsonify({
                            'success': False,
                            'blocked': True,
                            'banned': True,
                            'strikes': 3,
                            'window': '2min',
                            'message': ban_msg
                        }), 403
                    # Plain form post (page reload): flash so the
                    # warning still appears bottom-right after reload.
                    from flask import flash
                    flash(ban_msg, 'error')
                    return redirect(url_for('file_concern'))

                con.close()
                if wants_json:
                    return jsonify({
                        'success': False,
                        'blocked': True,
                        'banned': False,
                        'strikes': strikes,
                        'matched': matched_word,
                        'message': warn_msg
                    }), 400
                # Plain form post (page reload): flash fallback
                from flask import flash
                flash(warn_msg, 'error')
                return redirect(url_for('file_concern'))

            if report_id and feedback:
                comque = """
                    INSERT INTO feedbacks (account_id, report_id, feedback, uploaded_at)
                    VALUES (%s, %s, %s, %s)
                """
                cursor.execute(comque, (account_id, report_id, feedback, datetime.now()))

                # AJAX comment posts expect JSON so the bottom-right toast can fire
                if wants_json:
                    con.commit()
                    con.close()
                    # NOTE: clean comment does NOT reset strikes — only the
                    # 2-minute window expiry or the 5-min ban clears them.
                    return jsonify({'success': True, 'message': 'Comment sent successfully!'})

        else:
            # If geolocation coordinates are provided, store them in `location`.
            # - location variable from template is treated as locname
            # - request.form should provide geox and geoy
            # (fallbacks included because some browsers/templates may not send them)
            geox = data.get('geox')
            geoy = data.get('geoy')

            # ===== STRICT AI DUPLICATE BLOCKING =====
            # Fetch the last 50 reports (as dicts), compute coordinate proximity and
            # text similarity, pass top 12 to the LLM judge. If the similarity hits
            # the threshold, block the submission.
            con2 = connect_db()
            cursor2 = con2.cursor(dictionary=True)
            cursor2.execute("SELECT r.*, l.x, l.y, l.locname FROM reports r JOIN location l ON r.location_id = l.location_id ORDER BY r.created_at DESC LIMIT 50")
            recent50 = [dict(r) for r in cursor2.fetchall()]
            con2.close()

            candidate_reports = []
            for rep in recent50:
                rep_x = rep.get('x')
                rep_y = rep.get('y')
                near, _dist = coordinate_proximity(geox, geoy, rep_x, rep_y)
                if not near:
                    continue
                title = (rep.get('title') or '').strip()
                description = (rep.get('description') or '').strip()
                text = f"{title}. {description}" if title and description else (title or description)
                sim = text_similarity(combined_text if 'combined_text' in locals() else (title or description), text)
                candidate_reports.append({
                    'report_id': rep.get('report_id'),
                    'locname': rep.get('locname'),
                    'title': title,
                    'text': text,
                    'sim': sim,
                    'near': near
                })

            candidate_reports.sort(key=lambda c: c['sim'], reverse=True)
            top_matches = candidate_reports[:12]

            if top_matches:
                llm_scores = llm_judge_duplicates(combined_text, top_matches)
                for m in top_matches:
                    sim = llm_scores.get(m['report_id'])
                    if sim is None:
                        sim = m['sim']
                    if sim >= 0.85 or (sim >= 0.75 and m['near']) or sim >= 0.95:
                        con.rollback()
                        flash("Duplicate Report Detected: Your concern is very similar to an existing report in this area. Submission blocked.", 'error')
                        con.close()
                        return redirect(url_for('file_concern'))

            # No duplicate detected -> proceed with insertion
            location_id = None
            # Always insert into `location` so we can always attach a non-NULL location_id.
            # If geox/geoy were missing, we already defaulted them to 0 above.
            cursor.execute(insert_location_sql, (location, geox, geoy))
            location_id = cursor.lastrowid  # race-safe (was SELECT MAX)

            # Generate UUID public key for this report
            public_key = str(uuid.uuid4())
            cursor.execute(query, (account_id, category, title, location_id, img_url, description, 'Pending', public_key))

            # Rule: 1st file already stored as reports.image_url cover;
            # 2nd..Nth files -> reports_media (report_id, path)
            if extra_paths:
                report_id = cursor.lastrowid
                cursor.executemany(
                    "INSERT INTO reports_media (report_id, path) VALUES (%s, %s)",
                    [(report_id, p) for p in extra_paths]
                )

        con.commit()
        con.close()
        return redirect(url_for('file_concern'))
        
    # NOTE: reports without rows in reports_media must still appear,
    # so we avoid INNER JOIN here and load media in a second query below.
    fetch_query = """
        SELECT r.*, a.first_name, a.last_name, a.profile_photo
        FROM reports r
        JOIN accounts a ON r.account_id = a.account_id
        ORDER BY r.created_at DESC
    """
    cursor.execute(fetch_query)
    all_concerns = cursor.fetchall()

    # Load extra media (2nd..Nth files) grouped by report for cover + "+N" badge.
    # Wrapped in try/except so page still renders if reports_media is missing.
    media_by_report = {}
    media_counts_by_report = {}
    try:
        _rep_ids = [c.get('report_id') for c in all_concerns if c.get('report_id') is not None]
        if _rep_ids:
            _ph = ",".join(["%s"] * len(_rep_ids))
            cursor.execute(
                f"SELECT report_id, path FROM reports_media WHERE report_id IN ({_ph}) ORDER BY report_id",
                tuple(_rep_ids)
            )
            for mrow in cursor.fetchall():
                rid = mrow.get('report_id')
                media_by_report.setdefault(rid, []).append(mrow.get('path'))
            for rid, plist in media_by_report.items():
                media_counts_by_report[rid] = len(plist)
    except Exception:
        media_by_report = {}
        media_counts_by_report = {}

    # Fetch all updates and group them by report_id for per-card rendering
    cursor.execute("""
        SELECT 
        CONCAT(a.first_name, ' ', a.last_name) AS fullname,
        rp.report_id,
        rp.image_url,
        rp.description,
        rp.submitted_at
        FROM report_update rp
        JOIN accounts a ON a.account_id = rp.account_id
        ORDER BY rp.submitted_at DESC;
    """)
    all_updates = cursor.fetchall()

    # Fetch feedbacks for all reports in one query and group them by report_id
    report_ids = [c.get('report_id') for c in all_concerns if c.get('report_id') is not None]
    feedbacks_by_report = {}
    feedback_counts_by_report = {}

    if report_ids:
        placeholders = ",".join(["%s"] * len(report_ids))
        feedback_query = f"""
            SELECT 
                f.feedback_id,
                f.report_id,
                f.account_id,
                f.feedback,
                f.uploaded_at,
                a.profile_photo,
                CONCAT(a.first_name, ' ', a.last_name) AS fullname
            FROM feedbacks f
            LEFT JOIN accounts a ON a.account_id = f.account_id
            WHERE f.report_id IN ({placeholders})
            ORDER BY f.feedback_id DESC
        """
        cursor.execute(feedback_query, tuple(report_ids))
        all_feedbacks = cursor.fetchall()
        now = datetime.now()
        for fb in all_feedbacks:
            if fb.get('uploaded_at'):
                diff = now - fb['uploaded_at']
                if diff.days > 7:
                    fb['time_ago'] = fb['uploaded_at'].strftime('%b %d, %Y')
                elif diff.days > 0:
                    fb['time_ago'] = f"{diff.days}d ago"
                else:
                    seconds = diff.seconds
                    if seconds >= 3600:
                        hours = seconds // 3600
                        fb['time_ago'] = f"{hours}h ago"
                    elif seconds >= 60:
                        minutes = seconds // 60
                        fb['time_ago'] = f"{minutes}m ago"
                    else:
                        fb['time_ago'] = "Just now"
            else:
                fb['time_ago'] = ""
                
            rid = fb.get('report_id')
            feedbacks_by_report.setdefault(rid, []).append(fb)

    # Build counts for comment badges
    for rid, fbs in feedbacks_by_report.items():
        feedback_counts_by_report[rid] = len(fbs)

    updates_by_report = {}
    for upd in all_updates:
        rid = upd.get('report_id')
        if rid is None:
            continue
        updates_by_report.setdefault(rid, []).append(upd)

    upvote_counts = {}
    user_upvoted = set()

    account_id = session.get('user_id')

    if report_ids:
        # Get upvote counts
        upvote_query = f"SELECT report_id, COUNT(*) as count FROM upvotes WHERE report_id IN ({placeholders}) GROUP BY report_id"
        cursor.execute(upvote_query, tuple(report_ids))
        for row in cursor.fetchall():
            upvote_counts[row['report_id']] = row['count']
            
        # Get user's upvotes
        if account_id:
            user_upvote_query = f"SELECT report_id FROM upvotes WHERE account_id = %s AND report_id IN ({placeholders})"
            cursor.execute(user_upvote_query, (account_id,) + tuple(report_ids))
            for row in cursor.fetchall():
                user_upvoted.add(row['report_id'])

    # Fetch real user data for sidebar
    cursor.execute(
        "SELECT first_name, last_name, profile_photo, account_status FROM accounts WHERE account_id = %s",
        (account_id,)
    )
    user_data = cursor.fetchone()
    con.close()

    return render_template(
        'file_concern.html',
        concerns=all_concerns,
        media_by_report=media_by_report,
        media_counts_by_report=media_counts_by_report,
        updates_by_report=updates_by_report,
        feedbacks_by_report=feedbacks_by_report,
        feedback_counts_by_report=feedback_counts_by_report,
        upvote_counts=upvote_counts,
        user_upvoted=user_upvoted,
        user=user_data
    )
# ===== API: Fetch report details by public_key (UUID) =====
@app.route('/api/report/<public_key>')
def api_report_by_key(public_key):
    """Fetch full report details by its UUID public_key for the modal viewer"""
    if 'user_id' not in session:
        from flask import jsonify
        return jsonify({'success': False, 'message': 'Not logged in'}), 401
    
    con = connect_db()
    cursor = con.cursor(dictionary=True)
    cursor.execute("""
        SELECT r.report_id, r.public_key, r.title, r.description, r.category, r.status,
               r.image_url, r.created_at,
               a.first_name, a.last_name, a.profile_photo,
               l.locname, l.x, l.y
        FROM reports r
        JOIN accounts a ON r.account_id = a.account_id
        LEFT JOIN location l ON r.location_id = l.location_id
        WHERE r.public_key = %s
    """, (public_key,))
    report = cursor.fetchone()
    
    if not report:
        con.close()
        from flask import jsonify
        return jsonify({'success': False, 'message': 'Report not found'}), 404
    
    # Sanitize bytes
    for k, v in list(report.items()):
        if isinstance(v, (bytes, bytearray)):
            report[k] = v.decode('utf-8', errors='ignore') if v else ""
        elif isinstance(v, datetime):
            report[k] = v.strftime('%B %d, %Y - %I:%M %p')
    
    # Get upvote count
    cursor.execute("SELECT COUNT(*) as count FROM upvotes WHERE report_id = %s", (report['report_id'],))
    report['upvote_count'] = cursor.fetchone()['count']
    
    # Get comment count
    cursor.execute("SELECT COUNT(*) as count FROM feedbacks WHERE report_id = %s", (report['report_id'],))
    report['comment_count'] = cursor.fetchone()['count']
    
    # Get recent comments
    cursor.execute("""
        SELECT f.feedback, f.uploaded_at,
               CONCAT(a.first_name, ' ', a.last_name) AS fullname,
               a.profile_photo
        FROM feedbacks f
        LEFT JOIN accounts a ON a.account_id = f.account_id
        WHERE f.report_id = %s
        ORDER BY f.uploaded_at DESC
        LIMIT 10
    """, (report['report_id'],))
    comments = cursor.fetchall()
    for c in comments:
        for k, v in list(c.items()):
            if isinstance(v, (bytes, bytearray)):
                c[k] = v.decode('utf-8', errors='ignore') if v else ""
            elif isinstance(v, datetime):
                c[k] = v.strftime('%b %d, %Y • %I:%M %p')
    
    report['comments'] = comments
    
    con.close()
    from flask import jsonify
    return jsonify({'success': True, 'report': report})


@app.route('/api/upvote', methods=['POST'])
def api_upvote():
    if 'user_id' not in session:
        return {'success': False, 'message': 'Not logged in'}, 401
    
    data = request.get_json()
    report_id = data.get('report_id')
    user_id = session.get('user_id')
    
    con = connect_db()
    cursor = con.cursor(dictionary=True)
    
    # Check if user is verified
    cursor.execute("SELECT account_status FROM accounts WHERE account_id = %s", (user_id,))
    user = cursor.fetchone()
    if not user or user.get('account_status') != 'Verified':
        con.close()
        return {'success': False, 'message': 'Account pending verification'}, 403
        
    
    # Check if already upvoted
    cursor.execute("SELECT upvote_id FROM upvotes WHERE report_id=%s AND account_id=%s", (report_id, user_id))
    existing = cursor.fetchone()
    
    if existing:
        cursor.execute("DELETE FROM upvotes WHERE upvote_id=%s", (existing['upvote_id'],))
        action = 'removed'
    else:
        cursor.execute("INSERT INTO upvotes (report_id, account_id) VALUES (%s, %s)", (report_id, user_id))
        action = 'added'
        
    con.commit()
    
    cursor.execute("SELECT COUNT(*) as count FROM upvotes WHERE report_id=%s", (report_id,))
    new_count = cursor.fetchone()['count']
    con.close()
    
    return {'success': True, 'action': action, 'count': new_count}

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        con = connect_db()
        cursor = con.cursor()

        try:
            data = request.form

            email = data.get('e', '').strip()
            phone = data.get('pn', '').strip()
            password = data.get('pass', '')
            conpass = data.get('conpass', '')

            if len(password) < 8:
                return render_template(
                    'register.html',
                    error="Password must be at least 8 characters long"
                )

            if password != conpass:
                return render_template(
                    'register.html',
                    error="Passwords do not match"
                )

            if phone:
                phone = phone.replace(' ', '')

                if phone.startswith('+63'):
                    phone = '0' + phone[3:]

                import re

                if not re.fullmatch(r'^09\d{9}$', phone):
                    return render_template(
                        'register.html',
                        error="Invalid Philippine mobile number format"
                    )

            if not email and not phone:
                return render_template(
                    'register.html',
                    error="Enter email or phone number"
                )

            conditions = []
            check_values = []

            if email:
                conditions.append("email_address = %s")
                check_values.append(email)

            if phone:
                conditions.append("phone_number = %s")
                check_values.append(phone)

            check_query = f"""
                SELECT email_address, phone_number, otp_verified
                FROM registration
                WHERE {' OR '.join(conditions)}
            """

            cursor.execute(check_query, tuple(check_values))
            existing_user = cursor.fetchone()
            
            
            if existing_user:
                existing_email, existing_phone, existing_verified = existing_user

                if existing_verified != 'Y':
                    session['otp_verified'] = False

                    otp_result = send_otp_sms(existing_phone)

                    if not otp_result.get('success'):
                        return render_template(
                            'register.html',
                            error="OTP could not be sent. Please try again."
                        )

                    return redirect(url_for('otpverify'))

                if email and existing_email == email:
                    return render_template(
                        'register.html',
                        error="Email address is already in use"
                    )

                if phone and existing_phone == phone:
                    return render_template(
                        'register.html',
                        error="Phone number is already in use"
                    )

                return render_template(
                    'register.html',
                    error="Account already exists"
                )

            hashed_pw = hash_password_bcrypt(password)

            query = """
                INSERT INTO registration
                (email_address, phone_number, password, otp_verified)
                VALUES (%s, %s, %s, 'N')
            """
            values = (
                email if email else None,
                phone if phone else None,
                hashed_pw
            )

            cursor.execute(query, values)

            new_reg_id = cursor.lastrowid

            con.commit()

            session['reg_id'] = new_reg_id
            session['otp_verified'] = False

            # Send OTP
            otp_result = send_otp_sms(phone)

            if not otp_result.get('success'):
                print("OTP ERROR:", otp_result.get('error'))

                return render_template(
                    'register.html',
                    error="Registration successful, but OTP could not be sent. Please try again."
                )

            return redirect(url_for('otpverify'))

        except Exception as e:
            con.rollback()

            print("REGISTRATION ERROR:", e)

            return render_template(
                'register.html',
                error=f"Registration failed: {str(e)}"
            )

        finally:
            cursor.close()
            con.close()

    return render_template('register.html')

@app.route('/otpverify', methods=['GET', 'POST'])
def otpverify():

    if 'reg_id' not in session:
        return redirect(url_for('register'))

    if 'otp' not in session:
        return redirect(url_for('register'))

    if request.method == 'POST':

        user_input_code = request.form.get('otp_code', '').strip()
        expected_code = session.get('otp')
        expires_at = session.get('otp_expires_at')

        if not user_input_code:
            return render_template(
                'otp.html',
                error="Please enter the verification code."
            )

        if not expires_at:
            session.pop('otp', None)
            session.pop('otp_expires_at', None)

            return render_template(
                'otp.html',
                error="OTP is no longer valid. Please request a new code."
            )

        if time.time() > expires_at:
            session.pop('otp', None)
            session.pop('otp_expires_at', None)

            return render_template(
                'otp.html',
                error="OTP has expired. Please request a new code."
            )

        if user_input_code == expected_code:

            session.pop('otp', None)
            session.pop('otp_expires_at', None)

            session['otp_verified'] = True

            return redirect(url_for('cd'))

        return render_template(
            'otp.html',
            error="Invalid verification code. Please try again."
        )

    return render_template('otp.html')
    

@app.route('/complete_details', methods=['GET', 'POST'])
def cd():
    con = connect_db()
    cursor = con.cursor(buffered=True)
    reg_id = session.get('reg_id') or request.args.get('reg_id') or request.form.get('reg_id')
    
    if not reg_id:
        return redirect(url_for('login'))
    
    # Fetch reg_user data for display
    cred = "SELECT email_address, phone_number FROM registration WHERE registration_id = %s"
    cursor.execute(cred, (reg_id,))
    reg_user = cursor.fetchone()
    
    if request.method == 'GET':
        if reg_user:
            u = reg_user[0] or reg_user[1]
            return render_template('complete_details.html', u=u, reg_id=reg_id, email=reg_user[0] or '', phone=reg_user[1] or '')
        return redirect(url_for('login'))
    
    else:  # POST
        data = request.form
        
        # Handle cropped profile photo base64
        cropped_image = data.get('cropped_image')
        pp_path = None
        if cropped_image and cropped_image.startswith('data:image'):
            import base64
            header, encoded = cropped_image.split(",", 1)
            file_ext = header.split(";")[0].split("/")[1]
            filename = f"profile_{reg_id}_{int(datetime.now().timestamp())}.{file_ext}"
            os.makedirs('static/uploads', exist_ok=True)
            pp_path = f"static/uploads/{filename}"
            with open(pp_path, "wb") as fh:
                fh.write(base64.b64decode(encoded))
              
        # Insert into accounts with 'Incomplete' status since ID is not yet uploaded
        cursor.execute("""
            INSERT INTO accounts (account_id, first_name, last_name, middle_name, dob, profile_photo, house_no, street_name, province, city, barangay, account_status) 
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            reg_id, data.get('fname'), data.get('lname'), data.get('mname'), data.get('dob'), pp_path,
            data.get('house_no'), data.get('street_name'), data.get('province_name'), data.get('city_name'), data.get('barangay_name'),
            'Incomplete'
        ))
        
        cursor.execute("SELECT account_id FROM accounts WHERE account_id = %s", (reg_id,))
        newid = cursor.fetchone()[0]
        
        # Add contacts for email and primary phone
        cursor.execute(
            "INSERT INTO contacts (account_id, email, phone_number) VALUES (%s, %s, %s)", 
            (newid, reg_user[0] if reg_user[0] else None, reg_user[1] if reg_user[1] else None)
        )
                
        cursor.execute("""
            INSERT INTO password (password) 
            SELECT password FROM registration WHERE registration_id = %s
        """, (reg_id,))
        
        cursor.execute("SELECT MAX(pass_id) FROM password")
        new_pass_id = cursor.fetchone()[0]
        
        # Ensure credentials and user_role exist for ALL usercreds related to this account
        cursor.execute("""
            SELECT u.user_id FROM usercreds u
            JOIN contacts c ON u.contact_id = c.contact_id
            WHERE c.account_id = %s
        """, (newid,))
        user_ids = [row[0] for row in cursor.fetchall()]
        
        for uid in user_ids:
            cursor.execute("SELECT cred_id, pass_id FROM credentials WHERE user_id = %s", (uid,))
            row = cursor.fetchone()
            if not row:
                cursor.execute("INSERT INTO credentials (user_id, pass_id, is_locked) VALUES (%s, %s, 'N')", (uid, new_pass_id))
            elif row[1] is None:
                cursor.execute("UPDATE credentials SET pass_id = %s WHERE cred_id = %s", (new_pass_id, row[0]))
            
            cursor.execute("SELECT * FROM user_role WHERE user_id = %s", (uid,))
            if not cursor.fetchone():
                cursor.execute("INSERT INTO user_role (user_id, role_id) VALUES (%s, %s)", (uid, 1))
        
        # Finally delete registration record
        cursor.execute("DELETE FROM registration WHERE registration_id = %s", (reg_id,))
        con.commit()
        con.close()
        
        session['pending_user_id'] = newid # Pass to next step
        session.pop('reg_id', None)
        return redirect(url_for('id_verification'))

@app.route('/id_verification', methods=['GET', 'POST'])
def id_verification():
    if request.method == 'GET':
        user_id = session.get('pending_user_id')
        if not user_id:
            return redirect(url_for('login'))
        return render_template('id_verification.html', user_id=user_id)
    
    else: # POST
        user_id = request.form.get('user_id')
        if not user_id:
            return redirect(url_for('login'))
            
        # --- Valid ID (required) ---
        id_proof = request.files.get('id_proof')
        id_type = request.form.get('id_type', '').strip() or None
        id_proof_path = None
        if id_proof and id_proof.filename:
            os.makedirs('static/uploads/id_proofs', exist_ok=True)
            id_filename = f"id_{user_id}_{int(datetime.now().timestamp())}_{id_proof.filename}"
            id_proof_path = f"static/uploads/id_proofs/{id_filename}"
            id_proof.save(id_proof_path)
        
        if not id_proof_path or not id_type:
            return render_template('id_verification.html', user_id=user_id, error="Please select an ID type and upload your valid ID")

        # --- Selfie with ID (required) ---
        selfie = request.files.get('selfie')
        selfie_path = None
        if selfie and selfie.filename:
            os.makedirs('static/uploads/id_proofs', exist_ok=True)
            selfie_filename = f"selfie_{user_id}_{int(datetime.now().timestamp())}_{selfie.filename}"
            selfie_path = f"static/uploads/id_proofs/{selfie_filename}"
            selfie.save(selfie_path)

        if not selfie_path:
            return render_template('id_verification.html', user_id=user_id, error="Please upload a selfie holding your ID")
        
        con = connect_db()
        cursor = con.cursor(buffered=True)
        cursor.execute("""
            UPDATE accounts 
            SET id_proof_path = %s,
                id_type = %s,
                selfie_path = %s,
                account_status = 'Pending'
            WHERE account_id = %s
        """, (id_proof_path, id_type, selfie_path, user_id))
        con.commit()
        
        # Auto login the user
        cursor.execute("SELECT first_name FROM accounts WHERE account_id = %s", (user_id,))
        user_name = cursor.fetchone()[0]
        con.close()
        
        session['user_id'] = user_id
        session['user_name'] = user_name
        session['user_role'] = 'resident'
        session.pop('pending_user_id', None)
        
        return redirect(url_for('dashboard'))

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/lgu_announcements', methods=['GET', 'POST'])
def lgu_announcements():
    if 'user_id' not in session or session.get('user_role') != 'lgu':
        return redirect(url_for('login'))
    
    con = connect_db()
    cursor = con.cursor(dictionary=True)
    
    if request.method == 'POST':
        category = request.form.get('category', '').strip()
        title = request.form.get('title', '').strip()
        content = request.form.get('content', '').strip()
        account_id = session.get('user_id')
        
        if title and content:
            cursor.execute(
                "INSERT INTO announcements (account_id, category, ann_title, ann_desc, submitted_at) VALUES (%s, %s, %s, %s, %s)",
                (account_id, category, title, content, datetime.now())
            )
            con.commit()
            
        return redirect(url_for('lgu_announcements'))
        
    cursor.execute("SELECT first_name, last_name, profile_photo FROM accounts WHERE account_id = %s", (session['user_id'],))
    user_rows = cursor.fetchall()
    user_data = user_rows[0] if user_rows else None
    
    cursor.execute("SELECT * FROM announcements ORDER BY submitted_at DESC")
    announcements = cursor.fetchall()
    for ann in announcements:
        if ann['submitted_at']:
            ann['formatted_date'] = ann['submitted_at'].strftime('%B %d, %Y')
        else:
            ann['formatted_date'] = ''
            
    con.close()
    
    return render_template('lgu_announcements.html', user=user_data, announcements=announcements)

@app.route('/delete_announcement/<int:ann_id>', methods=['POST'])
def delete_announcement(ann_id):
    if 'user_id' not in session or session.get('user_role') != 'lgu':
        return redirect(url_for('login'))
        
    con = connect_db()
    cursor = con.cursor()
    cursor.execute("DELETE FROM announcements WHERE ann_id = %s", (ann_id,))
    con.commit()
    con.close()
    return redirect(url_for('lgu_announcements'))

@app.route('/barangay_map')
def barangay_map():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    con = connect_db()
    cursor = con.cursor(dictionary=True)
    cursor.execute(
        "SELECT first_name, last_name, profile_photo FROM accounts WHERE account_id = %s",
        (session['user_id'],)
    )
    user_rows = cursor.fetchall()
    user_data = user_rows[0] if user_rows else None

    # For leaflet pins: return rich report details + locname + x/y (location coordinates)
    rep = """
        SELECT 
            r.report_id, 
            r.title, 
            r.category, 
            r.description, 
            r.image_url, 
            r.status, 
            DATE_FORMAT(r.created_at, '%b %d, %Y • %h:%i %p') AS created_at_str,
            l.locname, 
            l.x, 
            l.y, 
            CONCAT(a.first_name, ' ', a.last_name) AS reporter_name,
            a.profile_photo AS reporter_photo
        FROM reports r
        JOIN location l ON r.location_id = l.location_id
        LEFT JOIN accounts a ON r.account_id = a.account_id
        WHERE l.x IS NOT NULL AND l.y IS NOT NULL
        ORDER BY r.created_at DESC
    """
    cursor.execute(rep)
    rloc = [dict(r) for r in cursor.fetchall()]

    # Sanitize any bytes/None for JSON serialization (dict rows)
    for r in rloc:
        for k, v in list(r.items()):
            if isinstance(v, (bytes, bytearray)):
                r[k] = v.decode('utf-8', errors='ignore') if v else ""
            elif v is None:
                r[k] = ""

    # DEBUG: verify DB data for leaflet pins
    print('[/barangay_map] rloc rows:', len(rloc))
    if rloc:
        print('[/barangay_map] first rloc row:', rloc[0])

    con.close()

    return render_template('barangay_map.html', user=user_data, rloc=rloc)


@app.route('/lgu_verifications')
def lgu_verifications():
    if 'user_id' not in session or session.get('user_role') != 'lgu':
        return redirect(url_for('login'))
        
    con = connect_db()
    cursor = con.cursor(dictionary=True)
    
    # Get LGU user info
    cursor.execute("SELECT first_name, last_name, profile_photo FROM accounts WHERE account_id = %s", (session['user_id'],))
    user_rows = cursor.fetchall()
    user_data = user_rows[0] if user_rows else None
    
    # Get pending users
    cursor.execute("""
        SELECT a.account_id, a.first_name, a.last_name, a.profile_photo,
               a.id_proof_path, a.id_proof_path,
               c.email, c.phone_number
        FROM accounts a
        LEFT JOIN contacts c ON a.account_id = c.account_id
        WHERE a.account_status = 'Pending'
    """)
    pending_users = cursor.fetchall()
    con.close()
    
    return render_template('lgu_verifications.html', user=user_data, pending_users=pending_users)

@app.route('/verify_user', methods=['POST'])
def verify_user():
    if 'user_id' not in session or session.get('user_role') != 'lgu':
        return redirect(url_for('login'))
        
    account_id = request.form.get('account_id')
    action = request.form.get('action') # 'accept' or 'reject'
    
    if account_id and action:
        con = connect_db()
        cursor = con.cursor()
        new_status = 'Verified' if action == 'accept' else 'Rejected'
        cursor.execute("UPDATE accounts SET account_status = %s WHERE account_id = %s", (new_status, account_id))
        con.commit()
        con.close()
        
    return redirect(url_for('lgu_verifications'))

@app.route('/lgu_analytics')
def lgu_analytics():
    if 'user_id' not in session or session.get('user_role') != 'lgu':
        return redirect(url_for('login'))

    con = connect_db()
    cursor = con.cursor(dictionary=True)
    account_id = session.get('user_id')

    # --- User profile ---
    cursor.execute(
        "SELECT first_name, last_name, profile_photo FROM accounts WHERE account_id = %s",
        (account_id,)
    )
    user_rows = cursor.fetchall()
    user_data = user_rows[0] if user_rows else None

    # --- Overall counts ---
    cursor.execute("SELECT COUNT(*) AS total FROM reports")
    total = cursor.fetchone()['total'] or 0

    cursor.execute("SELECT COUNT(*) AS cnt FROM reports WHERE LOWER(status) = 'pending'")
    pending = cursor.fetchone()['cnt'] or 0

    cursor.execute("SELECT COUNT(*) AS cnt FROM reports WHERE LOWER(status) IN ('in progress','inprogress','in_progress')")
    in_progress = cursor.fetchone()['cnt'] or 0

    cursor.execute("SELECT COUNT(*) AS cnt FROM reports WHERE LOWER(status) = 'resolved'")
    resolved = cursor.fetchone()['cnt'] or 0

    cursor.execute("SELECT COUNT(*) AS cnt FROM reports WHERE LOWER(status) IN ('denied', 'rejected', 'declined', 'cancelled', 'spam', 'invalid')")
    denied = cursor.fetchone()['cnt'] or 0

    cursor.execute("SELECT COUNT(*) AS cnt FROM reports WHERE DATE(created_at) = date('now', 'localtime')")
    today_count = dict(cursor.fetchone())['cnt'] or 0

    cursor.execute("SELECT COUNT(*) AS cnt FROM reports WHERE strftime('%Y-%W', created_at)=strftime('%Y-%W', date('now', 'localtime'))")
    this_week = dict(cursor.fetchone())['cnt'] or 0

    # This month / last month
    cursor.execute("""
        SELECT COUNT(*) AS cnt FROM reports
        WHERE CAST(strftime('%Y', created_at) AS INTEGER) = CAST(strftime('%Y', date('now', 'localtime')) AS INTEGER)
          AND CAST(strftime('%m', created_at) AS INTEGER) = CAST(strftime('%m', date('now', 'localtime')) AS INTEGER)
    """)
    this_month = dict(cursor.fetchone())['cnt'] or 0

    cursor.execute("""
        SELECT COUNT(*) AS cnt FROM reports
        WHERE CAST(strftime('%Y', created_at) AS INTEGER) = CAST(strftime('%Y', date('now', 'localtime', '-1 month')) AS INTEGER)
          AND CAST(strftime('%m', created_at) AS INTEGER) = CAST(strftime('%m', date('now', 'localtime', '-1 month')) AS INTEGER)
    """)
    last_month = dict(cursor.fetchone())['cnt'] or 0

    # --- Engagement ---
    cursor.execute("SELECT COUNT(*) AS cnt FROM upvotes")
    total_upvotes = cursor.fetchone()['cnt'] or 0

    cursor.execute("SELECT COUNT(*) AS cnt FROM feedbacks")
    total_comments = cursor.fetchone()['cnt'] or 0

    cursor.execute("SELECT COUNT(DISTINCT account_id) AS cnt FROM reports")
    total_residents = cursor.fetchone()['cnt'] or 0

    # --- Monthly trend (last 6 months) ---
    cursor.execute("""
        SELECT strftime('%b %Y', created_at) AS month_label,
               CAST(strftime('%Y', created_at) AS INTEGER) AS yr,
               CAST(strftime('%m', created_at) AS INTEGER) AS mo,
               COUNT(*) AS cnt,
               SUM(CASE WHEN LOWER(status)='resolved' THEN 1 ELSE 0 END) AS resolved_cnt
        FROM reports
        WHERE created_at >= date('now', 'localtime', '-6 months')
        GROUP BY yr, mo, month_label ORDER BY yr ASC, mo ASC
    """)
    trend_rows = cursor.fetchall()
    monthly_labels   = [dict(r)['month_label']   for r in trend_rows]
    monthly_counts   = [dict(r)['cnt']           for r in trend_rows]
    monthly_resolved = [dict(r)['resolved_cnt']  for r in trend_rows]

    # --- Day-of-week distribution ---
    cursor.execute("""
        SELECT CAST(strftime('%w', created_at) AS INTEGER) AS dow, COUNT(*) AS cnt
        FROM reports GROUP BY dow ORDER BY dow
    """)
    dow_map = {dict(r)['dow']: dict(r)['cnt'] for r in cursor.fetchall()}
    dow_labels = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat']
    dow_counts = [dow_map.get(i, 0) for i in range(1, 8)]

    # --- Week-of-month distribution (current month) ---
    cursor.execute("""
        SELECT CAST(CEILING(CAST(strftime('%d', created_at) AS INTEGER) / 7.0) AS INTEGER) AS week_num,
               COUNT(*) AS cnt
        FROM reports
        WHERE CAST(strftime('%Y', created_at) AS INTEGER) = CAST(strftime('%Y', date('now', 'localtime')) AS INTEGER)
          AND CAST(strftime('%m', created_at) AS INTEGER) = CAST(strftime('%m', date('now', 'localtime')) AS INTEGER)
        GROUP BY week_num ORDER BY week_num
    """)
    wom_map = {dict(r)['week_num']: dict(r)['cnt'] for r in cursor.fetchall() if dict(r).get('week_num')}
    wom_labels = ['Week 1', 'Week 2', 'Week 3', 'Week 4', 'Week 5']
    wom_counts = [wom_map.get(i, 0) for i in range(1, 6)]

    # --- Category breakdown ---
    cat_colors = {
        'road':       '#fbbf24',
        'sanitation': '#34d399',
        'safety':     '#f87171',
        'flooding':   '#60a5fa',
        'lighting':   '#a78bfa',
        'other':      '#94a3b8',
    }
    cursor.execute("""
        SELECT LOWER(REPLACE(category,' ','')) AS cat_key, category, COUNT(*) AS cnt,
               SUM(CASE WHEN LOWER(status)='resolved' THEN 1 ELSE 0 END) AS res_cnt
        FROM reports GROUP BY cat_key, category ORDER BY cnt DESC
    """)
    cat_rows = cursor.fetchall()
    category_data = [
        {'name': r['category'], 'count': r['cnt'], 'resolved': r['res_cnt'],
         'color': cat_colors.get(r['cat_key'], '#8b0000')}
        for r in cat_rows
    ]
    category_names  = [c['name']  for c in category_data]
    category_counts = [c['count'] for c in category_data]

    # --- Top locations ---
    cursor.execute("""
        SELECT l.locname, COUNT(*) AS cnt
        FROM reports r
        JOIN location l ON r.location_id = l.location_id
        WHERE l.locname IS NOT NULL AND l.locname != ''
        GROUP BY l.locname ORDER BY cnt DESC LIMIT 7
    """)
    top_locations = [
        {'locname': r['locname'], 'count': r['cnt']}
        for r in cursor.fetchall()
    ]

    # --- Top reporters ---
    cursor.execute("""
        SELECT a.first_name, a.last_name, a.profile_photo, COUNT(*) AS cnt
        FROM reports r JOIN accounts a ON r.account_id=a.account_id
        GROUP BY r.account_id,a.first_name,a.last_name,a.profile_photo
        ORDER BY cnt DESC LIMIT 5
    """)
    top_reporters = [
        {'name': f"{r['first_name']} {r['last_name']}",
         'count': r['cnt'],
         'initial': (r['first_name'] or 'U')[0].upper(),
         'photo': r['profile_photo']}
        for r in cursor.fetchall()
    ]

    # --- Oldest pending ---
    cursor.execute("""
        SELECT r.title, r.category, r.created_at, a.first_name, a.last_name,
               CAST(julianday(date('now', 'localtime')) - julianday(r.created_at) AS INTEGER) AS days_old
        FROM reports r JOIN accounts a ON r.account_id=a.account_id
        WHERE LOWER(r.status)='pending' ORDER BY r.created_at ASC LIMIT 5
    """)
    oldest_pending = [dict(r) for r in cursor.fetchall()]

    # --- Recent 10 reports ---
    cursor.execute("""
        SELECT r.report_id, r.title, r.category, r.status, r.created_at,
               a.first_name, a.last_name, l.locname
        FROM reports r
        JOIN accounts a ON r.account_id = a.account_id
        LEFT JOIN location l ON r.location_id = l.location_id
        ORDER BY r.created_at DESC LIMIT 10
    """)
    recent_reports = cursor.fetchall()

    # --- Random report sample (from the past week, or fallback to any random report) ---
    cursor.execute("""
        SELECT r.report_id, r.title, r.category, r.description, r.status, r.created_at,
               a.first_name, a.last_name, l.locname
        FROM reports r
        JOIN accounts a ON r.account_id = a.account_id
        LEFT JOIN location l ON r.location_id = l.location_id
        WHERE r.created_at >= date('now', 'localtime', '-7 days')
        ORDER BY RANDOM() LIMIT 1
    """)
    sample_report = cursor.fetchone()
    if sample_report is None:
        cursor.execute("""
            SELECT r.report_id, r.title, r.category, r.description, r.status, r.created_at,
                   a.first_name, a.last_name, l.locname
            FROM reports r
            JOIN accounts a ON r.account_id = a.account_id
            LEFT JOIN location l ON r.location_id = l.location_id
            ORDER BY RANDOM() LIMIT 1
        """)
        sample_report = cursor.fetchone()
    sample_report = dict(sample_report) if sample_report else None

    con.close()

    stats = {
        'total': total, 'pending': pending,
        'in_progress': in_progress, 'resolved': resolved,
        'denied': denied,
        'today': today_count, 'this_week': this_week,
        'this_month': this_month, 'last_month': last_month,
        'total_upvotes': total_upvotes,
        'total_comments': total_comments,
        'total_residents': total_residents,
    }

    # Generate monthly volume summary and sample report insight via offline AI dictionary
    monthly_summary, sample_summary, status_summary, location_summary, category_summary = generate_monthly_analytics_summary(
        monthly_labels, monthly_counts, monthly_resolved, sample_report, stats, top_locations, category_data
    )

    return render_template(
        'lgu_analytics.html',
        user=user_data,
        stats=stats,
        monthly_labels=monthly_labels,
        monthly_counts=monthly_counts,
        monthly_resolved=monthly_resolved,
        dow_labels=dow_labels,
        dow_counts=dow_counts,
        wom_labels=wom_labels,
        wom_counts=wom_counts,
        category_data=category_data,
        category_names=category_names,
        category_counts=category_counts,
        top_locations=top_locations,
        top_reporters=top_reporters,
        oldest_pending=oldest_pending,
        recent_reports=recent_reports,
        sample_report=sample_report,
        monthly_summary=monthly_summary,
        sample_summary=sample_summary,
        status_summary=status_summary,
        location_summary=location_summary,
        category_summary=category_summary
    )



@app.route('/lgu_map')
def lgu_map():
    if 'user_id' not in session or session.get('user_role') != 'lgu':
        return redirect(url_for('login'))

    con = connect_db()
    cursor = con.cursor(dictionary=True)

    cursor.execute(
        "SELECT first_name, last_name, profile_photo FROM accounts WHERE account_id = %s",
        (session['user_id'],)
    )
    user_rows = cursor.fetchall()
    user_data = user_rows[0] if user_rows else None

    # For leaflet pins: return rich report details + locname + x/y (location coordinates)
    rep = """
        SELECT 
            r.report_id, 
            r.title, 
            r.category, 
            r.description, 
            r.image_url, 
            r.status, 
            DATE_FORMAT(r.created_at, '%b %d, %Y • %h:%i %p') AS created_at_str,
            l.locname, 
            l.x, 
            l.y, 
            CONCAT(a.first_name, ' ', a.last_name) AS reporter_name,
            a.profile_photo AS reporter_photo
        FROM reports r
        JOIN location l ON r.location_id = l.location_id
        LEFT JOIN accounts a ON r.account_id = a.account_id
        WHERE l.x IS NOT NULL AND l.y IS NOT NULL
        ORDER BY r.created_at DESC
    """
    cursor.execute(rep)
    rloc = [dict(r) for r in cursor.fetchall()]

    # Sanitize any bytes/None for JSON serialization (dict rows)
    for r in rloc:
        for k, v in list(r.items()):
            if isinstance(v, (bytes, bytearray)):
                r[k] = v.decode('utf-8', errors='ignore') if v else ""
            elif v is None:
                r[k] = ""

    con.close()

    return render_template('lgu_map.html', user=user_data, rloc=rloc)




@app.route('/announcements')
def announcements():
    if 'user_id' not in session:
        return redirect(url_for('login'))
        
    con = connect_db()
    cursor = con.cursor(dictionary=True)
    cursor.execute("SELECT first_name, last_name, profile_photo FROM accounts WHERE account_id = %s", (session['user_id'],))
    user_rows = cursor.fetchall()
    user_data = user_rows[0] if user_rows else None
    
    cursor.execute("""
        SELECT a.*, acc.first_name, acc.last_name 
        FROM announcements a
        LEFT JOIN accounts acc ON a.account_id = acc.account_id
        ORDER BY a.submitted_at DESC
    """)
    db_anns = cursor.fetchall()
    
    # Format for JSON
    anns_list = []
    emoji_map = {
        'General': '📢',
        'Health': '🏥',
        'Infrastructure': '🛣️',
        'Events': '🎉',
        'Safety': '🚨'
    }
    
    for row in db_anns:
        cat = row['category'] or 'General'
        emoji = emoji_map.get(cat, '📢')
        author = f"{row['first_name']} {row['last_name']}" if row['first_name'] else "Barangay Picaleon"
        date_str = row['submitted_at'].strftime('%B %d, %Y') if row['submitted_at'] else ''
        
        # Build JSON item
        anns_list.append({
            'cat': cat.lower(),
            'emoji': emoji,
            'tag': f"{emoji} {cat}",
            'title': row['ann_title'],
            'excerpt': (row['ann_desc'][:100] + '...') if row['ann_desc'] and len(row['ann_desc']) > 100 else (row['ann_desc'] or ''),
            'date': date_str,
            'author': author,
            'views': '-',
            'content': f"<p>{row['ann_desc']}</p>"
        })
        
    con.close()
    
    import json
    anns_json = json.dumps(anns_list)
    
    return render_template('announcements.html', user=user_data, announcements_json=anns_json)

if __name__ == '__main__':
    app.run(host="0.0.0.0", port=5000, debug=True)

