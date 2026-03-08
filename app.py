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
GROQ_MODEL = "llama3-70b-8192"

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

    session["uid"]        = uid
    session["user_name"]  = user_doc.get("name") if user_doc.exists else name
    session["user_email"] = email

    return jsonify({"success": True, "name": session["user_name"]})

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
    if neg_prompt:
        full_prompt += f" --no {neg_prompt}"

    try:
        import urllib.parse
        encoded = urllib.parse.quote(full_prompt)
        seed    = abs(hash(full_prompt)) % 99999
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
