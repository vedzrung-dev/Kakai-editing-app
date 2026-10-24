"""AI Drama Editor. Upload a video -> AI edits it: captions, dramatic title, zoom punches,
sound effects, background music (auto-ducked under speech), cinematic color. Run: python editor_app.py"""
import hmac, json, os, queue, subprocess, sys, threading, time, uuid
from flask import Flask, abort, jsonify, request, send_from_directory
from werkzeug.utils import secure_filename
import clipper

PASSWORD = os.environ.get("APP_PASSWORD") or sys.exit("Set APP_PASSWORD first.")
DATA = os.path.abspath("edits")
os.makedirs(DATA, exist_ok=True)
app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024**3
jobs, q = {}, queue.Queue()
WEB = {"manifest.json", "sw.js", "icon-192.png", "icon-512.png"}
MOODS = {"tense": (110, 130.8, 185), "sad": (110, 130.8, 164.8),
         "epic": (98, 146.8, 196), "calm": (110, 138.6, 164.8)}
SFX = {
    "impact": "aevalsrc='0.9*sin(2*PI*(70-35*t)*t)*exp(-5*t)':d=1.5:s=44100",
    "whoosh": "anoisesrc=d=1:c=pink:a=0.6:r=44100,highpass=f=300,lowpass=f=5000,afade=t=in:d=0.5,afade=t=out:st=0.5:d=0.5",
    "riser": "aevalsrc='0.35*sin(2*PI*(180+500*t)*t)':d=2.5:s=44100,afade=t=in:d=2.2,afade=t=out:st=2.2:d=0.3",
}


def sh(*a, **k):
    subprocess.run(list(a), check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, **k)


def probe(p):
    o = subprocess.check_output(["ffprobe", "-v", "error", "-show_entries",
        "stream=codec_type,width,height:stream_side_data=rotation:format=duration", "-of", "json", p])
    j = json.loads(o)
    v = next(s for s in j["streams"] if s["codec_type"] == "video")
    if not any(s["codec_type"] == "audio" for s in j["streams"]):
        raise RuntimeError("This video has no audio/speech to edit.")
    w, h = v["width"], v["height"]
    if any(abs(int(d.get("rotation", 0))) % 180 == 90 for d in v.get("side_data_list", [])):
        w, h = h, w
    return w, h, float(j["format"]["duration"])


def ass_t(t):
    t = max(t, 0)
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def build_ass(path, W, H, segs, title):
    m = min(W, H)
    st = "Format: Name,Fontname,Fontsize,PrimaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding\n"
    head = (f"[Script Info]\nScriptType: v4.00+\nPlayResX: {W}\nPlayResY: {H}\n\n[V4+ Styles]\n{st}"
            f"Style: Cap,DejaVu Sans,{int(m * .07)},&H00FFFFFF,&H00000000,&H64000000,1,0,0,0,100,100,0,0,1,{max(2, int(m * .008))},2,2,40,40,{int(H * .12)},1\n"
            f"Style: Title,DejaVu Sans,{int(m * .11)},&H00FFFFFF,&H00000000,&H64000000,1,0,0,0,100,100,6,0,1,{max(3, int(m * .01))},3,5,60,60,10,1\n\n"
            "[Events]\nFormat: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n")
    ev = []
    if title:
        ev.append(f"Dialogue: 0,{ass_t(1)},{ass_t(5)},Title,,0,0,0,,{{\\fad(700,900)}}{title.upper()}")
    words = [w for s in segs for w in s["words"]]
    for i in range(0, len(words), 3):
        c = words[i:i + 3]
        ev.append(f"Dialogue: 0,{ass_t(c[0]['s'])},{ass_t(c[-1]['e'])},Cap,,0,0,0,,{' '.join(w['w'] for w in c).upper()}")
    open(path, "w", encoding="utf-8").write(head + "\n".join(ev))


def synth(out, dur, mood, fx):
    a, b, c = MOODS[mood]
    sh("ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
       f"aevalsrc='0.12*(sin(2*PI*{a}*t)+0.7*sin(2*PI*{b}*t)+0.5*sin(2*PI*{c}*t))*(0.65+0.35*sin(2*PI*0.12*t))':d={dur:.1f}:s=44100",
       os.path.join(out, "music.wav"))
    for f in fx:
        sh("ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", SFX[f], os.path.join(out, f + ".wav"))


