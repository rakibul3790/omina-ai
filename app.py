from flask import Flask, render_template, request, jsonify, redirect, url_for, session, make_response
import os, re, json as _json, datetime as _dt, base64, io
from collections import Counter
from groq import Groq
import firebase_admin
from firebase_admin import credentials, auth, firestore
try:
    import requests as _requests
except ImportError:
    _requests = None

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "omina-secret-2026")

# ── Persistent Session — 30 দিন login থাকবে ────────────────
app.config["PERMANENT_SESSION_LIFETIME"] = _dt.timedelta(days=30)
app.config["SESSION_COOKIE_SECURE"]      = True   # HTTPS only
app.config["SESSION_COOKIE_HTTPONLY"]    = True   # JS access বন্ধ (XSS protection)
app.config["SESSION_COOKIE_SAMESITE"]    = "Lax"  # CSRF protection

# ── Firebase Admin Init ──────────────────────────────────────
# সব sensitive data environment variables থেকে নেওয়া হচ্ছে
firebase_config = {
    "type":                        "service_account",
    "project_id":                  os.environ.get("FIREBASE_PROJECT_ID"),
    "private_key_id":              os.environ.get("FIREBASE_PRIVATE_KEY_ID"),
    "private_key":                 os.environ.get("FIREBASE_PRIVATE_KEY", "").replace("\\n", "\n"),
    "client_email":                os.environ.get("FIREBASE_CLIENT_EMAIL"),
    "client_id":                   os.environ.get("FIREBASE_CLIENT_ID"),
    "auth_uri":                    "https://accounts.google.com/o/oauth2/auth",
    "token_uri":                   "https://oauth2.googleapis.com/token",
    "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
    "client_x509_cert_url":        os.environ.get("FIREBASE_CLIENT_CERT_URL"),
}

cred = credentials.Certificate(firebase_config)
firebase_admin.initialize_app(cred)
db = firestore.client()

# ── Groq Client ─────────────────────────────────────────────
groq_client = Groq(api_key=os.environ.get("GROQ_API_KEY"))
GROQ_MODEL = "llama-3.3-70b-versatile"

# ── Fixed Instructions ───────────────────────────────────────
FIXED_INSTRUCTIONS = (
    "When asked to build a website, landing page, portfolio, or any web project "
    "WITHOUT specific design instructions: be CREATIVE — choose a unique color palette, "
    "modern layout, and impressive design on your own. Use Tailwind CSS via CDN. "
    "Make it visually stunning with gradients, animations, glassmorphism, or bold typography. "
    "Add realistic placeholder content and Font Awesome icons. "
    "Always produce complete, ready-to-run HTML in a single file. "
    "Never ask for design clarification — just build something impressive."
)

MOOD_MAP = {
    "friendly":     " Respond in a warm, friendly, conversational tone.",
    "professional": " Be concise and professional.",
    "creative":     " Be extra creative and expressive.",
    "funny":        " Add a touch of humor.",
    "serious":      " Be precise and serious, no fluff.",
}

# ── Helper: verify Firebase ID token ────────────────────────
def verify_token(id_token):
    try:
        decoded = auth.verify_id_token(id_token)
        return decoded
    except Exception:
        return None

def get_current_user():
    """Session থেকে user info নেওয়া"""
    if "uid" not in session:
        return None
    return {"uid": session["uid"], "name": session.get("user_name", "User"), "email": session.get("user_email", "")}

# ── Routes ───────────────────────────────────────────────────
@app.route("/")
def home():
    if "uid" not in session:
        return redirect(url_for("login_page"))
    return render_template("index.html",
        user_name=session.get("user_name", "User"),
        user_email=session.get("user_email", "")
    )

@app.route("/sw.js")
def service_worker():
    """Serve Service Worker from root scope"""
    from flask import send_from_directory, Response
    import os
    # Try templates folder first, then root
    sw_path = os.path.join(app.root_path, 'templates', 'sw.js')
    if not os.path.exists(sw_path):
        sw_path = os.path.join(app.root_path, 'sw.js')
    if os.path.exists(sw_path):
        with open(sw_path) as f:
            js = f.read()
        return Response(js, mimetype='application/javascript',
                       headers={'Service-Worker-Allowed': '/',
                                'Cache-Control': 'no-cache'})
    return Response('// SW not found', mimetype='application/javascript'), 404

@app.route("/manifest.json")
def manifest():
    """Serve PWA manifest"""
    from flask import send_from_directory
    import os, json as _json2
    mf_path = os.path.join(app.root_path, 'templates', 'manifest.json')
    if not os.path.exists(mf_path):
        mf_path = os.path.join(app.root_path, 'manifest.json')
    if os.path.exists(mf_path):
        with open(mf_path) as f:
            mf = _json2.load(f)
        from flask import jsonify as _jfy
        resp = _jfy(mf)
        resp.headers['Cache-Control'] = 'public, max-age=86400'
        return resp
    return jsonify({"error": "manifest not found"}), 404

