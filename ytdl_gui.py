"""
YouTube Downloader - окно для скачивания видео (обёртка над yt-dlp).

Собирается в exe через build_exe.bat. Для работы exe Python не нужен:
yt-dlp, Deno и FFmpeg приложение само скачивает в %LOCALAPPDATA%\\YTDownloader\\bin,
а yt-dlp раз в сутки обновляет (YouTube регулярно ломает старые версии).

Если YouTube просит подтвердить, что ты не бот, или видео 18+ - положи cookies.txt
(экспорт куки youtube.com из браузера) рядом с exe.
"""

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, ttk

# В exe без консоли sys.stdout = None, и любой print уронил бы программу
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

APP_TITLE = "YouTube Downloader"
IS_WIN = os.name == "nt"
EXE = ".exe" if IS_WIN else ""
NO_WINDOW = 0x08000000 if IS_WIN else 0  # CREATE_NO_WINDOW: не мигать чёрными окнами

FROZEN = getattr(sys, "frozen", False)
APP_HOME = Path(sys.executable).parent if FROZEN else Path(__file__).resolve().parent
RESOURCES = Path(getattr(sys, "_MEIPASS", APP_HOME))
DATA_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "share") / "YTDownloader"
BIN_DIR = DATA_DIR / "bin"
SETTINGS_FILE = DATA_DIR / "settings.json"
UPDATE_STAMP = DATA_DIR / "last_update.txt"
NIGHTLY_FLAG = DATA_DIR / "nightly.txt"
DEBUG_LOG = DATA_DIR / "debug.log"
UPDATE_EVERY_SEC = 24 * 60 * 60

DEFAULT_FOLDER = str(Path.home() / "Desktop" / "youtube")

GITHUB = "https://github.com"
YTDLP_URL = f"{GITHUB}/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe"
DENO_URL = f"{GITHUB}/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip"
FFMPEG_URL = (f"{GITHUB}/yt-dlp/FFmpeg-Builds/releases/download/latest/"
              "ffmpeg-master-latest-win64-gpl.zip")

SINGLE_TEMPLATE = "%(title).150B [%(id)s].%(ext)s"
PLAYLIST_TEMPLATE = "%(playlist_title).100B/%(playlist_index)s - %(title).150B [%(id)s].%(ext)s"

VIDEO_COMMON = ["--merge-output-format", "mp4", "--embed-metadata", "--embed-chapters"]
QUALITIES = [
    ("1080p - MP4, открывается везде",
     ["-f", "bv*+ba/b", "-S", "res:1080,vcodec:h264,acodec:aac", *VIDEO_COMMON]),
    ("Максимальное - до 4K/8K (кодек AV1/VP9, нужен VLC или свежий плеер)",
     ["-f", "bv*+ba/b", "-S", "res,fps,acodec:aac", *VIDEO_COMMON]),
    ("720p - MP4, файл поменьше",
     ["-f", "bv*+ba/b", "-S", "res:720,vcodec:h264,acodec:aac", *VIDEO_COMMON]),
    ("Только звук - MP3 с обложкой",
     ["-f", "ba/b", "-x", "--audio-format", "mp3", "--audio-quality", "0",
      "--embed-metadata", "--embed-thumbnail"]),
]

PROGRESS_TEMPLATE = ("download:PROG %(progress.downloaded_bytes)s %(progress.total_bytes)s "
                     "%(progress.total_bytes_estimate)s %(progress.speed)s %(progress.eta)s")
URL_RE = re.compile(r"https?://\S+")
YOUTUBE_RE = re.compile(r"https?://(?:www\.|m\.|music\.)?(?:youtube\.com|youtu\.be)/\S+")


# ================================================================= утилиты

def load_settings():
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(data):
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def touch(path):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(time.ctime(), encoding="utf-8")
    except OSError:
        pass


def is_older_than(path, seconds):
    try:
        return time.time() - path.stat().st_mtime > seconds
    except OSError:
        return True


def human_size(num):
    if num is None:
        return "?"
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if num < 1024:
            return f"{num:.0f} {unit}" if unit == "Б" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} ТБ"


