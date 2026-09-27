# ============================================================
# AGNI-X PERSONAL ASSISTANT
# Presentation-ready laptop assistant for desktop use
# ============================================================

import datetime
import json
import logging
import math
import os
import platform
import queue
import re
import subprocess
import threading
import traceback
import webbrowser
from pathlib import Path
from urllib.parse import quote_plus

import customtkinter as ctk
import pyttsx3
import pywhatkit
import requests
import speech_recognition as sr
import tkinter as tk

try:
    import pyautogui
except ImportError:  # pragma: no cover
    pyautogui = None

try:
    from pywinauto import Desktop
except ImportError:  # pragma: no cover
    Desktop = None

try:
    from openai import OpenAI
except Exception:  # pragma: no cover
    OpenAI = None


# ================= CONFIG =================

APP_TITLE = "AGNI-X PERSONAL ASSISTANT"
VOICE_NAME = "en-IN-PrabhatNeural"
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
MAX_CHAT_HISTORY = 14
ASSISTANT_NAME = "AGNI-X"
PRESENTATION_TAGLINE = "AI for learning, productivity, and intelligent laptop control"
DEFAULT_CITY = "Ghaziabad"

BASE_DIR = Path(__file__).resolve().parent
MEMORY_FILE = BASE_DIR / "memory.json"
CONTACTS_FILE = BASE_DIR / "contacts.json"
NICKNAMES_FILE = BASE_DIR / "nicknames.json"
TASKS_FILE = BASE_DIR / "tasks.json"
NOTES_FILE = BASE_DIR / "notes.json"
VOICE_FILE = BASE_DIR / "myvoice.wav"
TEMP_VOICE_FILE = BASE_DIR / "temp.wav"
ICON_FILE = BASE_DIR / "AGNI_X_ICON.ico"
SCREENSHOT_DIR = BASE_DIR / "screenshots"

WAKE_WORDS = (
    "agni",
    "hey agni",
    "hi agni",
    "hello agni",
    "wake up agni",
)

# ================= LOGGING =================

logging.basicConfig(
    filename=str(BASE_DIR / "agni.log"),
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(threadName)s | %(message)s",
)
logger = logging.getLogger("agni_x")


# ================= WINDOWS CONTROL =================

class WindowsController:
    """Controlled Windows app discovery, launching, and foreground UI actions."""

    def __init__(self):
        self.app_index = {}
        self.refresh_apps()

    @staticmethod
    def _clean_name(path):
        return re.sub(r"\s+", " ", path.stem.replace(" - Shortcut", "")).strip().lower()

    def refresh_apps(self):
        locations = [
            Path(os.environ.get("PROGRAMDATA", "C:\\ProgramData")) / "Microsoft/Windows/Start Menu/Programs",
            Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
            Path.home() / "Desktop",
        ]
        index = {}
        for location in locations:
            if not location.exists():
                continue
            for path in location.rglob("*"):
                if path.suffix.lower() not in {".lnk", ".url", ".exe"}:
                    continue
                name = self._clean_name(path)
                if name and name not in index:
                    index[name] = path
        self.app_index = index
        return index

    def available_apps(self, limit=30):
        return sorted(self.app_index)[:limit]

    def resolve_app(self, requested):
        requested = normalize_text(requested)
        if not requested:
            return None
        if requested in self.app_index:
            return self.app_index[requested]
        matches = [path for name, path in self.app_index.items() if requested in name or name in requested]
        return sorted(matches, key=lambda path: len(path.name))[0] if matches else None

    def launch(self, requested):
        path = self.resolve_app(requested)
        if path is None:
            return False, f"I could not find an installed app named {requested}."
        try:
            if path.suffix.lower() in {".lnk", ".url"}:
                os.startfile(str(path))
            else:
                subprocess.Popen([str(path)], close_fds=True)
            return True, f"Opening {path.stem.replace(' - Shortcut', '')}."
        except (OSError, subprocess.SubprocessError):
            logger.exception("Could not launch app %s", path)
            return False, f"I could not open {path.stem}."

    def focus(self, requested):
        if Desktop is None:
            return False, "Window control needs pywinauto installed."
        try:
            window = Desktop(backend="uia").window(title_re=f"(?i).*{re.escape(requested)}.*")
            if not window.exists(timeout=2):
                return False, f"I could not find an open window for {requested}."
            window.restore()
            window.set_focus()
            return True, f"Focused {requested}."
        except Exception:
            logger.exception("Could not focus window %s", requested)
            return False, f"I could not focus {requested}."

    def type_text(self, text):
        if pyautogui is None:
            return False, "Typing control needs pyautogui installed."
        try:
            pyautogui.write(text, interval=0.01)
            return True, "Text entered."
        except Exception:
            logger.exception("Could not type text")
            return False, "I could not enter that text."

    def press_key(self, key):
        if pyautogui is None:
            return False, "Keyboard control needs pyautogui installed."
        try:
            pyautogui.press(key)
            return True, f"Pressed {key}."
        except Exception:
            logger.exception("Could not press key %s", key)
            return False, f"I could not press {key}."

    def click_control(self, label):
        if Desktop is None:
            return False, "Button control needs pywinauto installed."
        try:
            control = Desktop(backend="uia").get_active().child_window(title=label, control_type="Button")
            if not control.exists(timeout=2):
                return False, f"I could not find a button named {label}."
            control.click_input()
            return True, f"Clicked {label}."
        except Exception:
            logger.exception("Could not click control %s", label)
            return False, f"I could not click {label}."

    def active_window_title(self):
        if Desktop is None:
            return None
        try:
            window = Desktop(backend="uia").get_active()
            return window.window_text().strip() or None
        except Exception:
            logger.exception("Could not read the active window")
            return None

    def capture_screen(self):
        if pyautogui is None:
            return None
        try:
            SCREENSHOT_DIR.mkdir(exist_ok=True)
            filename = SCREENSHOT_DIR / f"screen_{datetime.datetime.now():%Y%m%d_%H%M%S}.png"
            pyautogui.screenshot().save(filename)
            return filename
        except Exception:
            logger.exception("Could not capture the screen")
            return None


# ================= HELPERS =================


def safe_load_json(filename, default):
    if not filename.exists():
        return default
    try:
        with open(filename, "r", encoding="utf-8") as file:
            return json.load(file)
    except Exception:
        logger.exception("Could not load %s", filename)
        return default


def safe_save_json(filename, data):
    temporary_file = filename.with_suffix(filename.suffix + ".tmp")
    try:
        with open(temporary_file, "w", encoding="utf-8") as file:
            json.dump(data, file, indent=4, ensure_ascii=False)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_file, filename)
        return True
    except Exception:
        logger.exception("Could not save %s", filename)
        try:
            temporary_file.unlink(missing_ok=True)
        except OSError:
            logger.exception("Could not remove temporary save file %s", temporary_file)
        return False


def normalize_text(value):
    return " ".join(str(value).strip().lower().split())


# ================= AGNI-X APP =================