def ask_json(prompt):
    import requests
    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    for _ in range(3):
        r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                          headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]},
                          json={"contents": [{"parts": [{"text": prompt}]}]}, timeout=300)
        if r.status_code != 429:
            break
        time.sleep(30)
    r.raise_for_status()
    t = r.json()["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(t[t.index("{"): t.rindex("}") + 1])


def make_plan(segs, D):
    plan = {"title": "", "mood": "tense", "beats": []}
    if not segs:
        return plan
    tr = "\n".join(f"[{int(s['start'])}s] {s['text']}" for s in segs)
    try:
        p = ask_json(f"""You are a cinematic editor for a dramatic video ({int(D)} seconds). Reply ONLY with JSON:
{{"title":"dramatic title, max 5 words, same language as the speech","mood":"tense|sad|epic|calm",
"beats":[{{"t":seconds,"fx":"impact|whoosh|riser"}}]}}
Rules: 3-6 beats on the most dramatic moments (reveals, confrontations, emotional lines), at least 5 seconds apart,
each between 3 and {int(D) - 3} seconds. TRANSCRIPT:\n{tr}""")
        plan["title"] = str(p.get("title", ""))[:60]
        plan["mood"] = p.get("mood") if p.get("mood") in MOODS else "tense"
        last = -99
        for b in sorted(p.get("beats", []), key=lambda b: float(b["t"])):
            t = float(b["t"])
            if b.get("fx") in SFX and 3 <= t <= D - 3 and t - last >= 5:
                plan["beats"].append({"t": t, "fx": b["fx"]})
                last = t
    except Exception:
        pass  # no AI plan -> still captions + music + grade
    return plan


def render(src, out, quality, segs, plan, D, W, H):
    build_ass(os.path.join(out, "cap.ass"), W, H, segs, plan["title"])
    beats = plan["beats"]
    synth(out, D + 2, plan["mood"], {b["fx"] for b in beats})
    ins = ["-i", src, "-i", "music.wav"]
    for b in beats:
        ins += ["-i", b["fx"] + ".wav"]
    zoom = ""
    if beats:
        z = "+".join(f"between(t,{b['t']},{b['t'] + 0.7})" for b in beats)
        zoom = (f"scale=w='trunc(iw*(1+0.07*({z}))/2)*2':h='trunc(ih*(1+0.07*({z}))/2)*2':eval=frame,"
                f"crop={W}:{H},")
    v = (f"[0:v]scale={W}:{H},setsar=1,{zoom}eq=contrast=1.08:saturation=0.88,vignette=PI/5,"
         f"ass=cap.ass,fade=t=in:d=0.8,fade=t=out:st={max(D - 0.8, 0):.2f}:d=0.8[v]")
    a = "[0:a]asplit=2[sa][sb];[1:a][sa]sidechaincompress=threshold=0.03:ratio=10:attack=30:release=600[md];"
    names = ""
    for i, b in enumerate(beats):
        ms = int(max(0, b["t"] - 2.2 if b["fx"] == "riser" else b["t"]) * 1000)
        a += f"[{i + 2}:a]adelay={ms}|{ms},volume=0.9[b{i}];"
        names += f"[b{i}]"
    a += f"[sb][md]{names}amix=inputs={2 + len(beats)}:normalize=0:duration=first,afade=t=out:st={max(D - 1, 0):.2f}:d=1,alimiter=limit=0.95[a]"
    name = f"edited_{quality}.mp4"
    sh("ffmpeg", "-y", "-loglevel", "error", *ins, "-filter_complex", v + ";" + a, "-map", "[v]", "-map", "[a]",
       "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p", "-c:a", "aac",
       "-b:a", "160k", "-movflags", "+faststart", "-t", f"{D:.2f}", name, cwd=out)
    return name


def worker():
    while True:
        j = jobs[q.get()]
        out = os.path.join(DATA, j["id"])
        try:
            W0, H0, D = probe(j["file"])
            t = 720 if j["quality"] == "720p" else 1080
            W, H = (round(W0 * t / H0 / 2) * 2, t) if W0 >= H0 else (t, round(H0 * t / W0 / 2) * 2)
            j["status"] = "listening (captions)"
            segs = clipper.transcribe(j["file"])
            j["status"] = "AI planning the edit"
            plan = make_plan(segs, D)
            j["status"] = "rendering (takes a while)"
            j["out"] = render(j["file"], out, j["quality"], segs, plan, D, W, H)
            j["plan"] = plan
            j["status"] = "done"
            for f in os.listdir(out):
                if f.startswith("source") or f.endswith((".wav", ".ass", ".mp3")):
                    os.remove(os.path.join(out, f))
        except subprocess.CalledProcessError as e:
            j["status"], j["error"] = "error", (e.stderr or b"").decode(errors="ignore")[-300:]
        except Exception as e:
            j["status"], j["error"] = "error", str(e)[:300]


@app.before_request
def guard():
    if request.path.startswith(("/api/", "/files/")):
        t = request.headers.get("X-Token") or request.args.get("t", "")
        if not hmac.compare_digest(t.encode(), PASSWORD.encode()):
            abort(401)


@app.get("/")
def index():
    return PAGE


@app.get("/<name>")
def web(name):
    if name not in WEB:
        abort(404)
    return send_from_directory(".", name)


@app.get("/api/jobs")
def list_jobs():
    return jsonify(sorted(jobs.values(), key=lambda j: -j["created"]))


@app.post("/api/edit")
def new_edit():
    f = request.files.get("file")
    if not f:
        abort(400)
    jid = uuid.uuid4().hex[:10]
    out = os.path.join(DATA, jid)
    os.makedirs(out)
    ext = os.path.splitext(secure_filename(f.filename or ""))[1][:6] or ".mp4"
    path = os.path.join(out, "source" + ext)
    f.save(path)
    jobs[jid] = {"id": jid, "name": f.filename or "video", "file": path, "status": "queued", "error": "",
                 "quality": "1080p" if request.form.get("q") == "1080p" else "720p", "out": "", "created": time.time()}
    q.put(jid)
    return jsonify(jobs[jid])


@app.get("/files/<jid>/<name>")
def files(jid, name):
    if jid not in jobs:
        abort(404)
    return send_from_directory(os.path.join(DATA, jid), name)


PAGE = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0f0f14"><link rel="manifest" href="/manifest.json">
<link rel="apple-touch-icon" href="/icon-192.png"><title>AI Drama Editor</title>
<style>*{box-sizing:border-box}body{margin:0;background:#0f0f14;color:#f2f2f7;font:16px system-ui,sans-serif;padding:max(16px,env(safe-area-inset-top)) 16px 24px}
h1{font-size:22px}input,select,button{font:inherit;border-radius:12px;border:0;padding:13px;width:100%;margin-bottom:10px}
input,select{background:#1a1a23;color:#f2f2f7}button{background:#7c3aed;color:#fff;font-weight:600}
.c{background:#1a1a23;border-radius:16px;padding:14px;margin-top:14px}.u{font-size:13px;color:#8b8b99}
video{width:100%;border-radius:12px;margin-top:8px;max-height:60vh;background:#000}a.d{display:block;text-align:center;background:#2a2a36;color:#fff;border-radius:12px;padding:11px;margin-top:8px;text-decoration:none}
[hidden]{display:none!important}</style></head><body><h1>&#127916; AI Drama Editor</h1>
<div id="lg"><input id="pw" type="password" placeholder="Password"><button id="go">Sign in</button><p id="er" style="color:#f87171"></p></div>
<div id="ap" hidden><select id="q"><option value="720p">720p (faster)</option><option value="1080p">1080p</option></select>
<input type="file" id="f" accept="video/*" hidden><button id="up">Upload video &amp; auto-edit</button>
<p class="u">AI adds captions, dramatic title, zoom punches, sound effects, music and a cinematic look.</p><div id="js"></div></div>
<script>
const $=s=>document.querySelector(s);let T=localStorage.getItem('t')||'',tm,last='';
const esc=s=>String(s).replace(/[&<>"']/g,c=>'&#'+c.charCodeAt(0)+';');
const api=(p,o={})=>fetch(p,{...o,headers:{'X-Token':T,...(o.headers||{})}});
function show(){$('#lg').hidden=!!T;$('#ap').hidden=!T;if(T)load()}
$('#go').onclick=async()=>{T=$('#pw').value;const r=await api('/api/jobs');if(r.ok){localStorage.setItem('t',T);show()}else{T='';$('#er').textContent='Wrong password'}};
$('#up').onclick=()=>$('#f').click();
$('#f').onchange=async e=>{const f=e.target.files[0];if(!f)return;$('#up').textContent='Uploading...';
 const fd=new FormData();fd.append('file',f);fd.append('q',$('#q').value);
 try{await api('/api/edit',{method:'POST',body:fd})}catch(_){}
 $('#up').textContent='Upload video & auto-edit';e.target.value='';load()};
async function load(){const r=await api('/api/jobs');if(r.status==401){localStorage.removeItem('t');T='';show();return}
 const j=await r.json(),s=JSON.stringify(j);if(s!==last){last=s;
 $('#js').innerHTML=j.map(x=>{const u=`/files/${x.id}/${x.out}?t=${encodeURIComponent(T)}`;
 return `<div class="c"><div class="u">${esc(x.name)} - ${esc(x.status)}</div>${x.error?`<p style="color:#f87171">${esc(x.error)}</p>`:''}
 ${x.status=='done'?`<video controls playsinline preload="metadata" src="${u}"></video><a class="d" href="${u}" download>Download</a>`:''}</div>`}).join('')}
 clearTimeout(tm);if(j.some(x=>!['done','error'].includes(x.status)))tm=setTimeout(load,3000)}
if('serviceWorker'in navigator)navigator.serviceWorker.register('/sw.js');show();
</script></body></html>"""

if __name__ == "__main__":
    threading.Thread(target=worker, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), threaded=True)