@app.route("/login")
def login_page():
    if "uid" in session:
        return redirect(url_for("home"))
    return render_template("login.html")

# ── Firebase token verify & session set ─────────────────────
@app.route("/api/session", methods=["POST"])
def set_session():
    """Frontend থেকে Firebase ID token পাঠাবে, backend session সেট করবে"""
    data     = request.json
    id_token = data.get("idToken", "")
    decoded  = verify_token(id_token)
    if not decoded:
        return jsonify({"success": False, "message": "Invalid token."}), 401

    uid   = decoded["uid"]
    email = decoded.get("email", "")
    name  = decoded.get("name", email.split("@")[0])

    # Firestore এ user document তৈরি/আপডেট
    user_ref = db.collection("users").document(uid)
    user_doc = user_ref.get()
    if not user_doc.exists:
        user_ref.set({
            "name":      name,
            "email":     email,
            "plan":      "free",
            "joined":    _dt.datetime.utcnow().isoformat(),
            "msg_count": 0,
            "bio":       "",
            "avatar":    decoded.get("picture", ""),
        })
    else:
        # Avatar আপডেট
        user_ref.update({"avatar": decoded.get("picture", "")})

    session.permanent     = True   # 30 দিন login থাকবে
    session["uid"]        = uid
    session["user_name"]  = user_doc.get("name") if user_doc.exists else name
    session["user_email"] = email

    return jsonify({"success": True, "name": session["user_name"]})

# ── Session check — Firebase token দিয়ে re-verify ──────────
@app.route("/api/verify-session", methods=["POST"])
def verify_session():
    """Page load এ Firebase token পাঠিয়ে session refresh করা"""
    data     = request.json or {}
    id_token = data.get("idToken", "")
    if not id_token:
        # Token নেই কিন্তু session আছে → still valid
        if "uid" in session:
            return jsonify({"success": True, "name": session.get("user_name", "User")})
        return jsonify({"success": False}), 401

    decoded = verify_token(id_token)
    if not decoded:
        session.clear()
        return jsonify({"success": False}), 401

    uid   = decoded["uid"]
    email = decoded.get("email", "")

    user_ref = db.collection("users").document(uid)
    user_doc = user_ref.get()
    name = user_doc.to_dict().get("name", decoded.get("name", email.split("@")[0])) if user_doc.exists else decoded.get("name", email.split("@")[0])

    session.permanent     = True
    session["uid"]        = uid
    session["user_name"]  = name
    session["user_email"] = email

    return jsonify({"success": True, "name": name})

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login_page"))

# ── Chat ─────────────────────────────────────────────────────
@app.route("/chat", methods=["POST"])
def chat():
    user = get_current_user()
    if not user:
        return jsonify({"reply": "Error: Not logged in."}), 401

    data       = request.json
    user_msg   = data.get("message", "")
    custom_sys = data.get("system_prompt", "").strip()
    lang_instr = data.get("lang_instruction", "").strip()
    history    = data.get("history", [])
    mood       = data.get("mood", "")
    file_names = data.get("file_names", [])
    persona_prompt = data.get("persona_prompt", "").strip()

    # Base system
    base_system = (
        f"You are Omina AI, a highly capable AI assistant. "
        f"The user's name is {user['name']}. "
        "Always format responses with markdown: headings, bullet points, **bold**, `code blocks`."
    )
    if custom_sys:
        base_system = custom_sys
    elif persona_prompt:
        base_system = persona_prompt

    if file_names:
        files_str = ", ".join(file_names)
        base_system += (
            f" The user has uploaded {len(file_names)} file(s): {files_str}. "
            "The file content is included in the user's message. Analyze it as requested."
        )

    if lang_instr:
        system_content = (
            lang_instr + " " + base_system + " " +
            FIXED_INSTRUCTIONS + MOOD_MAP.get(mood, "") +
            " " + lang_instr
        )
    else:
        system_content = base_system + " " + FIXED_INSTRUCTIONS + MOOD_MAP.get(mood, "")

    # History (last 12)
    messages = []
    for h in history[-12:]:
        role = "user" if h.get("role") == "user" else "assistant"
        messages.append({"role": role, "content": h.get("content", "")})
    messages.append({"role": "user", "content": user_msg})

    try:
        completion = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "system", "content": system_content}] + messages,
            max_tokens=2048,
            temperature=0.75,
            top_p=0.9,
        )
        bot_reply = completion.choices[0].message.content
        bot_reply = re.sub(r"<think>.*?</think>", "", bot_reply, flags=re.DOTALL).strip()

        # Firestore এ message count বাড়ানো
        db.collection("users").document(user["uid"]).update({
            "msg_count": firestore.Increment(1)
        })

    except Exception as e:
        bot_reply = f"Error: {str(e)}"

    return jsonify({"reply": bot_reply})

