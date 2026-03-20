from flask import Flask, request, jsonify, render_template, Response
from groq import Groq
from dotenv import load_dotenv
import os, random, json, threading, re, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date
import uuid
from flask import Flask

app = Flask(__name__)

@app.route("/")
def home():
    return "Flask is working!"

# VERY IMPORTANT
app = app
load_dotenv()
app    = Flask(__name__)
client = Groq(api_key=os.getenv("GROQ_API_KEY"))

# ============================
# 🧠 SHARED STATE
# ============================
joke_pool   = {cat: [] for cat in ["random","tech","dark","dad","mom","pun","roast"]}
told_jokes  = set()
pool_lock   = threading.Lock()
laugh_stats = {cat: {"told":0,"laughs":0} for cat in joke_pool}
CONVERSATION_MEMORY = []
conv_lock   = threading.Lock()

session_stats = {
    "total_jokes":0,"total_laughs":0,
    "hilarious":0,"funny":0,"meh":0,
    "top_category":"random","best_joke":"","best_score":0,
    "streak":0,"last_visit":str(date.today()),
    "category_counts":{cat:0 for cat in ["random","tech","dark","dad","mom","pun","roast"]},
}
stats_lock   = threading.Lock()
joke_history = []
history_lock = threading.Lock()
jotd_cache   = {"joke":"","date":"","category":"random"}
jotd_lock    = threading.Lock()
theme_state  = {"mode":"dark"}
user_names   = {}

CATEGORY_VOICES  = {"random":"daniel","tech":"austin","dark":"troy","dad":"daniel","mom":"hannah","pun":"hannah","roast":"austin"}
CATEGORY_EMOTION = {"random":"<cheerful>","tech":"<curious>","dark":"<whisper>","dad":"<laugh>","mom":"<cheerful>","pun":"<cheerful>","roast":"<laugh>"}
CATEGORY_PERSONAS = {
    "random":"You are a street comedian. Fresh jokes from Reddit, Twitter, open mic.",
    "tech":  "You are a senior dev comedian. Jokes from coding pain, bugs, deployments.",
    "dark":  "You are a deadpan dark comedian like Anthony Jeselnik. Never hateful.",
    "dad":   "You are the ultimate dad — corny, proud of your groan-worthy puns.",
    "mom":   "You are a witty sharp mom with sass and wisdom. Roast with love.",
    "pun":   "You are a wordplay genius. Smart layered puns.",
    "roast": "You are a Comedy Central roast comedian. Bold but never cruel.",
}
SYSTEM_PROMPT = (
    "You are AmourAI, a witty comedian who tells short, punchy jokes like a real human. "
    "ALWAYS start with a casual greeting like 'Hey!', 'Yo!', 'Haha hi!' or 'Sup!' "
    "Then lead with 'I got a joke', 'Bro listen to this', 'Okay okay hear me out'. "
    "Then deliver the joke. MAX 4 lines. Casual, punchy, human. No formal language."
)
MAIN_MODEL = "llama-3.1-8b-instant"

# ============================
# 🤖 AGENTS 1-4: JOKE PIPELINE
# ============================
def agent_writer(category, count=5):
    persona = CATEGORY_PERSONAS.get(category, CATEGORY_PERSONAS["random"])
    with pool_lock:
        existing = list(told_jokes) + joke_pool.get(category, [])
    avoid = ("\n\nNEVER repeat:\n" + "\n".join(f"- {j}" for j in existing[-15:])) if existing else ""
    prompt = (
        f"Generate exactly {count} unique {category} jokes.\n"
        f"- Start with 'Hey!', 'Yo!', 'Sup!' or 'Haha hi!'\n"
        f"- Lead-in: 'I got a joke', 'Okay hear me out', 'Bro listen'\n"
        f"- Clear setup + surprising punchline. MAX 3 lines.\n"
        f"- Return ONLY a raw JSON array. Format: [\"joke1\", \"joke2\"]\n{avoid}"
    )
    try:
        time.sleep(1)
        print(f"  ✍️  Writer → {count} {category} jokes...")
        res = client.chat.completions.create(
            model=MAIN_MODEL,
            messages=[{"role":"system","content":SYSTEM_PROMPT},{"role":"user","content":prompt}],
            max_tokens=600, temperature=1.3, top_p=0.95,
        )
        raw = res.choices[0].message.content.strip()
        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json"): raw = raw[4:]
        jokes = json.loads(raw.strip())
        print(f"  ✅ Writer → {len(jokes)} jokes")
        return jokes
    except Exception as e:
        print(f"  ❌ Writer ERROR ({category}): {e}")
        return []