def human_eta(seconds):
    if seconds is None:
        return ""
    seconds = int(seconds)
    return f"{seconds // 60}:{seconds % 60:02d}" if seconds < 3600 else \
        f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def open_path(path):
    path = str(path)
    if IS_WIN:
        os.startfile(path)  # noqa: S606 - открыть папку/файл в проводнике
    else:
        subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def kill_tree(proc):
    if proc.poll() is not None:
        return
    if IS_WIN:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
    else:
        proc.kill()


def cookies_file():
    for candidate in (APP_HOME / "cookies.txt", DATA_DIR / "cookies.txt"):
        if candidate.is_file():
            return candidate
    return None


def parse_timecode(text):
    """'83', '1:23', '1.23', '01:02:03', '1:23.5' -> секунды; пустая строка -> None."""
    text = text.strip().replace(",", ".").replace(" ", "")
    if not text:
        return None
    if ":" not in text and "." in text:
        text = text.replace(".", ":")  # "1.23" и "1.02.03" часто пишут вместо "1:23"
    parts = text.split(":")
    if len(parts) > 3 or not all(re.fullmatch(r"\d+(\.\d+)?", p) for p in parts):
        raise ValueError(text)
    if any("." in p for p in parts[:-1]) or any(float(p) >= 60 for p in parts[1:]):
        raise ValueError(text)
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60 + float(part)
    return seconds


def format_timecode(seconds):
    whole = int(seconds)
    frac = seconds - whole
    text = (f"{whole // 3600}:{whole % 3600 // 60:02d}:{whole % 60:02d}" if whole >= 3600
            else f"{whole // 60}:{whole % 60:02d}")
    return text + (f"{frac:.2f}"[1:].rstrip("0") if frac else "")


def file_label(seconds):
    """Метка для имени файла: двоеточия в именах файлов Windows запрещены."""
    whole = int(seconds)
    hours, minutes, secs = whole // 3600, whole % 3600 // 60, whole % 60
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    return f"{minutes}m{secs:02d}s" if minutes else f"{secs}s"


def seconds_arg(seconds):
    return f"{seconds:.3f}".rstrip("0").rstrip(".")


def section_text(section):
    start, end = section
    return f"{format_timecode(start)} - {format_timecode(end) if end is not None else 'до конца'}"


def is_playlist_page(url):
    """Ссылка именно на плейлист или канал, а не на одно видео."""
    return bool(re.search(r"youtube\.com/(playlist\?|@|channel/|c/|user/)", url))


def has_playlist_param(url):
    return "list=" in url and ("v=" in url or "youtu.be/" in url)


# ================================================================= диагностика

def classify(line):
    low = line.lower()
    if "sign in to confirm you" in low and "bot" in low:
        return "bot"
    if ("confirm your age" in low or "age-restricted" in low
            or "members-only" in low or "join this channel" in low):
        return "login"
    if "requested format" in low:
        return "other"
    if any(mark in low for mark in (
            "video unavailable", "this video is not available", "private video",
            "has been removed", "live event will begin", "premieres in", "copyright")):
        return "unavailable"
    if any(mark in low for mark in (
            "getaddrinfo", "failed to resolve", "name or service not known",
            "no address associated", "timed out", "connection reset", "connection refused",
            "network is unreachable", "winerror 10060", "winerror 11001", "winerror 10054",
            "unable to connect")):
        return "network"
    if "http error 403" in low:
        return "forbidden"
    return "other"