class AgniXApp:
    def __init__(self):
        self.memory = safe_load_json(MEMORY_FILE, {})
        self.contacts = safe_load_json(CONTACTS_FILE, {})
        self.nicknames = safe_load_json(NICKNAMES_FILE, {})
        self.tasks = safe_load_json(TASKS_FILE, [])
        self.notes = safe_load_json(NOTES_FILE, [])

        self.chat_history = []
        self.user_name = self.memory.get("user_name", "User")
        self.city = self.memory.get("city", DEFAULT_CITY)
        self.profile = self.memory.get("profile", {
            "school": "Not set",
            "goal": "Academic productivity and smart assistance",
            "role": "student",
        })
        api_key = os.getenv("OPENAI_API_KEY")
        self.client = OpenAI(api_key=api_key) if OpenAI and api_key else None
        self.windows = WindowsController()

        self.stop_event = threading.Event()
        self.listener_thread = None
        self.tts_queue = queue.Queue()
        self.tts_done = threading.Event()
        self.tts_done.set()
        self.recognizer = sr.Recognizer()
        self.recognizer.dynamic_energy_threshold = True
        self.recognizer.pause_threshold = 0.7
        self.recognizer.non_speaking_duration = 0.45
        self.running_lock = threading.Lock()
        self.command_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.agni_state = "idle"
        self.is_running = False
        self.presentation_mode = False
        self.pending_command = None

        self.arc_angle = 0
        self.core_phase = 0
        self.scan_phase = 0

        self.app = None
        self.status_text = None
        self.output_box = None
        self.arc_canvas = None
        self.heard_text = None
        self.activate_button = None
        self.stop_button = None
        self.command_entry = None
        self.send_button = None
        self.clock_label = None
        self.date_label = None
        self.weather_temp_label = None
        self.weather_desc_label = None
        self.weather_city_label = None
        self.welcome_label = None
        self.system_metric_1 = None
        self.system_metric_2 = None
        self.system_metric_3 = None

        self.tts_worker = threading.Thread(
            target=self._tts_worker_loop,
            name="TTS-Worker",
            daemon=True,
        )
        self.tts_worker.start()

    # ================= SAFE GUI METHODS =================

    def ui(self, callback):
        if self.app is not None and self.app.winfo_exists():
            try:
                self.app.after(0, callback)
            except tk.TclError:
                logger.exception("Could not schedule GUI callback")

    def set_state(self, state):
        with self.state_lock:
            self.agni_state = state

        labels = {
            "idle": "SYSTEM IDLE",
            "listening": "LISTENING",
            "processing": "THINKING",
            "speaking": "SPEAKING",
            "stopped": "SYSTEM OFFLINE",
        }
        colors = {
            "idle": "#39D9FF",
            "listening": "#66FFCC",
            "processing": "#FFD166",
            "speaking": "#A78BFA",
            "stopped": "#607080",
        }

        def update():
            label = labels.get(state, str(state).upper())
            color = colors.get(state, "#39D9FF")
            if self.status_text is not None:
                self.status_text.configure(text=label, text_color=color)

        self.ui(update)

    def set_heard(self, text):
        def update():
            if self.heard_text is not None:
                shown = text if text else "Waiting for voice input..."
                self.heard_text.configure(text=f"HEARD  ›  {shown}")

        self.ui(update)

    def add_output(self, speaker, text):
        def update():
            if self.output_box is None:
                return
            self.output_box.insert("end", f"{speaker}: {text}\n")
            self.output_box.see("end")

        self.ui(update)

    def set_buttons(self, running):
        def update():
            if self.activate_button is not None:
                self.activate_button.configure(state="disabled" if running else "normal")
            if self.stop_button is not None:
                self.stop_button.configure(state="normal" if running else "disabled")

        self.ui(update)

    # ================= TTS =================

    def speak(self, text):
        text = str(text).strip()
        if not text:
            return
        logger.info("Agni says: %s", text)
        self.tts_done.clear()
        self.add_output("AGNI", text)
        self.tts_queue.put(text)

    def _tts_worker_loop(self):
        try:
            engine = pyttsx3.init()
        except Exception:
            logger.exception("Could not initialize pyttsx3")
            engine = None

        while True:
            text = self.tts_queue.get()
            if text is None:
                self.tts_queue.task_done()
                break
            try:
                if self.is_running:
                    self.set_state("speaking")
                if engine is None:
                    engine = pyttsx3.init()
                engine.say(text)
                engine.runAndWait()
            except Exception:
                logger.exception("TTS worker failed")
            finally:
                self.tts_queue.task_done()
                if self.tts_queue.empty():
                    self.tts_done.set()
                if self.is_running and not self.stop_event.is_set():
                    self.set_state("idle")

    # ================= VOICE =================

    def take_command(self, timeout=5, phrase_time_limit=6):
        if self.stop_event.is_set():
            return ""
        try:
            self.set_state("listening")
            with sr.Microphone() as source:
                self.recognizer.adjust_for_ambient_noise(source, duration=0.5)
                audio = self.recognizer.listen(source, timeout=timeout, phrase_time_limit=phrase_time_limit)
            if self.stop_event.is_set():
                return ""
            self.set_state("processing")
            command = self.recognizer.recognize_google(audio, language="en-IN").strip().lower()
            logger.info("User command: %s", command)
            self.set_heard(command)
            self.add_output("YOU", command)
            return command
        except sr.WaitTimeoutError:
            return ""
        except sr.UnknownValueError:
            return ""
        except sr.RequestError:
            logger.exception("Speech recognition request failed")
            self.speak("Speech recognition is not available right now.")
            return ""
        except OSError:
            logger.exception("Microphone could not be opened")
            self.speak("I could not access the microphone.")
            return ""
        except Exception:
            logger.exception("Unexpected error while listening")
            return ""
        finally:
            if self.is_running and not self.stop_event.is_set():
                with self.state_lock:
                    current = self.agni_state
                if current != "speaking":
                    self.set_state("idle")

    # ================= DATA MANAGEMENT =================

    def remember(self, key, value):
        self.memory[key] = value
        return safe_save_json(MEMORY_FILE, self.memory)

    def recall(self, key):
        return self.memory.get(key)

    def save_contacts(self):
        return safe_save_json(CONTACTS_FILE, self.contacts)

    def save_nicknames(self):
        return safe_save_json(NICKNAMES_FILE, self.nicknames)

    def save_tasks(self):
        return safe_save_json(TASKS_FILE, self.tasks)

    def save_notes(self):
        return safe_save_json(NOTES_FILE, self.notes)

    # ================= CONTACTS & MESSAGING =================

    def find_contact(self, name):
        name = normalize_text(name)
        if not name:
            return None
        if name in self.nicknames:
            name = normalize_text(self.nicknames[name])
        if name in self.contacts:
            return name
        for contact_name in self.contacts:
            if contact_name in name or name in contact_name:
                return contact_name
        return None

    def add_contact(self):
        self.speak("What is the contact name?")
        name = self.take_command()
        if not name:
            self.speak("No name detected.")
            return
        self.speak("Now say the phone number.")
        number = self.take_command(timeout=8, phrase_time_limit=12)
        if not number:
            self.speak("No phone number detected.")
            return
        cleaned = "".join(char for char in number if char.isdigit() or char == "+")
        if len("".join(ch for ch in cleaned if ch.isdigit())) < 10:
            self.speak("That number looks incomplete.")
            return
        self.contacts[name.lower()] = cleaned
        if self.save_contacts():
            self.speak(f"Contact saved for {name}.")
        else:
            self.speak("I could not save the contact.")

    def add_nickname(self):
        self.speak("Say the nickname.")
        nickname = self.take_command()
        if not nickname:
            self.speak("No nickname detected.")
            return
        self.speak("Who is this nickname for?")
        name = self.take_command()
        if not name:
            self.speak("No contact name detected.")
            return
        self.nicknames[nickname.lower()] = name.lower()
        if self.save_nicknames():
            self.speak("Nickname saved.")
        else:
            self.speak("I could not save the nickname.")

    def call_contact(self):
        self.speak("Who should I call?")
        name = self.take_command()
        if not name:
            self.speak("I did not catch the name.")
            return
        matched = self.find_contact(name)
        if not matched:
            self.speak("Contact not found.")
            return
        number = self.contacts[matched].replace("+", "").replace(" ", "")
        self.speak(f"Opening WhatsApp for {matched}.")
        webbrowser.open(f"https://wa.me/{number}")

    def send_message(self):
        self.speak("Who should I message?")
        name = self.take_command()
        if not name:
            self.speak("I did not catch the name.")
            return
        matched = self.find_contact(name)
        if not matched:
            self.speak("Contact not found.")
            return
        self.speak(f"What message should I send to {matched}?")
        message = self.take_command(timeout=8, phrase_time_limit=12)
        if not message:
            self.speak("No message detected.")
            return
        try:
            now = datetime.datetime.now() + datetime.timedelta(minutes=2)
            pywhatkit.sendwhatmsg(
                self.contacts[matched],
                message,
                now.hour,
                now.minute,
                wait_time=15,
                tab_close=True,
                close_time=3,
            )
            self.speak("Message scheduled successfully.")
        except Exception:
            logger.exception("WhatsApp message failed")
            self.speak("I could not send the message.")

    # ================= NOTES / TASKS =================

    def add_note(self):
        self.speak("What note should I save?")
        note = self.take_command(timeout=10, phrase_time_limit=18)
        if not note:
            self.speak("No note detected.")
            return
        self.notes.append({
            "text": note,
            "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        })
        if self.save_notes():
            self.speak("Note saved.")
        else:
            self.speak("I could not save the note.")

    def list_notes(self):
        if not self.notes:
            self.speak("You do not have any saved notes.")
            return
        self.speak("Here are your notes.")
        for index, item in enumerate(self.notes[-5:], start=1):
            text = item.get("text", "")
            self.speak(f"Note {index}: {text}")

    def add_task(self):
        self.speak("What task should I add?")
        task = self.take_command(timeout=10, phrase_time_limit=18)
        if not task:
            self.speak("No task detected.")
            return
        self.tasks.append({
            "task": task,
            "done": False,
            "created": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        })
        if self.save_tasks():
            self.speak(f"Task added: {task}")
        else:
            self.speak("I could not save the task.")

    def list_tasks(self):
        if not self.tasks:
            self.speak("Your task list is empty.")
            return
        pending = [item for item in self.tasks if not item.get("done", False)]
        if not pending:
            self.speak("All tasks are completed.")
            return
        self.speak("Here are your active tasks.")
        for index, item in enumerate(pending, start=1):
            self.speak(f"Task {index}: {item.get('task', '')}")

    def complete_task(self):
        if not self.tasks:
            self.speak("You have no tasks.")
            return
        self.speak("Which task number should I mark complete?")
        choice = self.take_command(timeout=8, phrase_time_limit=10)
        try:
            number = int(choice)
        except ValueError:
            self.speak("I did not catch a valid task number.")
            return
        pending = [item for item in self.tasks if not item.get("done", False)]
        if number < 1 or number > len(pending):
            self.speak("That task number does not exist.")
            return
        target = pending[number - 1]
        target["done"] = True
        if self.save_tasks():
            self.speak(f"Task completed: {target.get('task', '')}")
        else:
            self.speak("I could not update the task.")

    # ================= DESKTOP / BROWSER / SYSTEM =================

    def control_windows(self, action, value=""):
        actions = {
            "open": self.windows.launch,
            "focus": self.windows.focus,
            "type": self.windows.type_text,
            "press": self.windows.press_key,
            "click": self.windows.click_control,
        }
        handler = actions.get(action)
        if handler is None:
            return
        _, message = handler(value)
        self.speak(message)

    def describe_screen(self):
        title = self.windows.active_window_title()
        screenshot = self.windows.capture_screen()
        if title and screenshot:
            self.speak(f"The active window is {title}. I saved a screenshot to {screenshot.name}.")
        elif title:
            self.speak(f"The active window is {title}.")
        elif screenshot:
            self.speak(f"I saved a screenshot to {screenshot.name}, but I could not read the active window title.")
        else:
            self.speak("I could not inspect the screen right now.")

    def request_confirmation(self, command):
        self.pending_command = command
        self.speak(f"This action can have an external effect: {command}. Say confirm to continue or cancel to stop.")

    def run_task_plan(self, plan):
        steps = [step.strip() for step in re.split(r"\s+(?:and then|then)\s+", plan) if step.strip()]
        if len(steps) < 2:
            self.speak("Give me at least two steps separated by then.")
            return
        self.speak(f"I will run {len(steps)} steps.")
        for number, step in enumerate(steps, start=1):
            self.add_output("AGNI", f"Step {number}/{len(steps)}: {step}")
            self.speak(f"Step {number}: {step}")
            self.handle_command(step)
            if self.pending_command is not None:
                self.speak("The plan is paused until you confirm or cancel the pending action.")
                return

    def list_installed_apps(self):
        self.windows.refresh_apps()
        apps = self.windows.available_apps()
        if not apps:
            self.speak("I could not find application shortcuts.")
            return
        self.speak("Installed apps include " + ", ".join(apps) + ".")

    def open_website(self, url, label):
        webbrowser.open(url)
        self.speak(f"Opening {label}.")

    def open_youtube(self):
        self.open_website("https://www.youtube.com", "YouTube")

    def open_google(self):
        self.open_website("https://www.google.com", "Google")

    def open_email(self):
        self.open_website("https://mail.google.com", "Gmail")

    def open_portal(self):
        self.open_website("https://classroom.google.com", "Google Classroom")

    def open_local_app(self, app_name):
        targets = {
            "notepad": "notepad.exe",
            "calculator": "calc.exe",
            "paint": "mspaint.exe",
            "browser": "msedge.exe",
            "explorer": "explorer.exe",
            "settings": "ms-settings:",
            "command": "cmd.exe",
            "terminal": "powershell.exe",
        }
        target = targets.get(app_name.lower())
        if not target:
            self.speak(f"I do not know how to open {app_name}.")
            return
        try:
            if target.startswith("ms-"):
                os.startfile(target)
            else:
                subprocess.Popen([target], close_fds=True)
            self.speak(f"Opening {app_name}.")
        except Exception:
            logger.exception("Failed to open local app: %s", app_name)
            self.speak(f"I could not open {app_name}.")

    def search_web(self, query=None):
        if not query:
            self.speak("What should I search for?")
            query = self.take_command()
        if not query:
            self.speak("I did not catch the search.")
            return
        webbrowser.open(f"https://www.google.com/search?q={quote_plus(query)}")
        self.speak("Searching Google.")

    def smart_play(self, query=None):
        if not query:
            self.speak("What should I play?")
            query = self.take_command()
        if not query:
            self.speak("I did not catch that.")
            return
        encoded = quote_plus(query)
        if any(word in query for word in ("song", "music", "playlist", "album", "audio")):
            webbrowser.open(f"https://open.spotify.com/search/{encoded}")
            self.speak("Opening Spotify.")
        else:
            webbrowser.open(f"https://www.youtube.com/results?search_query={encoded}")
            self.speak("Opening YouTube results.")

    def tell_time(self):
        current_time = datetime.datetime.now().strftime("%I:%M %p")
        self.speak(f"The time is {current_time}.")

    def tell_date(self):
        value = datetime.datetime.now().strftime("%A, %d %B %Y")
        self.speak(f"Today is {value}.")

    def get_weather(self, requested_city=None):
        city = normalize_text(requested_city) if requested_city else self.city
        if not requested_city:
            self.speak(f"Which city? Say skip to use {city}.")
            requested_city = self.take_command()
            if requested_city and requested_city != "skip":
                city = requested_city
        if not city:
            self.speak("I did not catch the city.")
            return
        try:
            response = requests.get(f"https://wttr.in/{quote_plus(city)}", params={"format": "3"}, timeout=10)
            response.raise_for_status()
            self.speak(response.text.strip())
        except Exception:
            logger.exception("Weather request failed")
            self.speak("I could not fetch the weather.")

    # ================= AI / LOCAL HELP =================

    def generate_local_reply(self, question):
        question = normalize_text(question)
        if not question:
            return "I did not catch the question."

        if "hello" in question or "hi" in question:
            return f"Hello! I am {ASSISTANT_NAME}. I can handle productivity, notes, searches, phone tasks, and desktop control for you."
        if "who are you" in question or "what are you" in question:
            return f"I am {ASSISTANT_NAME}, your personal AI assistant. I can manage tasks, search the web, open apps, answer questions, and help with daily laptop work."
        if "what can you do" in question or "help" in question:
            return (
                "I can open websites, search on Google, play music, tell time and date, manage notes and tasks, "
                "save contacts, send messages, call contacts, open local apps, and support presentations."
            )
        if "time" in question:
            return f"The current time is {datetime.datetime.now().strftime('%I:%M %p')}."
        if "date" in question:
            return f"Today is {datetime.datetime.now().strftime('%A, %d %B %Y')}."
        if "presentation" in question:
            return "Presentation mode is ready. I can help with a structure, speaking cues, project overview, and quick research support."
        if "name" in question and "my" in question:
            return f"Your name is saved as {self.user_name}."

        note = self.recall("note")
        if question.startswith("remember") and note:
            return f"I remember: {note}"

        return (
            "I can help with that. I can open websites, manage tasks, search online, answer questions, and support your productivity and presentations."
        )

    def set_user_profile(self, name=None, school=None, goal=None):
        if name:
            self.user_name = name
            self.memory["user_name"] = name
        if school:
            self.profile["school"] = school
            self.memory["profile"] = self.profile
        if goal:
            self.profile["goal"] = goal
            self.memory["profile"] = self.profile
        safe_save_json(MEMORY_FILE, self.memory)

    def set_city(self, city):
        city = normalize_text(city).title()
        if not city:
            self.speak("Please provide a city name.")
            return
        self.city = city
        self.remember("city", city)
        self.speak(f"Done. I will use {city} for weather updates.")

    def get_assistant_summary(self):
        task_count = len(self.tasks)
        pending = sum(1 for item in self.tasks if not item.get("done", False))
        note_count = len(self.notes)
        return (
            f"Welcome back {self.user_name}. Today you have {pending} pending tasks, "
            f"{note_count} saved notes, and {task_count} total tracked tasks. "
            f"Your focus is {self.profile.get('goal', 'smart productivity')}."
        )

    def show_dashboard_summary(self):
        self.speak(self.get_assistant_summary())

    def toggle_presentation_mode(self):
        self.presentation_mode = not self.presentation_mode
        if self.presentation_mode:
            self.speak("Presentation mode activated. I will help you with structure, cues, and delivery support.")
            self.add_output("AGNI", "Presentation mode is active. Try 'show agenda', 'presentation script', or 'summary'.")
        else:
            self.speak("Presentation mode deactivated. Returning to general assistant mode.")
            self.add_output("AGNI", "General assistant mode is active.")
        self.ui(self._update_presentation_metric)

    def _update_presentation_metric(self):
        if self.system_metric_2 is not None:
            label = "PRESENTATION ACTIVE" if self.presentation_mode else "PRESENTATION READY"
            color = "#7AF4FF" if self.presentation_mode else "#B3F7FF"
            self.system_metric_2.configure(text=label, text_color=color)

    def show_help(self):
        help_text = (
            "Available actions: open YouTube, open Google, open Gmail, open classroom, open documents, open desktop, "
            "open notepad, open calculator, open settings, search the web, play music, tell time, tell date, weather, "
            "add note, show notes, add task, show tasks, complete task, save contact, call contact, send message, "
            "remember, ask AGNI, dashboard summary, presentation mode, list apps, open any discovered app, "
            "focus a window, type text, press a key, click a button, inspect the screen, run multi-step plans, "
            "set my name, set city, set goal, and smart help."
        )
        self.speak(help_text)
        self.add_output("AGNI", help_text)

    def open_documents_folder(self):
        try:
            docs_path = str(Path.home() / "Documents")
            os.startfile(docs_path)
            self.speak("Opening your Documents folder.")
        except Exception:
            logger.exception("Failed to open Documents folder")
            self.speak("I could not open your Documents folder.")

    def open_desktop_folder(self):
        try:
            desktop_path = str(Path.home() / "Desktop")
            os.startfile(desktop_path)
            self.speak("Opening your Desktop.")
        except Exception:
            logger.exception("Failed to open Desktop folder")
            self.speak("I could not open your Desktop.")

    def create_presentation_script(self, topic="Your topic"):
        script = [
            f"Good morning everyone. Today I am presenting {topic}.",
            "First, I will explain the problem or idea clearly.",
            "Then I will show the key points, benefits, and practical use.",
            "Next, I will demonstrate a real example or live result.",
            "Finally, I will summarize the main takeaway and answer questions.",
        ]
        self.speak("Here is a strong presentation flow.")
        for line in script:
            self.speak(line)

    def ask_ai(self, question=None):
        if not question:
            self.speak("What do you want to ask?")
            question = self.take_command(timeout=8, phrase_time_limit=15)
        if not question:
            self.speak("I did not catch the question.")
            return

        if self.client is not None:
            try:
                self.chat_history.append({"role": "user", "content": question})
                self.chat_history = self.chat_history[-MAX_CHAT_HISTORY:]
                response = self.client.chat.completions.create(
                    model=OPENAI_MODEL,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "You are AGNI-X, a polished personal assistant for a laptop user. "
                                "Be friendly, smart, concise, and practical. "
                                "Keep answers clear and professional."
                            ),
                        },
                        *self.chat_history,
                    ],
                )
                reply = response.choices[0].message.content or "I could not generate a response."
                self.chat_history.append({"role": "assistant", "content": reply})
                self.chat_history = self.chat_history[-MAX_CHAT_HISTORY:]
                self.speak(reply)
                return
            except Exception:
                logger.exception("OpenAI request failed")

        local_reply = self.generate_local_reply(question)
        self.speak(local_reply)

    # ================= COMMAND DISPATCH =================

    def dispatch_command(self, command, source="voice"):
        if not command:
            return
        command = normalize_text(command)
        if not command:
            return
        logger.info("Dispatching %s command: %s", source, command)
        with self.command_lock:
            try:
                self.handle_command(command)
            except Exception:
                logger.exception("Command execution failed: %s", command)
                self.speak("I heard the command, but I could not complete it.")

    def handle_command(self, command, confirmed=False):
        if not command:
            return

        logger.info("Handling command: %s", command)

        if self.pending_command is not None:
            if command in ("confirm", "yes", "do it", "continue"):
                pending = self.pending_command
                self.pending_command = None
                self.handle_command(pending, confirmed=True)
            elif command in ("cancel", "no", "stop that"):
                self.pending_command = None
                self.speak("Cancelled.")
            else:
                self.speak("Please say confirm or cancel.")
            return

        if not confirmed and command.startswith(("send message", "make call", "call ", "delete ", "shutdown", "turn off")):
            self.request_confirmation(command)
            return

        if command.startswith("do ") or command.startswith("plan "):
            plan = re.sub(r"^(?:do|plan)\s+", "", command, count=1)
            self.run_task_plan(plan)
            return

        if command in ("what is on screen", "what's on screen", "inspect screen", "take screenshot", "screenshot"):
            self.describe_screen()
            return

        if command in ("shutdown", "stop agni", "stop", "turn off"):
            self.speak("Shutting down AGNI-X.")
            self.request_stop()
            return

        greetings = ("hello", "hi", "hey", "good morning", "good evening")
        if any(command == greeting or command.startswith(f"{greeting} ") for greeting in greetings):
            self.speak(f"Hello! I am {ASSISTANT_NAME}. I am ready to help with your work and presentation needs.")
            return

        if any(phrase in command for phrase in ("what can you do", "help me", "show me what you can do", "help")):
            self.show_help()
            return

        if any(phrase in command for phrase in ("summary", "dashboard summary", "status report", "my summary")):
            self.show_dashboard_summary()
            return

        if any(phrase in command for phrase in ("presentation mode", "presentation", "teach mode", "demo mode")):
            self.toggle_presentation_mode()
            return

        if command in ("list apps", "show apps", "what apps are installed"):
            self.list_installed_apps(); return
        if command.startswith("open "):
            requested = command[5:].strip()
            known_commands = {"youtube", "google", "gmail", "email", "classroom", "portal", "documents", "desktop", "notepad", "calculator", "settings", "command prompt", "cmd", "terminal", "powershell", "explorer", "file explorer"}
            if requested not in known_commands:
                self.control_windows("open", requested); return
        if command.startswith("focus "):
            self.control_windows("focus", command[6:].strip()); return
        if command.startswith("type "):
            self.control_windows("type", command[5:].strip()); return
        if command.startswith("press "):
            self.control_windows("press", command[6:].strip()); return
        if command.startswith("click "):
            self.control_windows("click", command[6:].strip()); return

        if "open youtube" in command:
            self.open_youtube(); return
        if "open google" in command:
            self.open_google(); return
        if "open gmail" in command or "open email" in command:
            self.open_email(); return
        if "open classroom" in command or "open portal" in command:
            self.open_portal(); return
        if "open documents" in command or "documents folder" in command:
            self.open_documents_folder(); return
        if "open desktop" in command:
            self.open_desktop_folder(); return
        if "open notepad" in command:
            self.open_local_app("notepad"); return
        if "open calculator" in command:
            self.open_local_app("calculator"); return
        if "open settings" in command:
            self.open_local_app("settings"); return
        if "open command prompt" in command or "open cmd" in command:
            self.open_local_app("command"); return
        if "open terminal" in command or "open powershell" in command:
            self.open_local_app("terminal"); return
        if "open explorer" in command or "open file explorer" in command:
            self.open_local_app("explorer"); return

        if any(phrase in command for phrase in ("time", "what time", "current time")):
            self.tell_time(); return
        if any(phrase in command for phrase in ("date", "today's date", "what date", "day is it")):
            self.tell_date(); return
        if command.startswith("weather "):
            self.get_weather(command[8:].strip()); return
        if command == "weather":
            self.get_weather(); return

        if command.startswith("search "):
            self.search_web(command[7:].strip()); return
        if command.startswith("google "):
            self.search_web(command[7:].strip()); return
        if command in ("search", "google"):
            self.search_web(); return
        if command.startswith("play "):
            self.smart_play(command[5:].strip()); return
        if command == "play":
            self.smart_play(); return

        if "add note" in command or "save note" in command:
            self.add_note(); return
        if "show notes" in command or "list notes" in command:
            self.list_notes(); return
        if "add task" in command or "new task" in command:
            self.add_task(); return
        if "show tasks" in command or "list tasks" in command:
            self.list_tasks(); return
        if "complete task" in command or "mark task" in command:
            self.complete_task(); return

        if "save contact" in command or "add contact" in command:
            self.add_contact(); return
        if "add nickname" in command or "save nickname" in command:
            self.add_nickname(); return
        if "make call" in command or command.startswith("call "):
            self.call_contact(); return
        if "send message" in command or "message" in command:
            self.send_message(); return

        if command.startswith("remember"):
            self.speak("What should I remember?")
            data = self.take_command()
            if data:
                if self.remember("note", data):
                    self.speak("Saved.")
                else:
                    self.speak("I could not save it.")
            return
        if "what do you remember" in command or "recall memory" in command:
            note = self.recall("note")
            self.speak(note if note else "Nothing is stored.")
            return

        if "agenda" in command or "presentation script" in command or "show agenda" in command:
            self.create_presentation_script(topic="your project or topic")
            return

        if "my name" in command or "user profile" in command:
            self.speak(f"Your name is {self.user_name}. Your focus is {self.profile.get('goal', 'productivity and smart assistance')}.")
            return

        if command.startswith("set my name"):
            name = command.replace("set my name", "").strip()
            if name:
                self.set_user_profile(name=name)
                self.speak(f"Done. I will call you {name}.")
            return

        if command.startswith("set goal"):
            goal = command.replace("set goal", "").strip()
            if goal:
                self.set_user_profile(goal=goal)
                self.speak(f"Your goal has been updated to {goal}.")
            return

        if command.startswith("set city"):
            self.set_city(command.replace("set city", "", 1).strip())
            return

        if "focus mode" in command:
            self.speak("Focus mode activated. I will reduce distractions and help you stay on track.")
            return

        if "study mode" in command:
            self.speak("Study mode activated. I can help you plan, summarize, and stay productive.")
            return

        if command.startswith("ask"):
            self.ask_ai(command[4:]); return

        if "who are you" in command or "your name" in command:
            self.speak(f"I am {ASSISTANT_NAME}, your personal assistant for this computer.")
            return

        if "train voice" in command:
            self.train_voice(); return

        self.ask_ai(command)

    def submit_typed_command(self, event=None):
        if self.command_entry is None:
            return "break"
        command = self.command_entry.get().strip()
        if not command:
            return "break"
        self.command_entry.delete(0, "end")
        logger.info("Typed command: %s", command)
        self.add_output("YOU", command)
        threading.Thread(
            target=self.dispatch_command,
            args=(command, "typed"),
            name="Typed-Command",
            daemon=True,
        ).start()
        return "break"

    # ================= VOICE LOOP =================

    def start_agni(self):
        with self.running_lock:
            if self.is_running:
                logger.warning("Start ignored: AGNI is already running")
                return
            self.is_running = True
            self.stop_event.clear()
            self.listener_thread = threading.Thread(
                target=self._listener_loop,
                name="Agni-Listener",
                daemon=True,
            )
            self.listener_thread.start()
        self.set_buttons(True)
        self.set_state("idle")
        self.speak("AGNI-X online. How can I help you?")
        logger.info("AGNI started")

    def request_stop(self):
        with self.running_lock:
            if not self.is_running:
                return
            self.is_running = False
            self.stop_event.set()
        self.set_state("stopped")
        self.set_buttons(False)
        logger.info("Stop requested")

    def _listener_loop(self):
        try:
            while not self.stop_event.is_set():
                command = self.take_command(timeout=6, phrase_time_limit=8)
                if self.stop_event.is_set():
                    break
                if not command:
                    continue
                if any(word in command for word in WAKE_WORDS):
                    self.speak("Yes?")
                    self.tts_done.wait(timeout=4.0)
                    if self.stop_event.is_set():
                        break
                    actual_command = self.take_command(timeout=8, phrase_time_limit=14)
                    if actual_command and not self.stop_event.is_set():
                        threading.Thread(
                            target=self.dispatch_command,
                            args=(actual_command, "voice"),
                            name="Voice-Command",
                            daemon=True,
                        ).start()
        except Exception:
            logger.exception("Listener loop crashed")
            self.speak("The listener encountered an error.")
        finally:
            with self.running_lock:
                self.is_running = False
            self.stop_event.set()
            self.set_state("stopped")
            self.set_buttons(False)
            logger.info("Listener loop ended")

    # ================= TRAIN VOICE =================

    def train_voice(self):
        self.speak("Say your training phrase.")
        phrase = self.take_command()
        if not phrase:
            self.speak("I could not hear the phrase.")
            return
        try:
            recognizer = sr.Recognizer()
            with sr.Microphone() as source:
                audio = recognizer.listen(source, timeout=5, phrase_time_limit=5)
            with open(VOICE_FILE, "wb") as file:
                file.write(audio.get_wav_data())
            self.speak("Voice sample saved successfully.")
        except Exception:
            logger.exception("Voice training failed")
            self.speak("Voice training failed.")

    # ================= ANIMATION =================

    def animate_arc_reactor(self):
        if self.app is None or not self.app.winfo_exists():
            return
        if self.arc_canvas is not None:
            c = self.arc_canvas
            c.delete("all")
            w = max(c.winfo_width(), 760)
            h = max(c.winfo_height(), 620)
            cx, cy = w // 2, h // 2
            with self.state_lock:
                state = self.agni_state

            speed = {"stopped": 0.4, "idle": 1.2, "listening": 4.5, "processing": 7.5, "speaking": 3.2}.get(state, 1.2)
            self.arc_angle = (self.arc_angle + speed) % 360
            self.core_phase += {"stopped": 0.02, "idle": 0.06, "listening": 0.16, "processing": 0.22, "speaking": 0.18}.get(state, 0.06)
            self.scan_phase = (self.scan_phase + speed * 2) % 360

            palette = {
                "stopped": ("#25323A", "#34434D", "#607080"),
                "idle": ("#00D9FF", "#1B8CFF", "#7AF7FF"),
                "listening": ("#00FFD1", "#00B8FF", "#B8FFF1"),
                "processing": ("#FFD166", "#FF8A3D", "#FFF0B3"),
                "speaking": ("#A78BFA", "#00D9FF", "#E4D7FF"),
            }
            a, b, glow = palette.get(state, palette["idle"])
            pulse = 10 * (0.5 + 0.5 * math.sin(self.core_phase))
            if state == "stopped":
                pulse = 0

            outer = int(min(w, h) * 0.46)
            ring2 = int(outer * 0.86)
            ring3 = int(outer * 0.70)
            node_r = int(outer * 0.76)
            core_outer = int(outer * 0.48)
            core_mid = int(outer * 0.31)

            c.create_line(18, cy, w - 18, cy, fill="#10242C", width=1)
            c.create_line(cx, 18, cx, h - 18, fill="#10242C", width=1)
            for x, y, sx, sy in [(22, 22, 1, 1), (w - 22, 22, -1, 1), (22, h - 22, 1, -1), (w - 22, h - 22, -1, -1)]:
                c.create_line(x, y, x + sx * 34, y, fill=a, width=2)
                c.create_line(x, y, x, y + sy * 34, fill=a, width=2)

            for radius, width, start, extent, count in [
                (outer, 3, self.arc_angle, 38, 6),
                (ring2, 3, -self.arc_angle * 0.72 + 50, 54, 5),
                (ring3, 4, self.arc_angle * 1.25 + 135, 72, 4),
            ]:
                step = 360 / count
                for k in range(count):
                    ang = start + k * step
                    c.create_arc(cx - radius, cy - radius, cx + radius, cy + radius, start=ang, extent=extent, style="arc", outline=a, width=width)

            for k in range(8):
                ang = math.radians(self.arc_angle * 0.28 + k * 45)
                x1 = cx + int(outer * 0.36) * math.cos(ang)
                y1 = cy + int(outer * 0.36) * math.sin(ang)
                x2 = cx + int(outer * 0.62) * math.cos(ang)
                y2 = cy + int(outer * 0.62) * math.sin(ang)
                c.create_line(x1, y1, x2, y2, fill="#174B5A", width=12)
                c.create_line(x1, y1, x2, y2, fill=b, width=2)

            if state == "processing":
                for off in range(6):
                    ang = math.radians(self.scan_phase - off * 3)
                    x = cx + outer * math.cos(ang)
                    y = cy - outer * math.sin(ang)
                    c.create_line(cx, cy, x, y, fill=b, width=max(1, 6 - off))

            for k in range(12):
                ang = math.radians(self.arc_angle * 1.65 + k * 30)
                x = cx + node_r * math.cos(ang)
                y = cy - node_r * math.sin(ang)
                size = 4 if k % 2 else 6
                c.create_oval(x - size, y - size, x + size, y + size, fill=glow, outline="")

            for r, col in [(core_outer, "#071C24"), (int(core_outer * 0.82), "#0A2C38"), (int(core_outer * 0.64), "#0D4553")]:
                c.create_oval(cx - r, cy - r, cx + r, cy + r, fill=col, outline=a, width=2)
            core_r = core_mid + pulse
            c.create_oval(cx - core_r, cy - core_r, cx + core_r, cy + core_r, fill="#08798D", outline=glow, width=4)
            mid = int(core_mid * 0.62 + pulse / 2)
            c.create_oval(cx - mid, cy - mid, cx + mid, cy + mid, fill="#0CCFE8", outline="")
            inner = int(core_mid * 0.30)
            c.create_oval(cx - inner, cy - inner, cx + inner, cy + inner, fill="#D9FFFF", outline="")

            for k in range(6):
                ang = math.radians(-self.arc_angle * 0.52 + k * 60)
                x1 = cx + int(outer * 0.30) * math.cos(ang)
                y1 = cy + int(outer * 0.30) * math.sin(ang)
                x2 = cx + int(outer * 0.55) * math.cos(ang)
                y2 = cy + int(outer * 0.55) * math.sin(ang)
                c.create_line(x1, y1, x2, y2, fill=b, width=5)
                c.create_oval(x2 - 7, y2 - 7, x2 + 7, y2 + 7, fill=a, outline="")

            c.create_text(cx, h - 34, text="AGNI ENERGY CORE", fill=a, font=("Consolas", 12, "bold"))
            c.create_text(cx, h - 16, text=f"MODE // {state.upper()}", fill="#6D8792", font=("Consolas", 9))

        self.app.after(33, self.animate_arc_reactor)

    # ================= GUI =================

    def show_startup_screen(self):
        splash = ctk.CTkToplevel(self.app)
        splash.title("AGNI-X")
        splash.geometry("700x420")
        splash.configure(fg_color="#061119")
        splash.overrideredirect(True)
        splash.attributes("-topmost", True)
        screen_w = splash.winfo_screenwidth()
        screen_h = splash.winfo_screenheight()
        x = (screen_w // 2) - 350
        y = (screen_h // 2) - 210
        splash.geometry(f"700x420+{x}+{y}")

        main_frame = ctk.CTkFrame(splash, fg_color="transparent")
        main_frame.pack(expand=True)

        ctk.CTkLabel(main_frame, text="AGNI-X", font=("Arial", 40, "bold"), text_color="#63F5FF").pack(pady=(18, 0))
        ctk.CTkLabel(main_frame, text=PRESENTATION_TAGLINE, font=("Consolas", 16), text_color="#A7B6C4").pack(pady=(8, 20))
        ctk.CTkLabel(main_frame, text="SMART DESKTOP AI ASSISTANT", font=("Consolas", 12, "bold"), text_color="#4FD7FF").pack()

        progress = ctk.CTkProgressBar(main_frame, width=450, height=18, mode="determinate")
        progress.set(0)
        progress.pack(pady=22)

        info = ctk.CTkLabel(main_frame, text="Loading neural systems...", font=("Consolas", 12), text_color="#D2E7F0")
        info.pack(pady=10)

        def update_progress(value, text):
            progress.set(value)
            info.configure(text=text)
            splash.update_idletasks()

        update_progress(0.2, "Initializing cognitive core...")
        self.app.after(300, lambda: update_progress(0.55, "Importing assistant intelligence..."))
        self.app.after(700, lambda: update_progress(0.9, "Preparing presentation-ready interface..."))
        self.app.after(1100, lambda: splash.destroy())

    def build_gui(self):
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        self.app = ctk.CTk()
        self.app.title(APP_TITLE)
        self.app.configure(fg_color="#071017")
        if ICON_FILE.exists():
            try:
                self.app.iconbitmap(str(ICON_FILE))
            except tk.TclError:
                logger.exception("Could not apply AGNI-X window icon")
        try:
            self.app.state("zoomed")
        except tk.TclError:
            self.app.geometry("1280x900")
        self.app.minsize(1050, 760)
        self.app.protocol("WM_DELETE_WINDOW", self.on_close)

        self.show_startup_screen()

        topbar = ctk.CTkFrame(self.app, fg_color="#091820", corner_radius=0, height=118)
        topbar.pack(fill="x")
        topbar.pack_propagate(False)
        topbar.grid_columnconfigure(0, weight=3)
        topbar.grid_columnconfigure(1, weight=2)
        topbar.grid_columnconfigure(2, weight=2)

        brand = ctk.CTkFrame(topbar, fg_color="transparent")
        brand.grid(row=0, column=0, sticky="w", padx=34, pady=18)
        ctk.CTkLabel(brand, text="AGNI-X", font=("Arial", 34, "bold"), text_color="#32E6FF").pack(anchor="w")
        ctk.CTkLabel(brand, text="PERSONAL AI ASSISTANT", font=("Consolas", 11), text_color="#6D8792").pack(anchor="w", pady=(2, 0))
        self.welcome_label = ctk.CTkLabel(brand, text=f"Welcome {self.user_name} | Presentation-Ready", font=("Consolas", 10), text_color="#7AF4FF")
        self.welcome_label.pack(anchor="w", pady=(6, 0))

        clock_panel = ctk.CTkFrame(topbar, fg_color="#07141B", corner_radius=12, border_width=1, border_color="#16313C")
        clock_panel.grid(row=0, column=1, sticky="nsew", padx=10, pady=14)
        self.clock_label = ctk.CTkLabel(clock_panel, text="00:00:00", font=("Consolas", 30, "bold"), text_color="#8CEEFF")
        self.clock_label.pack(pady=(10, 0))
        self.date_label = ctk.CTkLabel(clock_panel, text="Loading date...", font=("Consolas", 10), text_color="#6D8792")
        self.date_label.pack(pady=(0, 8))

        weather_panel = ctk.CTkFrame(topbar, fg_color="#07141B", corner_radius=12, border_width=1, border_color="#16313C")
        weather_panel.grid(row=0, column=2, sticky="nsew", padx=(10, 34), pady=14)
        self.weather_city_label = ctk.CTkLabel(weather_panel, text=self.city.upper(), font=("Consolas", 11, "bold"), text_color="#32E6FF")
        self.weather_city_label.pack(pady=(9, 0))
        self.weather_temp_label = ctk.CTkLabel(weather_panel, text="--°C", font=("Arial", 24, "bold"), text_color="#D7F7FF")
        self.weather_temp_label.pack()
        self.weather_desc_label = ctk.CTkLabel(weather_panel, text="WEATHER LOADING...", font=("Consolas", 9), text_color="#6D8792")
        self.weather_desc_label.pack(pady=(0, 7))

        main = ctk.CTkFrame(self.app, fg_color="transparent")
        main.pack(fill="both", expand=True, padx=28, pady=18)
        main.grid_columnconfigure(0, weight=2)
        main.grid_columnconfigure(1, weight=5)
        main.grid_rowconfigure(0, weight=1)

        left = ctk.CTkFrame(main, fg_color="#0A171E", corner_radius=18, border_width=1, border_color="#16313C")
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        ctk.CTkLabel(left, text="COMMAND STREAM", font=("Consolas", 14, "bold"), text_color="#32E6FF").pack(anchor="w", padx=18, pady=(16, 6))
        self.status_text = ctk.CTkLabel(left, text="SYSTEM OFFLINE", font=("Arial", 18, "bold"), text_color="#607080")
        self.status_text.pack(anchor="w", padx=18)
        self.heard_text = ctk.CTkLabel(left, text="HEARD  ›  Waiting for voice input...", font=("Consolas", 11), text_color="#7C96A1", wraplength=420, justify="left")
        self.heard_text.pack(anchor="w", padx=18, pady=(8, 10))
        self.output_box = ctk.CTkTextbox(left, font=("Consolas", 13), fg_color="#061016", border_width=1, border_color="#17313C", text_color="#CDE7EE", corner_radius=12)
        self.output_box.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        self.output_box.insert("end", "[ AGNI-X READY ]\nYour smart laptop assistant is online.\n\n")

        metrics = ctk.CTkFrame(left, fg_color="#0B1E27", corner_radius=12)
        metrics.pack(fill="x", padx=16, pady=(0, 16))
        ai_status = "AI ONLINE" if self.client is not None else "AI LOCAL MODE"
        self.system_metric_1 = ctk.CTkLabel(metrics, text=ai_status, font=("Consolas", 11, "bold"), text_color="#79F9FF")
        self.system_metric_1.grid(row=0, column=0, padx=18, pady=12)
        self.system_metric_2 = ctk.CTkLabel(metrics, text="PRESENTATION MODE", font=("Consolas", 11, "bold"), text_color="#B3F7FF")
        self.system_metric_2.grid(row=0, column=1, padx=18, pady=12)
        control_status = "DESKTOP CONTROL" if pyautogui is not None and Desktop is not None else "BASIC CONTROL"
        self.system_metric_3 = ctk.CTkLabel(metrics, text=control_status, font=("Consolas", 11, "bold"), text_color="#A7F7AE")
        self.system_metric_3.grid(row=0, column=2, padx=18, pady=12)

        right = ctk.CTkFrame(main, fg_color="#071017", corner_radius=18, border_width=1, border_color="#16313C")
        right.grid(row=0, column=1, sticky="nsew")
        self.arc_canvas = tk.Canvas(right, bg="#050B0F", highlightthickness=0, bd=0)
        self.arc_canvas.pack(fill="both", expand=True, padx=6, pady=6)

        deck = ctk.CTkFrame(self.app, fg_color="#091820", corner_radius=0, height=112)
        deck.pack(fill="x", side="bottom")
        deck.pack_propagate(False)
        command_frame = ctk.CTkFrame(deck, fg_color="transparent")
        command_frame.pack(pady=(15, 6))
        self.command_entry = ctk.CTkEntry(
            command_frame,
            width=700,
            height=48,
            placeholder_text="Type a command or ask AGNI anything...",
            font=("Arial", 15),
            fg_color="#0D1D25",
            border_color="#2A6575",
            border_width=2,
        )
        self.command_entry.grid(row=0, column=0, padx=(0, 10))
        self.command_entry.bind("<Return>", self.submit_typed_command)
        self.send_button = ctk.CTkButton(
            command_frame,
            text="SEND  ›",
            width=125,
            height=48,
            font=("Arial", 14, "bold"),
            command=self.submit_typed_command,
            fg_color="#176A91",
            hover_color="#2085B2",
        )
        self.send_button.grid(row=0, column=1, padx=(0, 14))
        self.activate_button = ctk.CTkButton(
            command_frame,
            text="◉  ACTIVATE VOICE",
            width=180,
            height=48,
            font=("Arial", 13, "bold"),
            command=self.start_agni,
            fg_color="#0D6B62",
            hover_color="#108A7D",
        )
        self.activate_button.grid(row=0, column=2, padx=4)
        self.stop_button = ctk.CTkButton(
            command_frame,
            text="■  STOP",
            width=105,
            height=48,
            font=("Arial", 13, "bold"),
            command=self.request_stop,
            state="disabled",
            fg_color="#542A36",
            hover_color="#743747",
        )
        self.stop_button.grid(row=0, column=3, padx=(10, 0))

        quick_row = ctk.CTkFrame(deck, fg_color="transparent")
        quick_row.pack(pady=(0, 10))
        for label, command in [
            ("Open YouTube", "open youtube"),
            ("Search", "search"),
            ("Summary", "summary"),
            ("Presentation", "presentation"),
            ("Help", "help"),
        ]:
            btn = ctk.CTkButton(
                quick_row,
                text=label,
                width=110,
                height=30,
                command=lambda cmd=command: self.dispatch_command(cmd, "quick"),
                fg_color="#153B4D",
                hover_color="#1C4A5E",
            )
            btn.pack(side="left", padx=6)

        ctk.CTkLabel(deck, text="VOICE: say AGNI • TEXT: no wake word required • ENTER: send", font=("Consolas", 10), text_color="#5E7883").pack()

        self.app.after(1200, lambda: self.speak(f"Welcome {self.user_name}. I am ready to assist you with your projects, tasks, and presentations."))

        self.set_state("stopped")
        self.update_clock()
        self.refresh_weather()
        self.animate_arc_reactor()
        self.command_entry.focus_set()
        self.app.mainloop()

    def update_clock(self):
        if self.app is None or not self.app.winfo_exists():
            return
        now = datetime.datetime.now()
        self.clock_label.configure(text=now.strftime("%H:%M:%S"))
        self.date_label.configure(text=now.strftime("%A  •  %d %B %Y").upper())
        self.app.after(1000, self.update_clock)

    def refresh_weather(self):
        def worker():
            try:
                city = self.city
                response = requests.get(
                    f"https://wttr.in/{quote_plus(city)}",
                    params={"format": "%t|%C"},
                    timeout=10,
                )
                response.raise_for_status()
                temp, condition = [part.strip() for part in response.text.split("|", 1)]
                self.app.after(0, lambda: self.weather_city_label.configure(text=city.upper()))
                self.app.after(0, lambda: self.weather_temp_label.configure(text=temp))
                self.app.after(0, lambda: self.weather_desc_label.configure(text=condition.upper()))
            except Exception:
                logger.exception("Dashboard weather refresh failed")
                self.app.after(0, lambda: self.weather_desc_label.configure(text="WEATHER UNAVAILABLE"))

        threading.Thread(target=worker, name="Weather-Refresh", daemon=True).start()
        if self.app is not None and self.app.winfo_exists():
            self.app.after(900000, self.refresh_weather)

    def on_close(self):
        logger.info("Window close requested")
        self.request_stop()
        try:
            self.tts_queue.put_nowait(None)
        except Exception:
            pass
        if self.app is not None:
            self.app.after(100, self.app.destroy)


# ================= START =================

if __name__ == "__main__":
    try:
        AgniXApp().build_gui()
    except Exception:
        logger.critical("AGNI could not start\n%s", traceback.format_exc())
        raise