def agent_dedup(jokes, category):
    with pool_lock:
        existing = told_jokes | set(joke_pool.get(category, []))
    fresh = []
    for j in jokes:
        jc = j.strip()
        if jc not in existing:
            prefix = jc[:30].lower()
            if not any(e[:30].lower()==prefix for e in existing if len(e)>=30):
                fresh.append(jc)
    print(f"  🔁 Dedup → {len(fresh)}/{len(jokes)} fresh")
    return fresh

def agent_pool_manager(category, jokes):
    if not jokes: return
    with pool_lock:
        joke_pool[category].extend(jokes)
        random.shuffle(joke_pool[category])
    print(f"  📦 Pool → {category}: {len(joke_pool[category])} jokes")

def agent_pool_monitor():
    for cat in joke_pool:
        with pool_lock:
            size = len(joke_pool[cat])
        if size < 3:
            threading.Thread(target=run_pipeline, args=(cat,5), daemon=True).start()

# ============================
# 🤖 AGENT 5: EMOTION TAGGER
# ============================
def agent_emotion_tagger(joke_text, category):
    try:
        res = client.chat.completions.create(
            model=MAIN_MODEL,
            messages=[
                {"role":"system","content":"Comedy emotion analyzer. Return only JSON."},
                {"role":"user","content":f"Analyze this {category} joke. Return JSON: intensity(1-10), laugh_type(chuckle/giggle/burst/rofl/groan/silence), mouth_speed(slow/medium/fast), reaction(smile/laugh/shocked/deadpan/wink). ONLY JSON.\n\nJoke: {joke_text}"}
            ],
            max_tokens=80, temperature=0.2,
        )
        raw = res.choices[0].message.content.strip()
        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json"): raw = raw[4:]
        return json.loads(raw.strip())
    except Exception as e:
        print(f"  ❌ Emotion ERROR: {e}")
        return {"intensity":5,"laugh_type":"chuckle","mouth_speed":"medium","reaction":"smile"}

