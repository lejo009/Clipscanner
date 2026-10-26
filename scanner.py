
import getpass
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

# ---------------- Einstellungen ----------------
STREAMERS = ["zarbex", "hamedloco", "trymacs", "marcoscm", "montanablack88"]
WINDOW_MIN = 10      # Zeitfenster: so viele Minuten zaehlen wir Clips
MIN_CLIPS = 5        # mindestens so viele Clips im Fenster fuer einen Alarm
FACTOR = 3           # Alarm erst, wenn es X-mal mehr als normal ist
CHECK_EVERY = 60     # Sekunden zwischen zwei Pruefungen (nur im Dauer-Modus)
COOLDOWN_MIN = 20    # nach einem Alarm so lange Ruhe fuer diesen Streamer
STATE_FILE = "state.json"
# ------------------------------------------------

API = "https://api.twitch.tv/helix"
LOCAL = ZoneInfo("Europe/Berlin")
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def to_dt(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00")) if text else EPOCH


def from_dt(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class Twitch:
    def __init__(self, client_id, client_secret):
        self.client_id = client_id
        self.client_secret = client_secret
        self.token = None
        self.login()

    def login(self):
        r = requests.post(
            "https://id.twitch.tv/oauth2/token",
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "client_credentials",
            },
            timeout=15,
        )
        r.raise_for_status()
        self.token = r.json()["access_token"]

    def get(self, path, params):
        for attempt in range(2):
            r = requests.get(
                f"{API}/{path}",
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Client-Id": self.client_id,
                },
                params=params,
                timeout=15,
            )
            if r.status_code == 401 and attempt == 0:
                self.login()
                continue
            r.raise_for_status()
            return r.json()

    def user_ids(self, logins):
        data = self.get("users", [("login", l) for l in logins]).get("data", [])
        return {u["login"].lower(): u["id"] for u in data}

    def clips_since(self, user_id, since, max_pages=5):
        clips, cursor = [], None
        for _ in range(max_pages):
            params = {
                "broadcaster_id": user_id,
                "started_at": from_dt(since),
                "first": 100,
            }
            if cursor:
                params["after"] = cursor
            resp = self.get("clips", params)
            clips += resp.get("data", [])
            cursor = resp.get("pagination", {}).get("cursor")
            if not cursor:
                break
        return clips


def push(topic, title, message, click_url=None):
    headers = {"Title": title, "Priority": "high", "Tags": "fire"}
    if click_url:
        headers["Click"] = click_url
    r = requests.post(
        f"https://ntfy.sh/{topic}",
        data=message.encode("utf-8"),
        headers=headers,
        timeout=15,
    )
    r.raise_for_status()


def check_streamer(twitch, topic, name, uid, state, now):
    if now - to_dt(state.get("baseline_time")) > timedelta(minutes=15):
        old = twitch.clips_since(uid, now - timedelta(hours=6), max_pages=10)
        state["baseline"] = len(old) / (6 * 60 / WINDOW_MIN)
        state["baseline_time"] = from_dt(now)

    recent = twitch.clips_since(uid, now - timedelta(minutes=WINDOW_MIN))
    count = len(recent)
    baseline = state["baseline"]
    threshold = max(MIN_CLIPS, FACTOR * baseline)

    line = f"{name}: {count} Clips jetzt, normal ca. {baseline:.1f}, Alarm ab {threshold:.0f}"
    cooldown_over = now - to_dt(state.get("last_alert")) > timedelta(minutes=COOLDOWN_MIN)

    if count >= threshold and cooldown_over:
        best = sorted(recent, key=lambda c: c["view_count"], reverse=True)[:3]
        lines = [f"{count} Clips in {WINDOW_MIN} Min (normal ca. {baseline:.1f})"]
        for c in best:
            created = to_dt(c["created_at"]).astimezone(LOCAL)
            lines.append(f"- {c['title']} ({c['view_count']} Views, {created:%H:%M} Uhr)")
        push(topic, f"HOT MOMENT: {name}", "\n".join(lines), best[0]["url"])
        state["last_alert"] = from_dt(now)
        line += "  <-- ALARM gesendet"
    return line


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(states):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(states, f)


def ask(env_name, label, secret=True):
    value = os.environ.get(env_name)
    if not value:
        value = (getpass.getpass if secret else input)(f"{label}: ")
    return value.strip()


def describe(e):
    resp = getattr(e, "response", None)
    if resp is not None:
        return f"{type(e).__name__} {resp.status_code}: {resp.text[:150]}"
    return type(e).__name__


def run_once(twitch, topic, ids, states):
    now = datetime.now(timezone.utc)
    lines = []
    for name, uid in ids.items():
        try:
            lines.append(check_streamer(twitch, topic, name, uid, states.setdefault(name, {}), now))
        except Exception as e:
            lines.append(f"{name}: Fehler ({describe(e)})")
    for line in lines:
        print(line)
    return lines


def setup(client_id, client_secret):
    twitch = Twitch(client_id, client_secret)
    ids = twitch.user_ids(STREAMERS)
    missing = [s for s in STREAMERS if s.lower() not in ids]
    if missing:
        print("Nicht gefunden (Name pruefen):", ", ".join(missing))
    if not ids:
        raise RuntimeError("Keiner der Streamer wurde gefunden. Namen in STREAMERS pruefen.")
    return twitch, ids, missing


def main():
    once = "--once" in sys.argv
    client_id = ask("TWITCH_CLIENT_ID", "Client ID")
    client_secret = ask("TWITCH_CLIENT_SECRET", "Client Secret")
    topic = ask("NTFY_TOPIC", "ntfy-Topic", secret=False)

    if once:
        states = load_state()
        meta = states.setdefault("_meta", {})
        try:
            twitch, ids, missing = setup(client_id, client_secret)
        except Exception as e:
            reason = str(e) if isinstance(e, RuntimeError) else describe(e)
            print("Fehler beim Einrichten:", reason)
            if not meta.get("error_pushed"):
                try:
                    push(topic, "Scanner-Fehler", f"Einrichtung fehlgeschlagen: {reason}")
                    meta["error_pushed"] = True
                except Exception as push_error:
                    print("Auch die Push-Nachricht ging nicht:", describe(push_error))
            save_state(states)
            sys.exit(1)

        lines = run_once(twitch, topic, ids, states)
        meta["error_pushed"] = False
        if not meta.get("setup_done"):
            text = "\n".join(lines)
            if missing:
                text += "\nNicht gefunden: " + ", ".join(missing)
            push(topic, "Scanner eingerichtet", text)
            meta["setup_done"] = True
        save_state(states)
        return

    twitch, ids, missing = setup(client_id, client_secret)
    push(topic, "Scanner gestartet", "Der Scanner laeuft. Bei einem HOT MOMENT melde ich mich.")
    print(f"Scanner laeuft, Pruefung alle {CHECK_EVERY} Sekunden. Zum Stoppen: Stopp-Knopf.\n")
    states = {}
    try:
        while True:
            print(datetime.now(LOCAL).strftime("%H:%M:%S"))
            run_once(twitch, topic, ids, states)
            time.sleep(CHECK_EVERY)
    except KeyboardInterrupt:
        print("\nScanner gestoppt.")


if __name__ == "__main__":
    main()
