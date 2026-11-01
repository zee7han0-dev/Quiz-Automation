import subprocess, time, json, os, sys, io
import urllib.request, urllib.error, base64
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from PIL import Image      # optional: pip install pillow (makes screenshots smaller/faster)
except ImportError:
    Image = None

# =====================  THINGS YOU MAY NEED TO EDIT  =====================
ADB_PATH = r"D:\scrcpy-win64-v5.0.1\adb.exe"

# Raced in parallel; the first valid answer wins. Names must match `--models` output.
MODELS = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]

TOTAL_QUESTIONS = 5            # questions per subject
MIN_SECONDS_PER_QUESTION = 6   # keeps you under 10 requests/min per model (free tier)
USE_TEXT_MODE = False          # read text via uiautomator first, screenshot as fallback

# Update these for the phone you're using (Developer options > Pointer location)
OPTION_COORDS = {
    'A': (530, 1143), 'B': (530, 1360), 'C': (530, 1600), 'D': (530, 1800),
    'NEXT': (750, 2321), 'SUBMIT': (775, 300), 'CONFIRM': (510, 2020),
}
# ========================================================================

API_KEY = os.environ.get("GEMINI_API_KEY")  # setx GEMINI_API_KEY "key", then open a NEW terminal
if not API_KEY:
    sys.exit("Set the GEMINI_API_KEY environment variable first.")

PROMPT = ("This is a multiple-choice question. The options are in order: the first "
          "option is A, the second is B, the third is C, the fourth is D. "
          "Reply with ONLY the letter of the correct option: A, B, C, or D.")


def list_models():
    url = "https://generativelanguage.googleapis.com/v1beta/models"
    req = urllib.request.Request(url, headers={"x-goog-api-key": API_KEY})
    with urllib.request.urlopen(req, timeout=10) as r:
        for m in json.loads(r.read())["models"]:
            if "generateContent" in m.get("supportedGenerationMethods", []):
                print(m["name"])


def screenshot_part():
    png = subprocess.run([ADB_PATH, "exec-out", "screencap", "-p"],
                         capture_output=True).stdout
    mime = "image/png"
    if Image is not None:
        try:
            img = Image.open(io.BytesIO(png)).convert("RGB")
            img.thumbnail((1000, 2200))   # raise if small text gets misread
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=85)
            png, mime = buf.getvalue(), "image/jpeg"
        except Exception:
            pass
    return {"inline_data": {"mime_type": mime, "data": base64.b64encode(png).decode()}}


def screen_text():
    """Read on-screen text through Android's accessibility tree. Returns None if unavailable."""
    try:
        out = subprocess.run(
            [ADB_PATH, "exec-out", "uiautomator", "dump", "/dev/tty"],
            capture_output=True, text=True, encoding="utf-8", errors="ignore",
            timeout=10).stdout
        start, end = out.find("<?xml"), out.rfind("</hierarchy>")
        if start == -1 or end == -1:
            return None
        root = ET.fromstring(out[start:end + len("</hierarchy>")])
        lines = []
        for n in root.iter("node"):
            t = (n.get("text") or n.get("content-desc") or "").strip()
            if t:
                lines.append(t)
        return "\n".join(lines)
    except Exception:
        return None


def call_model(model, body):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    try:
        req = urllib.request.Request(url, data=body, headers={
            "Content-Type": "application/json", "x-goog-api-key": API_KEY})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read())
        text = data["candidates"][0]["content"]["parts"][0]["text"].strip().upper()
        if text and text[0] in "ABCD":
            return text[0]
        print(f"  [{model}] unexpected reply: {text!r}")
    except urllib.error.HTTPError as e:
        print(f"  [{model}] HTTP {e.code}: {e.read().decode()[:200]}")
    except Exception as e:
        print(f"  [{model}] {type(e).__name__}: {e}")
    return None


def ask(parts):
    body = json.dumps({
        "contents": [{"parts": parts}],
        "generationConfig": {"maxOutputTokens": 300, "temperature": 0},
    }).encode()

    for _ in range(2):  # two rounds, each racing all models
        ex = ThreadPoolExecutor(len(MODELS))
        futures = [ex.submit(call_model, m, body) for m in MODELS]
        try:
            for f in as_completed(futures):
                if f.result():
                    return f.result()
        finally:
            ex.shutdown(wait=False, cancel_futures=True)
    return None


def get_answer():
    if USE_TEXT_MODE:
        text = screen_text()
        if text and len(text) > 40:
            ans = ask([{"text": PROMPT + "\n\nScreen text:\n" + text}])
            if ans:
                return ans
            print("  text mode failed, falling back to screenshot")
    return ask([{"text": PROMPT}, screenshot_part()])


def tap(name):
    x, y = OPTION_COORDS[name]
    subprocess.run([ADB_PATH, "shell", "input", "tap", str(x), str(y)])


# ---------------- command-line helpers ----------------
if "--models" in sys.argv:        # list model names your key can use
    list_models()
    sys.exit()

if "--dump" in sys.argv:          # show what text mode can see on the current screen
    print(screen_text())
    sys.exit()

if "--test" in sys.argv:          # one answer + timing, taps nothing
    t = time.time()
    print("Answer:", get_answer(), f"({time.time() - t:.1f}s)")
    sys.exit()

DRY_RUN = "--dry" in sys.argv     # full run but skips Submit/Confirm

# ---------------- main run ----------------
input("Open question 1, then press ENTER...")
for i in range(1, TOTAL_QUESTIONS + 1):
    print(f"\n--- Question {i}/{TOTAL_QUESTIONS} ---")
    q_start = time.time()

    answer = get_answer()
    print(f"AI answer: {answer}  ({time.time() - q_start:.1f}s)")

    if answer is None:
        input("AI failed. Answer this one manually, then press ENTER to continue...")
    else:
        tap(answer)
        time.sleep(0.3)

    if i < TOTAL_QUESTIONS:
        tap('NEXT')
        # wait out the rest of the minimum time; adds no delay if the API was already slow
        time.sleep(max(0.5, MIN_SECONDS_PER_QUESTION - (time.time() - q_start)))

if DRY_RUN:
    print("\nDry run finished. Submit and Confirm were NOT tapped.")
else:
    tap('SUBMIT')
    time.sleep(0.3)
    tap('CONFIRM')
    print("\nDone.")