# ── Profile ──────────────────────────────────────────────────
@app.route("/api/profile", methods=["GET"])
def get_profile():
    user = get_current_user()
    if not user:
        return jsonify({"success": False, "message": "Not logged in."}), 401
    doc = db.collection("users").document(user["uid"]).get()
    if not doc.exists:
        return jsonify({"success": False, "message": "User not found."}), 404
    d = doc.to_dict()
    return jsonify({"success": True, "profile": {
        "name":      d.get("name", ""),
        "email":     d.get("email", ""),
        "avatar":    d.get("avatar", ""),
        "bio":       d.get("bio", ""),
        "joined":    d.get("joined", "Unknown"),
        "plan":      d.get("plan", "free"),
        "msg_count": d.get("msg_count", 0),
    }})

@app.route("/api/profile", methods=["PUT"])
def update_profile():
    user = get_current_user()
    if not user:
        return jsonify({"success": False, "message": "Not logged in."}), 401
    data    = request.json
    allowed = {k: data[k] for k in ["name", "bio", "avatar"] if k in data}
    db.collection("users").document(user["uid"]).update(allowed)
    if "name" in allowed:
        session["user_name"] = allowed["name"]
    return jsonify({"success": True, "message": "Profile updated."})

# ── Stats ────────────────────────────────────────────────────
@app.route("/api/stats", methods=["GET"])
def get_stats():
    user = get_current_user()
    if not user:
        return jsonify({"success": False, "message": "Not logged in."}), 401

    sessions_ref = db.collection("users").document(user["uid"]).collection("chat_sessions")
    sessions     = list(sessions_ref.stream())

    total_sessions = len(sessions)
    total_messages = 0
    user_messages  = 0
    day_counter    = Counter()

    for s in sessions:
        s_data = s.to_dict()
        msgs   = s_data.get("messages", [])
        total_messages += len(msgs)
        for m in msgs:
            if m.get("role") == "user":
                user_messages += 1
            ts = m.get("timestamp", "")
            if ts:
                day_counter[ts[:10]] += 1

    bot_messages    = total_messages - user_messages
    avg_per_session = round(total_messages / total_sessions, 1) if total_sessions else 0
    most_active_day = day_counter.most_common(1)[0] if day_counter else ("N/A", 0)

    return jsonify({"success": True, "stats": {
        "total_sessions":    total_sessions,
        "total_messages":    total_messages,
        "user_messages":     user_messages,
        "bot_messages":      bot_messages,
        "avg_per_session":   avg_per_session,
        "most_active_day":   most_active_day[0],
        "most_active_count": most_active_day[1],
    }})

# ── Intents Management ───────────────────────────────────────
def _load_intents():
    try:
        with open("intents.json", "r", encoding="utf-8") as f:
            return _json.load(f)
    except:
        return {"intents": []}

def _save_intents(data):
    with open("intents.json", "w", encoding="utf-8") as f:
        _json.dump(data, f, ensure_ascii=False, indent=2)

@app.route("/api/intents", methods=["GET"])
def list_intents():
    if not get_current_user():
        return jsonify({"success": False, "message": "Not logged in."}), 401
    data = _load_intents()
    return jsonify({"success": True, "intents": data.get("intents", []), "count": len(data.get("intents", []))})

@app.route("/api/intents", methods=["POST"])
def add_intent():
    if not get_current_user():
        return jsonify({"success": False, "message": "Not logged in."}), 401
    body      = request.json
    tag       = body.get("tag", "").strip()
    patterns  = body.get("patterns", [])
    responses = body.get("responses", [])
    if not tag or not patterns or not responses:
        return jsonify({"success": False, "message": "tag, patterns, and responses are required."}), 400
    data = _load_intents()
    for intent in data["intents"]:
        if intent["tag"] == tag:
            return jsonify({"success": False, "message": f"Tag '{tag}' already exists."}), 400
    data["intents"].append({"tag": tag, "patterns": patterns, "responses": responses})
    _save_intents(data)
    return jsonify({"success": True, "message": f"Intent '{tag}' added.", "total": len(data["intents"])})

@app.route("/api/intents/<tag>", methods=["DELETE"])
def delete_intent(tag):
    if not get_current_user():
        return jsonify({"success": False, "message": "Not logged in."}), 401
    data   = _load_intents()
    before = len(data["intents"])
    data["intents"] = [i for i in data["intents"] if i["tag"] != tag]
    if len(data["intents"]) == before:
        return jsonify({"success": False, "message": f"Tag '{tag}' not found."}), 404
    _save_intents(data)
    return jsonify({"success": True, "message": f"Intent '{tag}' deleted."})

