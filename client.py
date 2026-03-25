import os
import json
import time
import subprocess
import requests
import signal
import threading
from datetime import datetime
from urllib.parse import urlparse
from pathlib import Path

CONFIG_FILE = "config.json"
MEDIA_DIR = Path("content").resolve()
PLAYLIST_FILE = MEDIA_DIR / "playlist.m3u"
player_process = None

def log(msg):
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{now}] {msg}")

def stop_player():
    global player_process
    if player_process:
        try:
            os.killpg(os.getpgid(player_process.pid), signal.SIGTERM)
        except:
            pass
        player_process = None
        log("Плеер остановлен")

def start_player():
    global player_process
    if not PLAYLIST_FILE.exists():
        log("playlist.m3u не найден")
        return False

    log("Запуск mpv (--vo=drm)")

    cmd = [
        "mpv",
        "--fs",
        "--loop-playlist=inf",
        "--no-osc",
        "--no-audio",
        "--no-border",
        "--keep-open=always",
        "--really-quiet",
        "--vo=drm",
        "--gpu-context=drm",
        "--hwdec=auto",
        f"--playlist={PLAYLIST_FILE}"
    ]

    try:
        player_process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            preexec_fn=os.setsid
        )
        log("mpv запущен успешно")
        return True
    except Exception as e:
        log(f"Ошибка запуска mpv: {e}")
        return False


def load_config():
    with open(CONFIG_FILE) as f:
        return json.load(f)


def save_config(cfg):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=4)


def get_local_ids():
    if not MEDIA_DIR.exists():
        return []
    ids = set()
    for f in MEDIA_DIR.iterdir():
        if f.is_file():
            name = f.stem
            if "_p-" in name:                     # страница PDF
                ids.add(name.split("_p-")[0])
            else:
                ids.add(name)
    return list(ids)


def build_playlist(videos_data):
    MEDIA_DIR.mkdir(exist_ok=True)
    with open(PLAYLIST_FILE, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for v in videos_data:
            fid = v["id"]
            ftype = v.get("file_type", "video")
            playback = v.get("playback", {})
            
            if ftype == "pdf":
                pages = playback.get("pdf_page_durations", [])
                if not pages:
                    pages = [{"page": 1, "duration": 5}]
                for p in pages:
                    page_file = MEDIA_DIR / f"{fid}_p-{p['page']:03d}.png"
                    if page_file.exists():
                        f.write(f"#EXTINF:{p['duration']},page{p['page']}\n")
                        f.write(f"{page_file}\n")
            else:
                # video / image
                for ext in [".mp4", ".png", ".jpg", ".jpeg"]:
                    candidate = MEDIA_DIR / f"{fid}{ext}"
                    if candidate.exists():
                        duration = playback.get("duration_seconds")
                        dur = duration if duration and duration > 0 else -1
                        f.write(f"#EXTINF:{dur},{fid}\n")
                        f.write(f"{candidate}\n")
                        break


def heartbeat(config):
    try:
        r = requests.post(
            f"{config['server_url']}/api/heartbeat",
            json={"token": config['token'], "id": config['device_id']},
            timeout=10
        )
        data = r.json()
        status = data.get("status")
        if status in (200, "actual") or data.get("success") is True:
            return "ok"
        if status in (403, "403"):
            return "blocked"
        return "invalid"
    except:
        return None


def check_videos(config):
    try:
        current_ids = get_local_ids()
        r = requests.post(
            f"{config['server_url']}/api/check-videos",
            json={
                "token": config['token'],
                "id": config['device_id'],
                "videos": current_ids
            },
            timeout=15
        )
        data = r.json()
        if data.get("status") == 205:
            log("Получен новый контент (205)")
            stop_player()
            # очистка
            for f in MEDIA_DIR.iterdir():
                if f.is_file():
                    f.unlink()
            # скачивание
            for v in data.get("videos", []):
                fid = v["id"]
                url = v["url"]
                ext = os.path.splitext(urlparse(url).path)[1].lower() or ".mp4"
                path = MEDIA_DIR / f"{fid}{ext}"
                subprocess.run(["wget", "-q", "-O", str(path), url], check=True)
                if v.get("file_type") == "pdf":
                    subprocess.run(["pdftoppm", "-r", "150", "-png", str(path), str(MEDIA_DIR / f"{fid}_p")],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    try:
                        path.unlink()
                    except:
                        pass
            build_playlist(data.get("videos", []))
            start_player()
            return True
        return False
    except Exception as e:
        log(f"check_videos ошибка: {e}")
        return False


def main():
    config = load_config()
    log("Клиент запущен (минимальная версия)")

    signal.signal(signal.SIGINT, lambda *a: stop_player())
    signal.signal(signal.SIGTERM, lambda *a: stop_player())

    last_hb = 0
    last_check = 0

    while True:
        now = time.time()

        # Heartbeat
        if now - last_hb > config.get("heartbeat_interval", 30):
            status = heartbeat(config)
            last_hb = now
            if status == "blocked":
                log("Устройство заблокировано")
                stop_player()
            elif status == "ok":
                log("Устройство активно")

        # Check-videos
        if now - last_check > config.get("check_videos_interval", 60):
            check_videos(config)
            last_check = now

        # Авторестарт mpv, если он упал
        global player_process
        if player_process and player_process.poll() is not None:
            log("mpv упал — перезапускаем")
            start_player()

        time.sleep(1)


if __name__ == "__main__":
    MEDIA_DIR.mkdir(exist_ok=True)
    main()