def explain(kinds):
    tips = []
    if "bot" in kinds:
        tips.append("YouTube просит подтвердить, что ты не бот.")
    if "login" in kinds:
        tips.append("Видео с ограничением по возрасту или только для спонсоров канала.")
    if kinds & {"bot", "login"}:
        tips.append("Нужен cookies.txt: поставь в браузер расширение «Get cookies.txt LOCALLY», "
                    "открой youtube.com залогиненным, экспортируй куки и сохрани файл "
                    f"как cookies.txt рядом с программой: {APP_HOME}")
    if "network" in kinds:
        tips.append("Компьютер не может достучаться до серверов YouTube - это сеть или DNS. "
                    "Помогает смена DNS на 1.1.1.1 / 8.8.8.8 или VPN.")
    if "unavailable" in kinds:
        tips.append("Часть видео недоступна: удалено, приватное или ещё не вышло.")
    if "forbidden" in kinds:
        tips.append("YouTube запретил скачивание (403). Обычно лечится свежим yt-dlp - "
                    "попробуй ещё раз завтра или с VPN.")
    return tips


# ================================================================= инструменты

class SetupError(Exception):
    pass


def http_download(url, dest, on_progress):
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 YTDownloader"})
    with urllib.request.urlopen(request, timeout=60) as response, open(tmp, "wb") as out:
        total = int(response.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = response.read(256 * 1024)
            if not chunk:
                break
            out.write(chunk)
            done += len(chunk)
            on_progress(done, total)
    os.replace(tmp, dest)


def extract_from_zip(zip_path, names, dest_dir):
    found = set()
    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            base = info.filename.rsplit("/", 1)[-1]
            if base in names:
                target = dest_dir / base
                tmp = target.with_name(base + ".part")
                with archive.open(info) as src, open(tmp, "wb") as out:
                    shutil.copyfileobj(src, out)
                os.replace(tmp, target)
                found.add(base)
    missing = set(names) - found
    if missing:
        raise SetupError(f"в архиве нет {', '.join(sorted(missing))}")


def first_existing(*candidates):
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(candidate)
    return None


class Tools:
    """Находит или скачивает yt-dlp, Deno и FFmpeg."""

    def __init__(self):
        self.ytdlp = self.deno = self.ffmpeg = None
        self.managed_ytdlp = False
        self.ready = False

    def prepare(self, status, progress):
        BIN_DIR.mkdir(parents=True, exist_ok=True)
        spotdl_dir = Path.home() / ".spotdl"

        # --- yt-dlp
        managed = BIN_DIR / f"yt-dlp{EXE}"
        self.ytdlp = first_existing(os.environ.get("YTDL_YTDLP"), managed)
        if not self.ytdlp and IS_WIN:
            status("Скачиваю yt-dlp (один раз)...")
            self._download(YTDLP_URL, managed, "yt-dlp", status, progress)
            touch(UPDATE_STAMP)
            self.ytdlp = str(managed)
        if not self.ytdlp:
            self.ytdlp = shutil.which("yt-dlp")
        if not self.ytdlp:
            raise SetupError("не нашёл yt-dlp")
        self.managed_ytdlp = Path(self.ytdlp) == managed
        if self.managed_ytdlp and is_older_than(UPDATE_STAMP, UPDATE_EVERY_SEC):
            status("Проверяю обновления yt-dlp...")
            progress(None)
            if self.update_ytdlp() == 0:
                touch(UPDATE_STAMP)

        # --- Deno: без него yt-dlp не проходит JS-проверку YouTube
        self.deno = first_existing(os.environ.get("YTDL_DENO"), BIN_DIR / f"deno{EXE}",
                                   spotdl_dir / f"deno{EXE}", shutil.which("deno"))
        if not self.deno and IS_WIN:
            archive = BIN_DIR / "deno.zip"
            status("Скачиваю Deno (один раз)...")
            self._download(DENO_URL, archive, "Deno", status, progress)
            extract_from_zip(archive, {"deno.exe"}, BIN_DIR)
            archive.unlink(missing_ok=True)
            self.deno = str(BIN_DIR / "deno.exe")

        # --- FFmpeg: склеивает видео со звуком и делает MP3
        self.ffmpeg = first_existing(os.environ.get("YTDL_FFMPEG"), BIN_DIR / f"ffmpeg{EXE}",
                                     shutil.which("ffmpeg"), spotdl_dir / f"ffmpeg{EXE}")
        if not self.ffmpeg and IS_WIN:
            archive = BIN_DIR / "ffmpeg.zip"
            status("Скачиваю FFmpeg (один раз, ~200 МБ)...")
            self._download(FFMPEG_URL, archive, "FFmpeg", status, progress)
            status("Распаковываю FFmpeg...")
            progress(None)
            extract_from_zip(archive, {"ffmpeg.exe", "ffprobe.exe"}, BIN_DIR)
            archive.unlink(missing_ok=True)
            self.ffmpeg = str(BIN_DIR / "ffmpeg.exe")

        self.ready = True

    @staticmethod
    def _download(url, dest, title, status, progress):
        def on_progress(done, total):
            if total:
                progress(done / total)
                status(f"Скачиваю {title}: {human_size(done)} из {human_size(total)}")
            else:
                progress(None)
                status(f"Скачиваю {title}: {human_size(done)}")
        try:
            http_download(url, dest, on_progress)
        except Exception as exc:
            raise SetupError(f"не удалось скачать {title}: {exc}") from exc

    def update_ytdlp(self, nightly=False):
        cmd = [self.ytdlp, "--update-to", "nightly"] if nightly else [self.ytdlp, "-U"]
        try:
            return subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, timeout=300,
                                  creationflags=NO_WINDOW).returncode
        except (OSError, subprocess.TimeoutExpired):
            return 1