@app.route("/api/intents/<tag>", methods=["PUT"])
def update_intent(tag):
    if not get_current_user():
        return jsonify({"success": False, "message": "Not logged in."}), 401
    body = request.json
    data = _load_intents()
    for intent in data["intents"]:
        if intent["tag"] == tag:
            if "patterns"  in body: intent["patterns"]  = body["patterns"]
            if "responses" in body: intent["responses"] = body["responses"]
            _save_intents(data)
            return jsonify({"success": True, "message": f"Intent '{tag}' updated."})
    return jsonify({"success": False, "message": f"Tag '{tag}' not found."}), 404

# ── Chat Export ──────────────────────────────────────────────
@app.route("/api/export/chat", methods=["POST"])
def export_chat():
    if not get_current_user():
        return jsonify({"success": False, "message": "Not logged in."}), 401
    data     = request.json
    messages = data.get("messages", [])
    fmt      = data.get("format", "json").lower()
    title    = data.get("title", "Omina Chat")
    now      = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")

    if fmt == "json":
        payload = _json.dumps({"title": title, "exported_at": now, "messages": messages}, ensure_ascii=False, indent=2)
        resp    = make_response(payload)
        resp.headers["Content-Type"]        = "application/json"
        resp.headers["Content-Disposition"] = f'attachment; filename="omina_chat_{_dt.datetime.now().strftime("%Y%m%d_%H%M%S")}.json"'
        return resp
    elif fmt == "txt":
        lines = [f"Omina AI — {title}", f"Exported: {now}", "=" * 50, ""]
        for m in messages:
            role = "You" if m.get("role") == "user" else "Omina AI"
            lines += [f"[{role}]", m.get("content", ""), ""]
        resp = make_response("\n".join(lines))
        resp.headers["Content-Type"]        = "text/plain; charset=utf-8"
        resp.headers["Content-Disposition"] = f'attachment; filename="omina_chat_{_dt.datetime.now().strftime("%Y%m%d_%H%M%S")}.txt"'
        return resp
    elif fmt == "md":
        lines = [f"# {title}", f"> Exported from Omina AI · {now}", ""]
        for m in messages:
            role = "**You**" if m.get("role") == "user" else "**Omina AI**"
            lines += [f"### {role}", m.get("content", ""), ""]
        resp = make_response("\n".join(lines))
        resp.headers["Content-Type"]        = "text/markdown; charset=utf-8"
        resp.headers["Content-Disposition"] = f'attachment; filename="omina_chat_{_dt.datetime.now().strftime("%Y%m%d_%H%M%S")}.md"'
        return resp

    return jsonify({"success": False, "message": "Invalid format. Use: json, txt, md"}), 400



# ══════════════════════════════════════════════════════════════
# ── Multi-Device Sync API ─────────────────────────────────────
# ══════════════════════════════════════════════════════════════

# Sync করা হবে এই keys গুলো
SYNC_KEYS = [
    # ── Chat & Sessions ──
    'omina_v4',        # chat sessions
    'omina_tabs',      # chat tabs
    'omina_folders',   # folders
    'omina_templates', # chat templates
    'omina_votes',     # message votes
    'omina_mc_hist',   # model compare history
    'omina_agent_hist',# AI agent history

    # ── User Data ──
    'omina_todos',     # to-do list
    'omina_sticky',    # sticky notes
    'omina_prompts',   # custom prompts
    'omina_memories',  # AI memories
    'omina_reviews',   # review comments
    'omina_tray',      # tray notifications

    # ── Profile & Settings ──
    'omina_profiles',
    'omina_active_profile',
    'omina_avatar',
    'omina_settings',  # theme, fontSize, language, sound etc

    # ── Features ──
    'omina_qa',        # quick access IDs
    'omina_imggen',    # image gen history
    'omina_ws_members','omina_ws_notes',
    'omina_wc_reset',  'omina_wc_ignored',
    'omina_comp_runs',

    # ── Device Preferences (sync করা হবে) ──
    'omina_tts_on', 'omina_tts_voice', 'omina_tts_rate', 'omina_tts_pitch',
    'omina_apikeys',
    'omina_ratelimits',
    'omina_chatlock',
    'omina_splash_done',
    'omina_onboard_done',
    'omina_cache',
]

@app.route("/api/sync/push", methods=["POST"])
def sync_push():
    """Device থেকে data Firestore এ save করা"""
    user = get_current_user()
    if not user:
        return jsonify({"success": False, "message": "Not logged in."}), 401

    data    = request.json or {}
    payload = data.get("data", {})

    if not payload:
        return jsonify({"success": False, "message": "No data provided."}), 400

    # শুধু allowed keys save করা
    filtered = {k: v for k, v in payload.items() if k in SYNC_KEYS}
    if not filtered:
        return jsonify({"success": False, "message": "No valid keys."}), 400

    sync_ref = db.collection("users").document(user["uid"]).collection("sync").document("appdata")
    sync_ref.set(filtered, merge=True)  # merge=True → existing data overwrite না করে merge করে

    return jsonify({"success": True, "synced": list(filtered.keys())})


