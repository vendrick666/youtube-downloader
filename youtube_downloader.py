"""
YouTube Video Downloader - обёртка над yt-dlp.

Что делает:
  1. Раз в сутки обновляет yt-dlp (YouTube регулярно ломает старые версии).
  2. Сам находит или ставит Deno (без него yt-dlp не качает с YouTube) и FFmpeg
     (нужен, чтобы склеить видео и звук в один MP4).
  3. Спрашивает ссылку и качество: 1080p, максимальное, 720p или только звук MP3,
     а также фрагмент по таймкодам (например 1:23-2:45) - или видео целиком.
  4. Плейлисты и каналы складывает в отдельную папку с нумерацией.
  5. Если скачать не вышло - выясняет причину (бот-проверка, сеть, 403,
     видео недоступно), при необходимости ставит свежую тестовую сборку yt-dlp
     и пробует ещё раз.
  6. В конце показывает, какие файлы реально скачались, а что нет.

Запуск:
  python youtube_downloader.py                         (спросит ссылку, качество, папку)
  python youtube_downloader.py ССЫЛКА --quality 1 --folder ПАПКА [--section 1:23-2:45]

Если YouTube пишет "Sign in to confirm you're not a bot" или просит подтвердить
возраст - положи cookies.txt (экспорт куки youtube.com из браузера) рядом со
скриптом, он подхватится автоматически.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

DEFAULT_FOLDER = str(Path.home() / "Desktop" / "youtube")

MIN_PYTHON = (3, 10)
SCRIPT_DIR = Path(__file__).resolve().parent
COOKIES_FILE = SCRIPT_DIR / "cookies.txt"
DEBUG_LOG = SCRIPT_DIR / "ytdl_debug.log"
TEMP_DIR = Path(tempfile.gettempdir())
UPDATE_STAMP = TEMP_DIR / "ytdl_last_update.txt"
NIGHTLY_FLAG = TEMP_DIR / "spotdl_use_nightly_ytdlp.txt"  # общий со Spotify-скриптом
UPDATE_EVERY_SEC = 24 * 60 * 60
MEDIA_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".m4a", ".mp3", ".opus", ".ogg", ".flac", ".wav"}

SINGLE_TEMPLATE = "%(title).150B [%(id)s].%(ext)s"
PLAYLIST_TEMPLATE = "%(playlist_title).100B/%(playlist_index)s - %(title).150B [%(id)s].%(ext)s"

VIDEO_COMMON = ["--merge-output-format", "mp4", "--embed-metadata", "--embed-chapters"]
QUALITIES = {
    "1": ("1080p, MP4 (H.264) - открывается везде",
          ["-f", "bv*+ba/b", "-S", "res:1080,vcodec:h264,acodec:aac", *VIDEO_COMMON]),
    "2": ("Максимальное (до 4K/8K, MP4; кодек AV1/VP9 - нужен VLC или свежий плеер)",
          ["-f", "bv*+ba/b", "-S", "res,fps,acodec:aac", *VIDEO_COMMON]),
    "3": ("720p, MP4 (H.264) - файл поменьше",
          ["-f", "bv*+ba/b", "-S", "res:720,vcodec:h264,acodec:aac", *VIDEO_COMMON]),
    "4": ("Только звук, MP3",
          ["-f", "ba/b", "-x", "--audio-format", "mp3", "--audio-quality", "0",
           "--embed-metadata", "--embed-thumbnail"]),
}
DEFAULT_QUALITY = "1"


# ---------------------------------------------------------------- helpers

def run_py(args, quiet=False, timeout=None):
    extra = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL} if quiet else {}
    return subprocess.run([sys.executable, *args], timeout=timeout, **extra)


def py_check(code):
    try:
        return run_py(["-c", code], quiet=True, timeout=120).returncode == 0
    except subprocess.TimeoutExpired:
        return False


def py_output(code):
    try:
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=120)
        return proc.stdout.strip() if proc.returncode == 0 else ""
    except Exception:
        return ""


def update_is_due():
    try:
        return time.time() - UPDATE_STAMP.stat().st_mtime > UPDATE_EVERY_SEC
    except OSError:
        return True


def pip_install(*packages, pre=False):
    cmd = ["-m", "pip", "install", "-U", "-q", "--disable-pip-version-check"]
    if pre:
        cmd.append("--pre")
    return run_py([*cmd, *packages]).returncode == 0


# ---------------------------------------------------------------- setup

def ensure_ytdlp():
    installed = py_check("import yt_dlp, deno")
    if installed and not update_is_due():
        return True

    if installed:
        print("Обновляю yt-dlp (раз в сутки, ~10 сек)...")
    else:
        print("Устанавливаю yt-dlp и Deno (один раз, около минуты)...")

    ok = pip_install("yt-dlp[default,deno]", pre=NIGHTLY_FLAG.exists())
    if ok:
        try:
            UPDATE_STAMP.write_text(time.ctime(), encoding="utf-8")
        except OSError:
            pass
    elif installed:
        print("[ВНИМАНИЕ] Обновить не получилось, продолжаю с текущей версией.")

    if not py_check("import yt_dlp"):
        print("[ОШИБКА] yt-dlp не установился. Попробуй вручную:")
        print(f'  "{sys.executable}" -m pip install -U "yt-dlp[default,deno]"')
        return False
    return True


def find_deno():
    path = py_output("import deno; print(deno.find_deno_bin())")
    if path and Path(path).is_file():
        return path
    path = shutil.which("deno")
    if path:
        return path
    for name in ("deno.exe", "deno"):
        candidate = Path.home() / ".spotdl" / name
        if candidate.is_file():
            return str(candidate)
    return None


def find_ffmpeg():
    path = shutil.which("ffmpeg")
    if path:
        return path
    for name in ("ffmpeg.exe", "ffmpeg"):  # FFmpeg, который скачал spotDL
        candidate = Path.home() / ".spotdl" / name
        if candidate.is_file():
            return str(candidate)
    code = "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())"
    path = py_output(code)
    if not path:
        print("Скачиваю FFmpeg (один раз)...")
        pip_install("imageio-ffmpeg")
        path = py_output(code)
    return path or None


def switch_to_nightly_ytdlp():
    print("Ставлю свежую тестовую сборку yt-dlp (в ней быстрее чинят YouTube)...")
    if not pip_install("yt-dlp[default,deno]", pre=True):
        print("[ВНИМАНИЕ] Не получилось обновить yt-dlp.")
        return False
    try:
        NIGHTLY_FLAG.write_text(time.ctime(), encoding="utf-8")
    except OSError:
        pass
    return True


# ---------------------------------------------------------------- input

def parse_urls(raw):
    return [part for part in raw.replace('"', " ").replace("'", " ").split()
            if part.startswith("http")]


def is_playlist_page(url):
    """Ссылка именно на плейлист или канал, а не на одно видео."""
    if re.search(r"youtube\.com/(playlist\?|@|channel/|c/|user/)", url):
        return True
    return False


def has_playlist_param(url):
    return "list=" in url and ("v=" in url or "youtu.be/" in url)


def ask(prompt, default=""):
    try:
        answer = input(prompt).strip()
    except EOFError:
        answer = ""
    return answer or default


def choose_quality(preset):
    if preset in QUALITIES:
        return preset
    print("Качество:")
    for key, (title, _) in QUALITIES.items():
        mark = "  (Enter)" if key == DEFAULT_QUALITY else ""
        print(f"  {key}) {title}{mark}")
    choice = ask("Выбери [1-4]: ", DEFAULT_QUALITY)
    return choice if choice in QUALITIES else DEFAULT_QUALITY


def parse_timecode(text):
    """'83', '1:23', '1.23', '01:02:03', '1:23.5' -> секунды; пустая строка -> None."""
    text = text.strip().replace(",", ".").replace(" ", "")
    if not text:
        return None
    if ":" not in text and "." in text:
        text = text.replace(".", ":")  # "1.23" часто пишут вместо "1:23"
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
    text = (f"{whole // 3600}:{whole % 3600 // 60:02d}:{whole % 60:02d}" if whole >= 3600
            else f"{whole // 60}:{whole % 60:02d}")
    frac = seconds - whole
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


def parse_section(text):
    """'1:23-2:45', '1:23-' или '1:23' (до конца), '-2:45' (с начала) -> (начало, конец)."""
    text = text.strip()
    if not text:
        return None
    start_text, dash, end_text = text.partition("-")
    start = parse_timecode(start_text) or 0.0
    end = parse_timecode(end_text) if dash else None
    if end is not None and end <= start:
        raise ValueError("конец раньше начала")
    return start, end


def choose_section(preset):
    if preset is not None:
        return parse_section(preset)
    while True:
        text = ask("Фрагмент, например 1:23-2:45 [Enter = видео целиком]: ")
        try:
            return parse_section(text)
        except ValueError:
            print("Не понял. Пиши так: 1:23-2:45, 1:23- (до конца) или -2:45 (с начала).")


# ---------------------------------------------------------------- download

def media_snapshot(folder):
    result = {}
    for f in Path(folder).rglob("*"):
        if f.suffix.lower() in MEDIA_EXTS and f.is_file():
            result[f] = f.stat().st_mtime
    return result


def run_ytdlp(args):
    """Прогресс идёт прямо в консоль, а сообщения об ошибках мы ещё и запоминаем."""
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.Popen([sys.executable, "-m", "yt_dlp", *args],
                            stderr=subprocess.PIPE, env=env)
    lines = []
    for raw in proc.stderr:
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        lines.append(line)
        print(line, flush=True)
    proc.wait()
    return proc.returncode, lines


def build_args(url, folder, quality, whole_playlist, ffmpeg, deno, section=None):
    playlist_mode = is_playlist_page(url) or (whole_playlist and has_playlist_param(url))
    template = PLAYLIST_TEMPLATE if playlist_mode else SINGLE_TEMPLATE
    extra = []
    if section:
        start, end = section
        # фрагмент получает своё имя, чтобы не путаться с целым видео
        suffix = f" {file_label(start)}-{file_label(end) if end is not None else 'конец'}"
        template = template[:-len(".%(ext)s")] + suffix + ".%(ext)s"
        end_arg = seconds_arg(end) if end is not None else "inf"
        extra = ["--download-sections", f"*{seconds_arg(start)}-{end_arg}",
                 "--force-keyframes-at-cuts"]
    args = [
        *QUALITIES[quality][1], *extra,
        "-P", folder,
        "-o", template,
        "-N", "4",
        "--yes-playlist" if playlist_mode else "--no-playlist",
    ]
    if ffmpeg:
        args += ["--ffmpeg-location", ffmpeg]
    if deno:
        args += ["--js-runtimes", f"deno:{deno}"]
    if COOKIES_FILE.is_file():
        args += ["--cookies", str(COOKIES_FILE)]
    return args + [url]


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


def write_debug_log(url, args, lines, rc):
    versions = py_output("import yt_dlp.version as v; print(v.__version__)")
    try:
        DEBUG_LOG.write_text(
            f"{time.ctime()}\nPython {sys.version.split()[0]} ({sys.executable})\n"
            f"yt-dlp {versions}\nКод выхода: {rc}\nURL: {url}\n"
            f"Аргументы: {args}\n\n" + "\n".join(lines),
            encoding="utf-8",
        )
    except OSError:
        pass


def explain(kinds):
    if "bot" in kinds or "login" in kinds:
        if "bot" in kinds:
            print("-> YouTube просит подтвердить, что ты не бот.")
        else:
            print("-> Видео с ограничением по возрасту или только для спонсоров канала.")
        print("   Нужен cookies.txt: поставь в браузер расширение 'Get cookies.txt LOCALLY',")
        print("   открой youtube.com, будучи залогиненным, экспортируй куки и сохрани")
        print(f"   файл как cookies.txt сюда: {SCRIPT_DIR}")
    if "network" in kinds:
        print("-> Компьютер не может достучаться до серверов YouTube - это сеть или DNS.")
        print("   Помогает смена DNS на 1.1.1.1 / 8.8.8.8 или VPN.")
    if "unavailable" in kinds:
        print("-> Часть видео недоступна: удалено, приватное или ещё не вышло.")
    if "forbidden" in kinds:
        print("-> YouTube запретил скачивание (403). Обычно лечится свежим yt-dlp.")


# ---------------------------------------------------------------- main

def main():
    parser = argparse.ArgumentParser(description="YouTube Video Downloader")
    parser.add_argument("url", nargs="?", help="ссылка (или несколько через пробел)")
    parser.add_argument("--folder", help="папка для сохранения")
    parser.add_argument("--quality", help="1 - 1080p, 2 - максимум, 3 - 720p, 4 - MP3")
    parser.add_argument("--section", help="только фрагмент, например 1:23-2:45 или 1:23- (до конца)")
    parser.add_argument("--no-pause", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    try:  # UTF-8 и построчный вывод: чтобы лог в файле был читаемым и по порядку
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except (AttributeError, ValueError):
        pass

    print("==========================================")
    print("      YOUTUBE VIDEO DOWNLOADER")
    print("==========================================\n")

    if sys.version_info < MIN_PYTHON:
        print(f"[ОШИБКА] Нужен Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} или новее, "
              f"а запущен {sys.version.split()[0]}.")
        print("Скачай свежий Python: https://www.python.org/downloads/")
        return 1

    if not ensure_ytdlp():
        return 1
    deno = find_deno()
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        print("[ВНИМАНИЕ] FFmpeg не найден: видео выше 720p может не склеиться со звуком.")

    folder = args.folder or ask(f"Папка для видео [Enter = {DEFAULT_FOLDER}]: ",
                                DEFAULT_FOLDER).strip('"')

    urls = parse_urls(args.url or ask("Вставь ссылку YouTube: "))
    if not urls:
        print("Ошибка: ссылка не указана!")
        return 1

    whole_playlist = False
    if any(has_playlist_param(u) and not is_playlist_page(u) for u in urls):
        whole_playlist = ask("Ссылка из плейлиста. Скачать весь плейлист? [y/N]: ",
                             "n").lower() in ("y", "yes", "д", "да")

    quality = choose_quality(args.quality)
    try:
        section = choose_section(args.section)
    except ValueError:
        print("Ошибка: не понял фрагмент. Пример: --section 1:23-2:45")
        return 1

    os.makedirs(folder, exist_ok=True)
    before = media_snapshot(folder)

    print("\n==========================================")
    print(f"Папка сохранения: {folder}")
    print(f"Качество: {QUALITIES[quality][0]}")
    if section:
        start, end = section
        print(f"Фрагмент: {format_timecode(start)} - "
              f"{format_timecode(end) if end is not None else 'до конца'} (режу точно по кадрам)")
    if COOKIES_FILE.is_file():
        print("Cookies: cookies.txt найден, использую")
    print("==========================================\n")

    failed = []  # (url, [строки ERROR])
    nightly_tried = False
    for url in urls:
        ytdlp_args = build_args(url, folder, quality, whole_playlist, ffmpeg, deno, section)
        rc, lines = run_ytdlp(ytdlp_args)
        errors = [line for line in lines if line.startswith("ERROR:")]

        if rc != 0 and errors:
            kinds = {classify(line) for line in errors}
            if kinds & {"forbidden", "other"} and not nightly_tried:
                nightly_tried = True
                print("\nСкачать не вышло, пробую ещё раз со свежим yt-dlp...\n")
                if switch_to_nightly_ytdlp():
                    rc, lines = run_ytdlp(ytdlp_args)  # готовые файлы yt-dlp пропустит
                    errors = [line for line in lines if line.startswith("ERROR:")]

        if rc != 0:
            write_debug_log(url, ytdlp_args, lines, rc)
            failed.append((url, errors or [line for line in lines if line.strip()][-3:]))

    after = media_snapshot(folder)
    saved = sorted(f for f, m in after.items() if before.get(f) != m)

    print("\n==========================================")
    if saved:
        print(f"[УСПЕХ] Скачано файлов: {len(saved)}")
        for f in saved[:15]:
            print(f"  + {f.relative_to(folder)}")
        if len(saved) > 15:
            print(f"  ... и ещё {len(saved) - 15}")
        print(f"Папка: {folder}")
    elif not failed:
        print("Новых файлов нет - похоже, это уже скачано в эту папку.")

    if failed:
        all_errors = [line for _, errs in failed for line in errs]
        print(f"\n[ОШИБКА] Не всё скачалось. Ошибок: {len(all_errors)}")
        for line in all_errors[:10]:
            print(f"  - {line[:300]}")
        if len(all_errors) > 10:
            print(f"  ... и ещё {len(all_errors) - 10}")
        print()
        explain({classify(line) for line in all_errors})
        print(f"   Подробный лог: {DEBUG_LOG}")
    print("==========================================")
    return 1 if failed and not saved else 0


if __name__ == "__main__":
    code = main()
    if "--no-pause" not in sys.argv and sys.stdin and sys.stdin.isatty():
        input("\nНажми Enter, чтобы закрыть...")
    sys.exit(code)