# ================================================================= скачивание

class Job:
    """Один запуск скачивания. Работает в фоновом потоке, всё сообщает через emit()."""

    def __init__(self, tools, urls, folder, quality, whole_playlist, emit,
                 section=None, exact_cut=True):
        self.tools = tools
        self.urls = urls
        self.folder = folder
        self.quality = quality
        self.whole_playlist = whole_playlist
        self.emit = emit
        self.section = section  # (начало, конец или None) в секундах, None - видео целиком
        self.exact_cut = exact_cut
        self.proc = None
        self.cancelled = False
        self.files = []
        self.errors = []
        self.log_lines = []
        self.started = time.time()

    def build_cmd(self, url):
        playlist_mode = is_playlist_page(url) or (self.whole_playlist and has_playlist_param(url))
        template = PLAYLIST_TEMPLATE if playlist_mode else SINGLE_TEMPLATE
        extra = []
        if self.section:
            start, end = self.section
            # фрагмент получает своё имя, чтобы не путаться с целым видео
            suffix = f" {file_label(start)}-{file_label(end) if end is not None else 'конец'}"
            template = template[:-len(".%(ext)s")] + suffix + ".%(ext)s"
            end_arg = seconds_arg(end) if end is not None else "inf"
            extra = ["--download-sections", f"*{seconds_arg(start)}-{end_arg}"]
            if self.exact_cut:
                extra.append("--force-keyframes-at-cuts")
        cmd = [
            self.tools.ytdlp, *QUALITIES[self.quality][1], *extra,
            "-P", self.folder,
            "-o", template,
            "--yes-playlist" if playlist_mode else "--no-playlist",
            "-N", "4", "--no-mtime", "--encoding", "utf-8",
            "--quiet", "--progress", "--newline", "--progress-template", PROGRESS_TEMPLATE,
            "--print", "before_dl:ITEM %(playlist_index)s/%(n_entries)s %(title)s",
            "--print", "after_move:FILE %(filepath)s",
        ]
        if self.tools.ffmpeg:
            cmd += ["--ffmpeg-location", self.tools.ffmpeg]
        if self.tools.deno:
            cmd += ["--js-runtimes", f"deno:{self.tools.deno}"]
        cookies = cookies_file()
        if cookies:
            cmd += ["--cookies", str(cookies)]
        return cmd + ["--", url]

    def run_once(self, url):
        cmd = self.build_cmd(url)
        self.log_lines.append("> " + " ".join(cmd))
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, env=env, creationflags=NO_WINDOW)
        errors = []
        for raw in self.proc.stdout:
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if not line:
                continue
            if line.startswith("PROG "):
                self.on_progress(line.split())
                continue
            self.log_lines.append(line)
            if line.startswith("ITEM "):
                self.on_item(line[5:])
            elif line.startswith("FILE "):
                path = line[5:].strip()
                if path and path not in self.files:
                    self.files.append(path)
            elif line.startswith("ERROR:"):
                errors.append(line)
                self.emit("log", line, "error")
            elif line.startswith("WARNING:"):
                self.emit("log", line, "warn")
            else:
                self.emit("log", line, "")
        self.proc.wait()
        return self.proc.returncode, errors

    def on_item(self, rest):
        counter, _, title = rest.partition(" ")
        index, _, total = counter.partition("/")
        action = f"Вырезаю фрагмент {section_text(self.section)}" if self.section else "Скачиваю"
        if index.isdigit() and total.isdigit():
            self.emit("status", f"{action} ({index} из {total}): {title}")
        else:
            self.emit("status", f"{action}: {title}")
        # при вырезании фрагмента yt-dlp сообщает прогресс только в самом конце
        self.emit("progress", None if self.section else 0.0)
        self.emit("detail", "Это может занять пару минут" if self.section else "")

    def on_progress(self, parts):
        def num(value):
            try:
                return float(value)
            except (TypeError, ValueError):
                return None

        values = (parts + ["NA"] * 6)[1:6]
        done, total, estimate, speed, eta = (num(v) for v in values)
        total = total or estimate
        if done is None:
            return
        fraction = done / total if total else None
        details = f"{human_size(done)} из {human_size(total)}" if total else human_size(done)
        if speed:
            details += f"  •  {human_size(speed)}/с"
        if eta:
            details += f"  •  осталось {human_eta(eta)}"
        self.emit("progress", fraction)
        self.emit("detail", details)

    def run(self):
        all_errors = []
        nightly_tried = False
        for url in self.urls:
            if self.cancelled:
                break
            rc, errors = self.run_once(url)
            if self.cancelled:
                break
            kinds = {classify(e) for e in errors}
            if (rc != 0 and kinds & {"forbidden", "other"} and not nightly_tried
                    and self.tools.managed_ytdlp):
                nightly_tried = True
                self.emit("status", "Не вышло. Обновляю yt-dlp до свежей тестовой сборки...")
                self.emit("progress", None)
                if self.tools.update_ytdlp(nightly=True) == 0:
                    touch(NIGHTLY_FLAG)
                    touch(UPDATE_STAMP)
                    self.emit("log", "yt-dlp обновлён, пробую ещё раз...", "")
                    rc, errors = self.run_once(url)  # готовые файлы yt-dlp пропустит
            if rc != 0 and not errors and not self.cancelled:
                tail = [line for line in self.log_lines[-3:] if not line.startswith("> ")]
                errors = [f"ERROR: yt-dlp завершился с кодом {rc}"] + tail
            all_errors += errors
        self.errors = all_errors
        self.write_debug_log()

    def cancel(self):
        self.cancelled = True
        if self.proc:
            kill_tree(self.proc)

    def write_debug_log(self):
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            DEBUG_LOG.write_text(
                f"{time.ctime()}\nyt-dlp: {self.tools.ytdlp}\nDeno: {self.tools.deno}\n"
                f"FFmpeg: {self.tools.ffmpeg}\nПапка: {self.folder}\n\n"
                + "\n".join(self.log_lines), encoding="utf-8")
        except OSError:
            pass