@app.route("/api/sync/pull", methods=["GET"])
def sync_pull():
    """Firestore থেকে data নিয়ে device এ load করা"""
    user = get_current_user()
    if not user:
        return jsonify({"success": False, "message": "Not logged in."}), 401

    sync_ref = db.collection("users").document(user["uid"]).collection("sync").document("appdata")
    doc      = sync_ref.get()

    if not doc.exists:
        return jsonify({"success": True, "data": {}})  # নতুন user → empty

    return jsonify({"success": True, "data": doc.to_dict()})


@app.route("/api/sync/key", methods=["POST"])
def sync_key():
    """Single key update — একটা data change হলে শুধু সেটা push"""
    user = get_current_user()
    if not user:
        return jsonify({"success": False, "message": "Not logged in."}), 401

    data  = request.json or {}
    key   = data.get("key", "")
    value = data.get("value")

    if key not in SYNC_KEYS:
        return jsonify({"success": False, "message": f"Key '{key}' not allowed."}), 400

    sync_ref = db.collection("users").document(user["uid"]).collection("sync").document("appdata")
    sync_ref.set({key: value}, merge=True)

    return jsonify({"success": True, "key": key})

# ── Image Generation (Pollinations.ai — no API key needed) ───
@app.route("/api/generate-image", methods=["POST"])
def generate_image():
    user = get_current_user()
    if not user:
        return jsonify({"success": False, "error": "Not logged in."}), 401

    data      = request.json
    prompt    = data.get("prompt", "").strip()
    style     = data.get("style", "realistic")
    aspect    = data.get("aspect", "1:1")
    neg_prompt = data.get("neg_prompt", "").strip()
    lighting   = data.get("lighting", "").strip()
    color      = data.get("color", "").strip()
    seed_val   = data.get("seed", None)

    if not prompt:
        return jsonify({"success": False, "error": "Prompt is required."}), 400

    style_map = {
        "realistic":  "photorealistic, ultra detailed, 8k, DSLR photography, sharp focus",
        "anime":      "anime style, vibrant colors, Japanese animation, Studio Ghibli",
        "oil":        "oil painting, thick brush strokes, classical art, canvas texture",
        "watercolor": "watercolor painting, soft color washes, artistic, delicate strokes",
        "3d":         "3D render, CGI, octane render, studio lighting, highly detailed",
        "pixel":      "pixel art, 16-bit retro style, crisp pixels, game sprite",
        "sketch":     "pencil sketch, hand drawn illustration, fine line art, grayscale",
        "cyberpunk":  "cyberpunk neon, dystopian city, neon lights, futuristic, rain",
        "flat":       "flat design, minimalist vector art, bold colors, clean shapes",
        "fantasy":    "fantasy art, magical atmosphere, epic scene, detailed illustration",
        "minimal":    "minimalist, clean composition, simple elegant, white background",
        "vintage":    "vintage retro aesthetic, film grain, faded colors, 1970s photo",
    }
    aspect_size = {
        "1:1":  (1024, 1024),
        "16:9": (1280, 720),
        "9:16": (720, 1280),
        "4:3":  (1024, 768),
        "3:2":  (1200, 800),
    }
    w, h = aspect_size.get(aspect, (1024, 1024))
    style_suffix = style_map.get(style, "")
    full_prompt  = f"{prompt}, {style_suffix}" if style_suffix else prompt
    if lighting:
        full_prompt += f", {lighting.lower()} lighting"
    if color:
        full_prompt += f", {color.lower()} color palette"
    if neg_prompt:
        full_prompt += f" --no {neg_prompt}"

    try:
        import urllib.parse
        encoded  = urllib.parse.quote(full_prompt)
        seed     = int(seed_val) if seed_val else abs(hash(full_prompt)) % 99999
        url     = f"https://image.pollinations.ai/prompt/{encoded}?width={w}&height={h}&seed={seed}&nologo=true&enhance=true"

        resp = _requests.get(url, timeout=90, headers={"User-Agent": "OminaAI/1.0"})

        if resp.status_code == 200 and "image" in resp.headers.get("content-type", ""):
            img_b64 = base64.b64encode(resp.content).decode("utf-8")
            ctype   = resp.headers.get("content-type", "image/jpeg").split(";")[0]
            return jsonify({
                "success": True,
                "image":   f"data:{ctype};base64,{img_b64}",
                "model":   "FLUX (Pollinations)",
                "prompt":  prompt,
                "style":   style,
            })
        else:
            return jsonify({"success": False, "error": f"Service returned {resp.status_code}. Try again."}), 500

    except _requests.exceptions.Timeout:
        return jsonify({"success": False, "error": "Generation timed out (>90s). Try a simpler prompt."}), 504
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


if __name__ == "__main__":
    print("Omina AI running at http://127.0.0.1:5000")
    app.run(debug=True)


# ═══════════════════════════════════════════════════════════════
#  PAYMENT GATEWAY ROUTES
# ═══════════════════════════════════════════════════════════════
import hashlib, hmac, time, uuid