# ============================
# 🤖 AGENT 6: LIP SYNC
# ============================
def agent_lip_sync(joke_text, mouth_speed="medium"):
    words = joke_text.split()
    frames = []
    frame_duration = {"slow":180,"medium":120,"fast":80}.get(mouth_speed, 120)
    vowel_shapes = {"a":"D","e":"E","i":"C","o":"A","u":"F"}
    for word in words:
        wc = re.sub(r'[^a-zA-Z]','',word).lower()
        if not wc:
            frames.append({"shape":"B","duration":frame_duration//2})
            continue
        for char in wc[:4]:
            frames.append({"shape":vowel_shapes.get(char, random.choice(["B","C"])),"duration":frame_duration})
        frames.append({"shape":"B","duration":frame_duration//2})
    return frames[:80]

# ============================
# 🤖 AGENT 7: ANALYTICS
# ============================
def agent_analytics_record(category, joke_text, score=5):
    with pool_lock:
        if category in laugh_stats:
            laugh_stats[category]["told"] += 1
            laugh_stats[category]["laughs"] += score
    with stats_lock:
        session_stats["total_jokes"] += 1
        session_stats["category_counts"][category] = session_stats["category_counts"].get(category,0)+1
        session_stats["top_category"] = max(session_stats["category_counts"], key=session_stats["category_counts"].get)
        if score >= session_stats["best_score"]:
            session_stats["best_score"] = score
            session_stats["best_joke"]  = joke_text

# ============================
# 🤖 AGENT 8: CONVERSATION
# ============================
def agent_intent_detector(user_msg, last_joke, waiting_for_next):
    joke_kw = ['joke','funny','laugh','tell me','give me','another','more','next','again','hit me','sure','yes','yep','yeah','ok','okay','ready']
    react_kw = ['hilarious','lol','haha','rofl','good one','nice','great','amazing','love it','so funny','dead','bad','terrible','awful','boring','meh','not funny','omg','bro','fr','ngl']
    lower = user_msg.lower().strip()
    if waiting_for_next and any(lower==a or lower.startswith(a) for a in ['yes','sure','ok','okay','yep','yeah','go','hit me','more','another','please','lol','haha']):
        return 'joke'
    if last_joke and any(w in lower for w in react_kw):
        return 'conversation'
    if any(w in lower for w in joke_kw[:8]):
        return 'joke'
    if len(user_msg.split()) <= 5:
        return 'conversation'
    return 'joke'

def agent_conversation(user_msg, category, last_joke):
    with conv_lock:
        hist = CONVERSATION_MEMORY[-4:]
    history_text = ("\n\nRecent:\n" + "\n".join(f"{'User' if m['role']=='user' else 'AmourAI'}: {m['content']}" for m in hist)) if hist else ""
    try:
        res = client.chat.completions.create(
            model=MAIN_MODEL,
            messages=[
                {"role":"system","content":"You are AmourAI, a hilarious comedian friend. React naturally, always pivot to jokes. Never sound like an AI."},
                {"role":"user","content":f"User: '{user_msg}'\nLast joke: '{last_joke}'{history_text}\n\nRespond MAX 2 lines. React naturally. End with offering another joke. Use bro/lol/haha/ngl."}
            ],
            max_tokens=100, temperature=1.1,
        )
        reply = res.choices[0].message.content.strip()
        with conv_lock:
            CONVERSATION_MEMORY.append({"role":"user","content":user_msg})
            CONVERSATION_MEMORY.append({"role":"assistant","content":reply})
            if len(CONVERSATION_MEMORY) > 10:
                CONVERSATION_MEMORY[:] = CONVERSATION_MEMORY[-10:]
        return reply
    except Exception as e:
        print(f"  ❌ Conv ERROR: {e}")
        return "Haha okay — want me to hit you with another one? 😂"

# ============================
# 🤖 AGENT 9: SHARE
# ============================
def agent_share(joke_text, category):
    emoji_map = {"random":"🎲","tech":"💻","dark":"🌑","dad":"👴","mom":"👩","pun":"🎭","roast":"🔥"}
    emoji = emoji_map.get(category,"😂")
    return f"{emoji} AmourAI says:\n\n{joke_text}\n\n— Try AmourAI for more laughs!"

# ============================
# 🤖 AGENT 10: STATS
# ============================
def agent_get_stats():
    with stats_lock:
        stats = dict(session_stats)
    total = stats["total_jokes"]
    stats["laugh_rate"] = round((stats["total_laughs"] / total * 100) if total > 0 else 0, 1)
    return stats

def agent_update_reaction(category, joke_text, reaction_type):
    score_map = {"hilarious":10,"funny":6,"meh":2}
    score = score_map.get(reaction_type, 5)
    with stats_lock:
        session_stats["total_laughs"] += score
        session_stats[reaction_type]   = session_stats.get(reaction_type,0)+1
        if score >= session_stats["best_score"]:
            session_stats["best_score"] = score
            session_stats["best_joke"]  = joke_text
    agent_analytics_record(category, joke_text, score)
    return score

# ============================
# 🤖 AGENT 11: HISTORY
# ============================
def agent_history_add(joke_text, category, emotion_data):
    with history_lock:
        joke_history.append({
            "id"       : str(uuid.uuid4())[:8],
            "joke"     : joke_text,
            "category" : category,
            "emotion"  : emotion_data,
            "timestamp": datetime.now().strftime("%H:%M"),
            "rating"   : None,
        })
        if len(joke_history) > 50:
            joke_history[:] = joke_history[-50:]

def agent_history_get():
    with history_lock:
        return list(reversed(joke_history))

def agent_history_rate(joke_id, rating):
    with history_lock:
        for j in joke_history:
            if j["id"] == joke_id:
                j["rating"] = rating
                return True
    return False

# ============================
# 🤖 AGENT 12: STREAK
# ============================
def agent_streak_check():
    today = str(date.today())
    with stats_lock:
        last = session_stats.get("last_visit","")
        yesterday = str(date.fromordinal(date.today().toordinal()-1))
        if last == today:
            streak = session_stats.get("streak",0)
        elif last == yesterday:
            session_stats["streak"] = session_stats.get("streak",0)+1
            session_stats["last_visit"] = today
            streak = session_stats["streak"]
        else:
            session_stats["streak"] = 1
            session_stats["last_visit"] = today
            streak = 1
    return streak

# ============================
# 🤖 AGENT 13: CONFETTI
# ============================
def agent_confetti_check(reaction_type, intensity):
    should = reaction_type == "hilarious" or intensity >= 9
    colors = {"hilarious":["#ff3d6e","#ff8c42","#7c3aed","#ffd700","#00ff88"],"funny":["#ff3d6e","#ff8c42","#7c3aed"],"rofl":["#ffd700","#ff3d6e","#00ff88","#7c3aed"]}
    return {"fire":should,"colors":colors.get(reaction_type,colors["funny"]),"count":150 if should else 0}

# ============================
# 🤖 AGENT 14: JOKE OF THE DAY
# ============================
def agent_joke_of_the_day():
    today = str(date.today())
    with jotd_lock:
        if jotd_cache["date"] == today and jotd_cache["joke"]:
            return dict(jotd_cache)
    try:
        cat = random.choice(["random","tech","dad","pun"])
        res = client.chat.completions.create(
            model=MAIN_MODEL,
            messages=[
                {"role":"system","content":SYSTEM_PROMPT},
                {"role":"user","content":f"Give me today's absolute best {cat} joke. This is the Joke of the Day — make it exceptional!"}
            ],
            max_tokens=150, temperature=1.2,
        )
        joke_text = res.choices[0].message.content.strip()
        with jotd_lock:
            jotd_cache["joke"]     = joke_text
            jotd_cache["date"]     = today
            jotd_cache["category"] = cat
        return dict(jotd_cache)
    except Exception as e:
        print(f"  ❌ JOTD ERROR: {e}")
        fallback = "Yo! I got one — Why did the calendar go to therapy? It had too many dates!"
        return {"joke":fallback,"date":today,"category":"random"}

# ============================
# 🤖 AGENT 15: NAME
# ============================
def agent_set_name(session_id, name):
    clean = name.strip()[:20]
    user_names[session_id] = clean
    return clean

def agent_get_name(session_id):
    return user_names.get(session_id, "You")

# ============================
# 🤖 AGENT 16: SOUND
# ============================
def agent_sound_effect(laugh_type, intensity):
    sound_map = {
        "burst" :{"type":"laugh_track","volume":0.7,"delay":500},
        "rofl"  :{"type":"big_laugh",  "volume":0.9,"delay":400},
        "chuckle":{"type":"chuckle",   "volume":0.5,"delay":600},
        "giggle" :{"type":"giggle",    "volume":0.5,"delay":500},
        "groan"  :{"type":"groan",     "volume":0.6,"delay":400},
        "silence":{"type":"crickets",  "volume":0.4,"delay":800},
    }
    effect = sound_map.get(laugh_type, sound_map["chuckle"]).copy()
    effect["intensity"] = intensity
    return effect

# ============================
# 🤖 AGENT 17: THEME
# ============================
def agent_theme_toggle():
    theme_state["mode"] = "light" if theme_state["mode"]=="dark" else "dark"
    return theme_state["mode"]

# ============================
# 🤖 AGENT 18: STAGE FX
# ============================
def agent_stage_fx(category, emotion_data):
    fx_map = {
        "random":{"particles":20,"color":"#ff3d6e","speed":1.0,"size":3},
        "tech"  :{"particles":30,"color":"#00ff88","speed":0.8,"size":2},
        "dark"  :{"particles":10,"color":"#7c3aed","speed":0.4,"size":4},
        "dad"   :{"particles":15,"color":"#ff8c42","speed":1.2,"size":3},
        "mom"   :{"particles":18,"color":"#ff6b9d","speed":0.9,"size":3},
        "pun"   :{"particles":25,"color":"#ffd700","speed":1.1,"size":2},
        "roast" :{"particles":35,"color":"#ff4400","speed":1.5,"size":3},
    }
    fx = fx_map.get(category, fx_map["random"]).copy()
    intensity = emotion_data.get("intensity",5) if emotion_data else 5
    fx["particles"] = int(fx["particles"] * (intensity/5))
    fx["active"] = True
    return fx

# ============================
# 🔗 PIPELINE
# ============================
def run_pipeline(category, count=5):
    print(f"\n🚀 Pipeline → [{category}]")
    jokes = agent_writer(category, count)
    fresh = agent_dedup(jokes, category)
    agent_pool_manager(category, fresh)
    print(f"✅ Pipeline done → [{category}] {len(fresh)} added\n")

def preload_all_parallel():
    print("\n" + "="*50)
    print("🚀 PRELOADING ALL POOLS")
    print("="*50)
    threading.Thread(target=agent_joke_of_the_day, daemon=True).start()
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(run_pipeline, cat, 5): cat for cat in joke_pool}
        for future in as_completed(futures):
            cat = futures[future]
            try:
                future.result()
                with pool_lock:
                    print(f"🎯 [{cat}] ready → {len(joke_pool[cat])} jokes")
            except Exception as e:
                print(f"❌ [{cat}] failed: {e}")
    print("✅ ALL POOLS LOADED!\n")

# ============================
# 🎤 ROUTES
# ============================
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/joke", methods=["POST"])
def joke():
    try:
        data     = request.get_json()
        category = data.get("category","random")
        user_msg = data.get("message","Tell me a joke")
        print(f"📩 Request: category={category}")
        agent_pool_monitor()

        joke_text = None
        with pool_lock:
            pool = joke_pool.get(category,[])
            while pool:
                candidate = pool.pop(0)
                if candidate.strip() not in told_jokes:
                    joke_text = candidate
                    told_jokes.add(joke_text.strip())
                    break

        if not joke_text:
            print(f"⚡ Pool empty — on-the-fly...")
            avoid = "\n".join(f"- {j}" for j in list(told_jokes)[-10:])
            res = client.chat.completions.create(
                model=MAIN_MODEL,
                messages=[
                    {"role":"system","content":SYSTEM_PROMPT},
                    {"role":"user","content":f"Tell me one fresh {category} joke. DO NOT repeat:\n{avoid}"}
                ],
                max_tokens=150, temperature=1.4,
            )
            joke_text = res.choices[0].message.content
            told_jokes.add(joke_text.strip())

        # Run agents
        emotion_data = {"intensity":5,"laugh_type":"chuckle","mouth_speed":"medium","reaction":"smile"}
        lip_frames   = []

        def get_emotion():
            nonlocal emotion_data
            emotion_data = agent_emotion_tagger(joke_text, category)
        def get_lipsync():
            nonlocal lip_frames
            lip_frames = agent_lip_sync(joke_text, emotion_data.get("mouth_speed","medium"))

        t1 = threading.Thread(target=get_emotion); t1.start(); t1.join()
        t2 = threading.Thread(target=get_lipsync); t2.start(); t2.join()

        agent_history_add(joke_text, category, emotion_data)
        sound_fx = agent_sound_effect(emotion_data.get("laugh_type","chuckle"), emotion_data.get("intensity",5))
        stage_fx = agent_stage_fx(category, emotion_data)
        threading.Thread(target=agent_analytics_record, args=(category,joke_text,emotion_data.get("intensity",5)), daemon=True).start()

        print(f"✅ Delivering: {joke_text[:60]}...")
        return jsonify({"joke":joke_text,"category":category,"emotion":emotion_data,"lip_frames":lip_frames,"sound_fx":sound_fx,"stage_fx":stage_fx})

    except Exception as e:
        print(f"❌ ERROR: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/speak", methods=["POST"])
def speak():
    try:
        data     = request.get_json()
        text     = data.get("text","")
        category = data.get("category","random")
        voice    = CATEGORY_VOICES.get(category,"daniel")
        emotion  = CATEGORY_EMOTION.get(category,"<cheerful>")
        res = client.audio.speech.create(
            model="canopylabs/orpheus-v1-english",
            voice=voice, input=f"{emotion} {text}", response_format="wav"
        )
        return Response(res.read(), mimetype="audio/wav", headers={"Content-Disposition":"inline; filename=joke.wav"})
    except Exception as e:
        print(f"❌ TTS ERROR: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/respond", methods=["POST"])
def respond():
    try:
        data         = request.get_json()
        user_msg     = data.get("message","")
        category     = data.get("category","random")
        last_joke    = data.get("last_joke","")
        waiting_next = data.get("waiting_for_next",False)
        print(f"💬 Respond: '{user_msg}'")
        intent = agent_intent_detector(user_msg, last_joke, waiting_next)
        if intent == 'joke':
            return jsonify({"intent":"joke"})
        reply = agent_conversation(user_msg, category, last_joke)
        return jsonify({"reply":reply,"intent":"conversation"})
    except Exception as e:
        print(f"❌ RESPOND ERROR: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/share",      methods=["POST"])
def share():
    d = request.get_json()
    return jsonify({"share_text": agent_share(d.get("joke",""), d.get("category","random"))})

@app.route("/stats",      methods=["GET"])
def stats():
    return jsonify(agent_get_stats())

@app.route("/feedback",   methods=["POST"])
def feedback():
    d        = request.get_json()
    category = d.get("category","random")
    joke     = d.get("joke","")
    reaction = d.get("reaction_type","funny")
    score    = agent_update_reaction(category, joke, reaction)
    confetti = agent_confetti_check(reaction, score)
    return jsonify({"status":"recorded","score":score,"confetti":confetti})

@app.route("/history",         methods=["GET"])
def history():
    return jsonify({"history": agent_history_get()})

@app.route("/history/rate",    methods=["POST"])
def history_rate():
    d = request.get_json()
    return jsonify({"success": agent_history_rate(d.get("id",""), d.get("rating",3))})

@app.route("/streak",          methods=["GET"])
def streak():
    return jsonify({"streak": agent_streak_check()})

@app.route("/jotd",            methods=["GET"])
def jotd():
    return jsonify(agent_joke_of_the_day())

@app.route("/name",            methods=["POST"])
def set_name():
    d = request.get_json()
    return jsonify({"name": agent_set_name(d.get("session_id","default"), d.get("name","You"))})

@app.route("/name/<session_id>",methods=["GET"])
def get_name(session_id):
    return jsonify({"name": agent_get_name(session_id)})

@app.route("/theme/toggle",    methods=["POST"])
def theme_toggle():
    return jsonify({"mode": agent_theme_toggle()})

@app.route("/theme",           methods=["GET"])
def theme_get():
    return jsonify({"mode": theme_state["mode"]})

@app.route("/reset",           methods=["POST"])
def reset():
    told_jokes.clear()
    for cat in joke_pool: joke_pool[cat].clear()
    joke_history.clear()
    with conv_lock:   CONVERSATION_MEMORY.clear()
    with stats_lock:
        for k in ["total_jokes","total_laughs","hilarious","funny","meh","best_score"]: session_stats[k]=0
        session_stats["best_joke"]=""
        session_stats["category_counts"]={cat:0 for cat in ["random","tech","dark","dad","mom","pun","roast"]}
    threading.Thread(target=preload_all_parallel, daemon=True).start()
    return jsonify({"status":"reset"})

@app.route("/pool-status",     methods=["GET"])
def pool_status():
    with pool_lock:
        status = {cat: len(jokes) for cat,jokes in joke_pool.items()}
    return jsonify({"pools":status,"total_told":len(told_jokes),"total_ready":sum(status.values()),"analytics":laugh_stats})

# To this
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, host="0.0.0.0", port=port)