# ================================================================= окно

class App:
    def __init__(self, root):
        self.root = root
        self.queue = queue.Queue()
        self.tools = Tools()
        self.job = None
        self.setup_running = False
        settings = load_settings()

        root.title(APP_TITLE)
        root.minsize(660, 660)
        icon = RESOURCES / "icon.ico"
        if IS_WIN and icon.is_file():
            try:
                root.iconbitmap(default=str(icon))
            except tk.TclError:
                pass
        style = ttk.Style(root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Big.TButton", padding=(18, 6))

        self.url_var = tk.StringVar()
        self.folder_var = tk.StringVar(value=settings.get("folder") or DEFAULT_FOLDER)
        self.quality_var = tk.IntVar(value=min(int(settings.get("quality", 0)), len(QUALITIES) - 1))
        self.playlist_var = tk.BooleanVar(value=bool(settings.get("whole_playlist", False)))
        self.start_var = tk.StringVar()
        self.end_var = tk.StringVar()
        self.exact_var = tk.BooleanVar(value=bool(settings.get("exact_cut", True)))
        self.status_var = tk.StringVar(value="Подготовка...")
        self.detail_var = tk.StringVar(value="")

        main = ttk.Frame(root, padding=14)
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)

        ttk.Label(main, text="Ссылка на видео, плейлист или канал:").grid(
            row=0, column=0, columnspan=2, sticky="w")
        self.url_entry = ttk.Entry(main, textvariable=self.url_var)
        self.url_entry.grid(row=1, column=0, sticky="ew", pady=(4, 10), ipady=3)
        ttk.Button(main, text="Вставить", command=self.paste).grid(
            row=1, column=1, padx=(8, 0), pady=(4, 10))

        quality_box = ttk.LabelFrame(main, text="Качество", padding=(10, 6))
        quality_box.grid(row=2, column=0, columnspan=2, sticky="ew")
        for index, (title, _) in enumerate(QUALITIES):
            ttk.Radiobutton(quality_box, text=title, value=index,
                            variable=self.quality_var).pack(anchor="w", pady=1)

        fragment = ttk.LabelFrame(main, text="Фрагмент (необязательно)", padding=(10, 6))
        fragment.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        ttk.Label(fragment, text="Начало:").grid(row=0, column=0, sticky="w")
        ttk.Entry(fragment, textvariable=self.start_var, width=10).grid(
            row=0, column=1, padx=(4, 14), ipady=2)
        ttk.Label(fragment, text="Конец:").grid(row=0, column=2, sticky="w")
        ttk.Entry(fragment, textvariable=self.end_var, width=10).grid(
            row=0, column=3, padx=(4, 14), ipady=2)
        ttk.Button(fragment, text="Сбросить", command=self.clear_fragment).grid(
            row=0, column=4, sticky="w")
        fragment.columnconfigure(4, weight=1)
        ttk.Label(fragment, foreground="#555",
                  text="Например 1:23 или 1:02:03. Пусто - скачать целиком, "
                       "пустой конец - до конца видео.").grid(
            row=1, column=0, columnspan=5, sticky="w", pady=(4, 0))
        ttk.Checkbutton(fragment, text="Точно по кадрам (медленнее; без галочки начало "
                                       "может уехать на пару секунд)",
                        variable=self.exact_var).grid(
            row=2, column=0, columnspan=5, sticky="w", pady=(4, 0))

        ttk.Label(main, text="Папка для сохранения:").grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(10, 0))
        ttk.Entry(main, textvariable=self.folder_var).grid(
            row=5, column=0, sticky="ew", pady=(4, 6), ipady=3)
        ttk.Button(main, text="Обзор...", command=self.browse).grid(
            row=5, column=1, padx=(8, 0), pady=(4, 6))

        ttk.Checkbutton(main, text="Если ссылка на видео из плейлиста - скачать весь плейлист",
                        variable=self.playlist_var).grid(row=6, column=0, columnspan=2, sticky="w")

        buttons = ttk.Frame(main)
        buttons.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(12, 8))
        self.download_btn = ttk.Button(buttons, text="Скачать", style="Big.TButton",
                                       command=self.start_download, state="disabled")
        self.download_btn.pack(side="left")
        self.cancel_btn = ttk.Button(buttons, text="Отмена", command=self.cancel, state="disabled")
        self.cancel_btn.pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="Открыть папку", command=self.open_folder).pack(side="right")
        self.log_btn = ttk.Button(buttons, text="Лог", command=lambda: open_path(DEBUG_LOG))
        self.log_btn.pack(side="right", padx=(0, 8))
        if not DEBUG_LOG.is_file():
            self.log_btn.state(["disabled"])

        self.progress = ttk.Progressbar(main, mode="determinate", maximum=1.0)
        self.progress.grid(row=8, column=0, columnspan=2, sticky="ew")
        ttk.Label(main, textvariable=self.status_var, font=("Segoe UI", 10, "bold")).grid(
            row=9, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Label(main, textvariable=self.detail_var, foreground="#555").grid(
            row=10, column=0, columnspan=2, sticky="w")

        log_frame = ttk.Frame(main)
        log_frame.grid(row=11, column=0, columnspan=2, sticky="nsew", pady=(8, 0))
        main.rowconfigure(11, weight=1)
        self.log = tk.Text(log_frame, height=8, wrap="word", state="disabled",
                           font=("Consolas", 9), relief="flat", background="#f6f6f6",
                           padx=6, pady=4)
        scroll = ttk.Scrollbar(log_frame, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.log.tag_configure("error", foreground="#c62828")
        self.log.tag_configure("warn", foreground="#9a6700")
        self.log.tag_configure("ok", foreground="#1b5e20")
        self.log.tag_configure("tip", foreground="#0d47a1")

        self.url_entry.bind("<Return>", lambda _e: self.start_download())
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(100, self.poll)
        root.after(300, self.paste_if_link)
        self.start_setup()

    # ------------------------------------------------------------ события из потоков

    def emit(self, kind, *payload):
        self.queue.put((kind, payload))

    def poll(self):
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                getattr(self, f"on_{kind}")(*payload)
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def on_status(self, text):
        self.status_var.set(text)

    def on_detail(self, text):
        self.detail_var.set(text)

    def on_progress(self, fraction):
        if fraction is None:
            if str(self.progress.cget("mode")) != "indeterminate":
                self.progress.configure(mode="indeterminate")
                self.progress.start(15)
        else:
            if str(self.progress.cget("mode")) != "determinate":
                self.progress.stop()
                self.progress.configure(mode="determinate")
            self.progress["value"] = max(0.0, min(1.0, fraction))

    def on_log(self, text, tag=""):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n", tag)
        self.log.see("end")
        self.log.configure(state="disabled")

    def on_setup_done(self, error):
        self.setup_running = False
        self.on_progress(0.0)
        self.detail_var.set("")
        if error:
            self.status_var.set("Не удалось подготовить программу")
            self.on_log(f"Ошибка подготовки: {error}", "error")
            self.on_log("Проверь интернет и нажми «Скачать» - попробую ещё раз.", "tip")
        else:
            self.status_var.set("Готово к скачиванию")
        self.download_btn.state(["!disabled"])

    def on_job_done(self):
        job = self.job
        self.job = None
        self.on_progress(1.0 if job.files and not job.cancelled else 0.0)
        self.detail_var.set("")
        self.download_btn.state(["!disabled"])
        self.cancel_btn.state(["disabled"])
        self.log_btn.state(["!disabled"])

        if job.cancelled:
            self.status_var.set("Отменено")
            self.on_log("Скачивание отменено.", "warn")
            return

        new = [f for f in job.files if self._is_new(f, job.started)]
        old = [f for f in job.files if f not in new]
        if new:
            self.on_log(f"Скачано файлов: {len(new)}", "ok")
            for path in new[:20]:
                self.on_log(f"  + {Path(path).name}", "ok")
        if old:
            self.on_log(f"Уже были в папке: {len(old)}", "")
        if job.errors:
            self.on_log(f"Не скачалось, ошибок: {len(job.errors)}", "error")
            for tip in explain({classify(e) for e in job.errors}):
                self.on_log("-> " + tip, "tip")

        if job.errors and not new:
            self.status_var.set("Скачать не получилось - подробности ниже")
        elif job.errors:
            self.status_var.set(f"Скачано {len(new)}, но не всё - подробности ниже")
        elif new:
            self.status_var.set(f"Готово! Скачано файлов: {len(new)}")
        else:
            self.status_var.set("Всё уже было скачано в эту папку")
        self.root.bell()

    @staticmethod
    def _is_new(path, started):
        try:
            return Path(path).stat().st_mtime >= started
        except OSError:
            return False

    # ------------------------------------------------------------ подготовка

    def start_setup(self):
        if self.setup_running:
            return
        self.setup_running = True
        self.download_btn.state(["disabled"])

        def worker():
            error = None
            try:
                self.tools.prepare(lambda t: self.emit("status", t),
                                   lambda f: self.emit("progress", f))
            except Exception as exc:  # показываем в окне, а не падаем
                error = str(exc)
            self.emit("setup_done", error)

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------ кнопки

    def paste(self):
        try:
            text = self.root.clipboard_get().strip()
        except tk.TclError:
            return
        self.url_var.set(text)
        self.url_entry.icursor("end")

    def paste_if_link(self):
        if self.url_var.get().strip():
            return
        try:
            text = self.root.clipboard_get().strip()
        except tk.TclError:
            return
        if YOUTUBE_RE.fullmatch(text):
            self.url_var.set(text)

    def browse(self):
        folder = filedialog.askdirectory(initialdir=self.folder_var.get() or DEFAULT_FOLDER,
                                         title="Куда сохранять видео")
        if folder:
            self.folder_var.set(os.path.normpath(folder))

    def open_folder(self):
        folder = Path(self.folder_var.get().strip() or DEFAULT_FOLDER)
        folder.mkdir(parents=True, exist_ok=True)
        open_path(folder)

    def clear_fragment(self):
        self.start_var.set("")
        self.end_var.set("")

    def read_fragment(self):
        """(начало, конец) в секундах, None - целиком. Ошибка ввода -> ValueError с текстом."""
        try:
            start = parse_timecode(self.start_var.get())
            end = parse_timecode(self.end_var.get())
        except ValueError:
            raise ValueError("Не понял таймкод. Пиши так: 1:23 или 1:02:03") from None
        if start is None and end is None:
            return None
        start = start or 0.0
        if end is not None and end <= start:
            raise ValueError("Конец фрагмента должен быть позже начала")
        return start, end

    def remember(self):
        save_settings({
            "folder": self.folder_var.get().strip(),
            "quality": self.quality_var.get(),
            "whole_playlist": self.playlist_var.get(),
            "exact_cut": self.exact_var.get(),
        })

    def start_download(self):
        if self.job or self.download_btn.instate(["disabled"]):
            return
        if not self.tools.ready:
            self.start_setup()
            return
        urls = URL_RE.findall(self.url_var.get())
        if not urls:
            self.status_var.set("Вставь ссылку на видео")
            self.url_entry.focus_set()
            return
        try:
            section = self.read_fragment()
        except ValueError as exc:
            self.status_var.set(str(exc))
            return
        folder = self.folder_var.get().strip() or DEFAULT_FOLDER
        try:
            Path(folder).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.status_var.set("Не могу создать папку")
            self.on_log(str(exc), "error")
            return
        self.remember()

        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self.status_var.set("Начинаю...")
        self.on_progress(None)
        self.download_btn.state(["disabled"])
        self.cancel_btn.state(["!disabled"])

        if section:
            how = "точно по кадрам" if self.exact_var.get() else "быстро, по ключевым кадрам"
            self.on_log(f"Фрагмент: {section_text(section)} ({how})", "tip")

        self.job = Job(self.tools, urls, folder, self.quality_var.get(),
                       self.playlist_var.get(), self.emit,
                       section=section, exact_cut=self.exact_var.get())

        def worker():
            try:
                self.job.run()
            except Exception as exc:
                self.emit("log", f"Внутренняя ошибка: {exc}", "error")
                self.job.errors.append(f"ERROR: {exc}")
            self.emit("job_done")

        threading.Thread(target=worker, daemon=True).start()

    def cancel(self):
        if self.job:
            self.status_var.set("Отменяю...")
            self.job.cancel()

    def on_close(self):
        self.remember()
        if self.job:
            self.job.cancel()
        self.root.destroy()


def main():
    if IS_WIN:
        try:  # чёткий текст на мониторах с масштабированием и своя иконка на панели задач
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("YTDownloader.App")
        except Exception:
            pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