# ── bKash Payment ───────────────────────────────────────────
BKASH_BASE_URL   = os.environ.get("BKASH_BASE_URL",   "https://tokenized.sandbox.bka.sh/v1.2.0-beta")
BKASH_APP_KEY    = os.environ.get("BKASH_APP_KEY",    "")
BKASH_APP_SECRET = os.environ.get("BKASH_APP_SECRET", "")
BKASH_USERNAME   = os.environ.get("BKASH_USERNAME",   "")
BKASH_PASSWORD   = os.environ.get("BKASH_PASSWORD",   "")

def bkash_grant_token():
    """Get bKash auth token"""
    import requests
    headers = {
        "Content-Type":  "application/json",
        "Accept":        "application/json",
        "username":      BKASH_USERNAME,
        "password":      BKASH_PASSWORD,
    }
    body = {"app_key": BKASH_APP_KEY, "app_secret": BKASH_APP_SECRET}
    res = requests.post(f"{BKASH_BASE_URL}/tokenized/checkout/token/grant", json=body, headers=headers, timeout=10)
    data = res.json()
    return data.get("id_token"), data.get("refresh_token")

@app.route("/api/payment/bkash/create", methods=["POST"])
def bkash_create():
    """Create bKash payment"""
    import requests as req
    user = get_current_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    data      = request.json or {}
    amount    = str(data.get("amount", "12"))
    plan_id   = data.get("plan_id", "pro")
    inv_id    = f"OMINA-{uuid.uuid4().hex[:10].upper()}"
    try:
        id_token, _ = bkash_grant_token()
        headers = {
            "Content-Type":  "application/json",
            "Accept":        "application/json",
            "Authorization": id_token,
            "X-APP-Key":     BKASH_APP_KEY,
        }
        body = {
            "mode":                  "0011",
            "payerReference":        user["uid"],
            "callbackURL":           os.environ.get("APP_URL", "") + "/api/payment/bkash/callback",
            "amount":                amount,
            "currency":              "BDT",
            "intent":                "sale",
            "merchantInvoiceNumber": inv_id,
        }
        res  = req.post(f"{BKASH_BASE_URL}/tokenized/checkout/create", json=body, headers=headers, timeout=10)
        resp = res.json()
        if resp.get("statusCode") == "0000":
            # Store pending payment in Firestore
            db.collection("payments").document(inv_id).set({
                "uid":        user["uid"],
                "plan_id":    plan_id,
                "amount":     amount,
                "method":     "bkash",
                "invoice_id": inv_id,
                "status":     "pending",
                "created_at": _dt.datetime.utcnow().isoformat(),
                "payment_id": resp.get("paymentID"),
            })
            return jsonify({"success": True, "bkashURL": resp.get("bkashURL"), "paymentID": resp.get("paymentID")})
        return jsonify({"error": resp.get("statusMessage", "bKash error")}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/payment/bkash/callback", methods=["GET", "POST"])
def bkash_callback():
    """bKash payment callback"""
    import requests as req
    payment_id = request.args.get("paymentID") or (request.json or {}).get("paymentID")
    status     = request.args.get("status")
    if status == "cancel" or status == "failure":
        return redirect("/?payment=failed")
    try:
        id_token, _ = bkash_grant_token()
        headers = {"Authorization": id_token, "X-APP-Key": BKASH_APP_KEY, "Content-Type": "application/json"}
        res  = req.post(f"{BKASH_BASE_URL}/tokenized/checkout/execute", json={"paymentID": payment_id}, headers=headers, timeout=10)
        resp = res.json()
        if resp.get("statusCode") == "0000":
            # Find and update payment record
            docs = db.collection("payments").where("payment_id", "==", payment_id).limit(1).stream()
            for doc in docs:
                pay_data = doc.to_dict()
                doc.reference.update({"status": "completed", "trxID": resp.get("trxID")})
                # Activate plan
                db.collection("users").document(pay_data["uid"]).set(
                    {"plan": pay_data["plan_id"], "plan_updated": _dt.datetime.utcnow().isoformat()},
                    merge=True
                )
            return redirect("/?payment=success")
        return redirect("/?payment=failed")
    except Exception as e:
        return redirect(f"/?payment=error&msg={str(e)}")

# ── Nagad Payment ────────────────────────────────────────────
NAGAD_BASE_URL    = os.environ.get("NAGAD_BASE_URL",    "https://api.mynagad.com")
NAGAD_MERCHANT_ID = os.environ.get("NAGAD_MERCHANT_ID", "")
NAGAD_PUBLIC_KEY  = os.environ.get("NAGAD_PUBLIC_KEY",  "")
NAGAD_PRIVATE_KEY = os.environ.get("NAGAD_PRIVATE_KEY", "")

@app.route("/api/payment/nagad/create", methods=["POST"])
def nagad_create():
    """Create Nagad payment"""
    user = get_current_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    data    = request.json or {}
    amount  = str(data.get("amount", "12"))
    plan_id = data.get("plan_id", "pro")
    inv_id  = f"OMINA-{uuid.uuid4().hex[:10].upper()}"
    try:
        import requests as req
        from base64 import b64encode
        timestamp   = _dt.datetime.now().strftime("%Y%m%d%H%M%S")
        order_id    = inv_id
        # Nagad API call
        headers = {
            "X-KM-Api-Version": "v-0.2.0",
            "X-KM-IP-V4":       "127.0.0.1",
            "X-KM-Client-Type": "PC_WEB",
            "Content-Type":     "application/json",
        }
        init_body = {
            "datetime":        timestamp,
            "invoiceNumber":   order_id,
            "amount":          amount,
            "challenge":       uuid.uuid4().hex,
        }
        init_url = f"{NAGAD_BASE_URL}/api/dfs/check-out/initialize/{NAGAD_MERCHANT_ID}/{order_id}"
        res = req.post(init_url, json=init_body, headers=headers, timeout=10)
        resp = res.json()
        # Store pending
        db.collection("payments").document(inv_id).set({
            "uid": user["uid"], "plan_id": plan_id, "amount": amount,
            "method": "nagad", "invoice_id": inv_id, "status": "pending",
            "created_at": _dt.datetime.utcnow().isoformat(),
        })
        return jsonify({"success": True, "redirectURL": resp.get("callBackUrl", ""), "invoice": inv_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/payment/nagad/callback", methods=["GET", "POST"])
def nagad_callback():
    payment_ref_id = request.args.get("payment_ref_id")
    status         = request.args.get("status")
    order_id       = request.args.get("order_id")
    if status == "Success" and order_id:
        try:
            docs = db.collection("payments").where("invoice_id", "==", order_id).limit(1).stream()
            for doc in docs:
                pay = doc.to_dict()
                doc.reference.update({"status": "completed", "nagad_ref": payment_ref_id})
                db.collection("users").document(pay["uid"]).set(
                    {"plan": pay["plan_id"], "plan_updated": _dt.datetime.utcnow().isoformat()},
                    merge=True
                )
            return redirect("/?payment=success")
        except Exception as e:
            return redirect(f"/?payment=error&msg={str(e)}")
    return redirect("/?payment=failed")

# ── Stripe (Card + PayPal) ────────────────────────────────────
STRIPE_SECRET_KEY      = os.environ.get("STRIPE_SECRET_KEY",      "")
STRIPE_WEBHOOK_SECRET  = os.environ.get("STRIPE_WEBHOOK_SECRET",  "")
STRIPE_PRICES = {
    # Set these in env or Stripe dashboard
    "pro_monthly":  os.environ.get("STRIPE_PRICE_PRO_M",  ""),
    "pro_yearly":   os.environ.get("STRIPE_PRICE_PRO_Y",  ""),
    "max_monthly":  os.environ.get("STRIPE_PRICE_MAX_M",  ""),
    "max_yearly":   os.environ.get("STRIPE_PRICE_MAX_Y",  ""),
    "code_monthly": os.environ.get("STRIPE_PRICE_CODE_M", ""),
    "code_yearly":  os.environ.get("STRIPE_PRICE_CODE_Y", ""),
}

@app.route("/api/payment/stripe/create-session", methods=["POST"])
def stripe_create_session():
    """Create Stripe Checkout session (card + PayPal)"""
    user = get_current_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    if not STRIPE_SECRET_KEY:
        return jsonify({"error": "Stripe not configured"}), 503
    try:
        import stripe
        stripe.api_key = STRIPE_SECRET_KEY
        data       = request.json or {}
        plan_id    = data.get("plan_id", "pro")
        billing    = data.get("billing", "m")   # m or y
        method     = data.get("method", "card") # card or paypal
        price_key  = f"{plan_id}_{('monthly' if billing=='m' else 'yearly')}"
        price_id   = STRIPE_PRICES.get(price_key)
        if not price_id:
            return jsonify({"error": f"Price not configured for {price_key}"}), 400
        pay_methods = ["card"]
        if method == "paypal":
            pay_methods = ["paypal"]
        app_url = os.environ.get("APP_URL", "https://omina-ai.onrender.com")
        session = stripe.checkout.Session.create(
            payment_method_types = pay_methods,
            mode                 = "subscription",
            line_items           = [{"price": price_id, "quantity": 1}],
            success_url          = f"{app_url}/?payment=success&session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url           = f"{app_url}/?payment=cancelled",
            client_reference_id  = user["uid"],
            customer_email       = user.get("email"),
            metadata             = {"plan_id": plan_id, "uid": user["uid"]},
        )
        # Store pending
        db.collection("payments").document(session.id).set({
            "uid": user["uid"], "plan_id": plan_id, "method": method,
            "billing": billing, "status": "pending",
            "created_at": _dt.datetime.utcnow().isoformat(),
        })
        return jsonify({"success": True, "url": session.url, "session_id": session.id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/payment/stripe/webhook", methods=["POST"])
def stripe_webhook():
    """Stripe webhook — activates plan after payment"""
    if not STRIPE_SECRET_KEY:
        return jsonify({"error": "Not configured"}), 503
    try:
        import stripe
        stripe.api_key = STRIPE_SECRET_KEY
        payload    = request.get_data(as_text=True)
        sig_header = request.headers.get("Stripe-Signature", "")
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
        if event["type"] == "checkout.session.completed":
            sess    = event["data"]["object"]
            uid     = sess.get("client_reference_id") or sess["metadata"].get("uid")
            plan_id = sess["metadata"].get("plan_id", "pro")
            if uid:
                db.collection("users").document(uid).set(
                    {"plan": plan_id, "plan_updated": _dt.datetime.utcnow().isoformat(),
                     "stripe_customer": sess.get("customer"),
                     "stripe_subscription": sess.get("subscription")},
                    merge=True
                )
                doc_ref = db.collection("payments").document(sess.id)
                if doc_ref.get().exists:
                    doc_ref.update({"status": "completed", "trxID": sess.get("payment_intent")})
        elif event["type"] in ("customer.subscription.deleted", "customer.subscription.paused"):
            sub  = event["data"]["object"]
            cust = sub.get("customer")
            # Find user by stripe_customer
            docs = db.collection("users").where("stripe_customer", "==", cust).limit(1).stream()
            for doc in docs:
                doc.reference.update({"plan": "free"})
        return jsonify({"received": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route("/api/payment/verify", methods=["POST"])
def verify_payment():
    """Verify payment status and return current plan"""
    user = get_current_user()
    if not user:
        return jsonify({"error": "Unauthorized"}), 401
    try:
        doc = db.collection("users").document(user["uid"]).get()
        if doc.exists:
            data = doc.to_dict()
            return jsonify({"plan": data.get("plan", "free"), "plan_updated": data.get("plan_updated")})
        return jsonify({"plan": "free"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── Image Analysis ────────────────────────────────────────────
@app.route("/api/analyze-image", methods=["POST"])
def analyze_image():
    user = get_current_user()
    if not user:
        return jsonify({"success": False, "error": "Not logged in."}), 401
    data      = request.json
    image_b64 = data.get("image", "")      # full data:image/... base64 string
    prompt    = data.get("prompt", "Analyze and describe this image in detail.")
    lang_instr = data.get("lang_instruction", "")
    if not image_b64:
        return jsonify({"success": False, "error": "No image provided."}), 400
    try:
        # Use Groq vision model (llama-4-scout supports vision)
        system_msg = f"You are Omina AI, a helpful visual analysis assistant. {lang_instr}"
        completion = groq_client.chat.completions.create(
            model="meta-llama/llama-4-scout-17b-16e-instruct",
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": image_b64}},
                    {"type": "text",      "text": prompt}
                ]}
            ],
            max_tokens=1024,
            temperature=0.6,
        )
        reply = completion.choices[0].message.content
        reply = re.sub(r"<think>.*?</think>", "", reply, flags=re.DOTALL).strip()
        return jsonify({"success": True, "reply": reply})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

# ── Voice Transcription ───────────────────────────────────────
@app.route("/api/transcribe", methods=["POST"])
def transcribe_voice():
    user = get_current_user()
    if not user:
        return jsonify({"success": False, "error": "Not logged in."}), 401
    if "audio" not in request.files:
        return jsonify({"success": False, "error": "No audio file."}), 400
    audio_file = request.files["audio"]
    lang       = request.form.get("lang", "")
    try:
        audio_bytes = audio_file.read()
        # Groq Whisper transcription
        transcription = groq_client.audio.transcriptions.create(
            file=("voice.webm", audio_bytes, audio_file.content_type or "audio/webm"),
            model="whisper-large-v3",
            language=lang if lang and lang != "auto" else None,
            response_format="text",
        )
        text = transcription if isinstance(transcription, str) else transcription.text
        return jsonify({"success": True, "text": text.strip()})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

# ── Message Translation ───────────────────────────────────────
@app.route("/api/translate", methods=["POST"])
def translate_message():
    user = get_current_user()
    if not user:
        return jsonify({"success": False, "error": "Not logged in."}), 401
    data      = request.json
    text      = data.get("text", "").strip()
    target    = data.get("target", "English")
    if not text:
        return jsonify({"success": False, "error": "No text provided."}), 400
    try:
        completion = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": f"You are a professional translator. Translate the given text to {target}. Return ONLY the translated text — no explanations, no original text, no quotes, no labels."},
                {"role": "user",   "content": text}
            ],
            max_tokens=1024,
            temperature=0.3,
        )
        translated = completion.choices[0].message.content.strip()
        return jsonify({"success": True, "translated": translated, "target": target})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
