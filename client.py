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
curtain_process = None

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

def start_curtain():
    global curtain_process
    if curtain_process and curtain_process.poll() is None:
        return
    try:
        curtain_process = subprocess.Popen(["python3", "-c", """
import tkinter as tk
root = tk.Tk()
root.attributes('-fullscreen', True)
root.configure(background='black')
root.config(cursor="none")
root.mainloop()
"""], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log("Чёрная шторка запущена")
    except:
        log("Не удалось запустить шторку (Tkinter)")

def stop_curtain():
    global curtain_process
    if curtain_process:
        try:
            curtain_process.terminate()
        except:
            pass
        curtain_process = None

def start_player():
    global player_process
    if not PLAYLIST_FILE.exists():
        log("playlist.m3u не найден")
        return False

    log("Запуск mpv (стабильный режим)")

    # Пробуем несколько вариантов vo в порядке стабильности
    vo_options = [
        ["--vo=xv", "--hwdec=auto"],           # самый стабильный на многих RK3588
        ["--vo=gpu", "--gpu-context=x11", "--hwdec=auto"],
        ["--vo=drm", "--gpu-context=drm", "--hwdec=auto"]
    ]

    for vo in vo_options:
        cmd = [
            "mpv", "--fs", "--loop-playlist=inf", "--no-osc", "--no-audio",
            "--no-border", "--keep-open=always", "--really-quiet"
        ] + vo + [f"--playlist={PLAYLIST_FILE}"]

        try:
            player_process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                preexec_fn=os.setsid
            )
            time.sleep(2)  # даём время на запуск
            if player_process.poll() is None:
                log(f"mpv запущен успешно с {vo[0]}")
                return True
            else:
                log(f"mpv упал с {vo[0]}")
        except Exception as e:
            log(f"Ошибка с {vo[0]}: {e}")

    log("Все варианты vo провалены")
    return False


def build_m3u_playlist(videos_data):
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
        r = requests.post(f"{config['server_url']}/api/heartbeat",
                          json={"token": config['token'], "id": config['device_id']}, timeout=8)
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
        current_ids = [f.stem.split('_p-')[0] if '_p-' in f.stem else f.stem 
                       for f in MEDIA_DIR.iterdir() if f.is_file()]

        r = requests.post(f"{config['server_url']}/api/check-videos",
                          json={"token": config['token'], "id": config['device_id'], "videos": current_ids},
                          timeout=15)
        data = r.json()

        if data.get("status") == 205:
            log("Получен новый контент (205)")
            stop_player()
            stop_curtain()

            # очистка
            for f in MEDIA_DIR.iterdir():
                if f.is_file():
                    f.unlink()

            # скачивание + обработка PDF
            for v in data.get("videos", []):
                fid = v["id"]
                url = v["url"]
                ext = os.path.splitext(urlparse(url).path)[1].lower() or ".mp4"
                path = MEDIA_DIR / f"{fid}{ext}"
                subprocess.run(["wget", "-q", "-O", str(path), url], check=True)

                if v.get("file_type") == "pdf":
                    subprocess.run(["pdftoppm", "-r", "150", "-png", str(path),
                                    str(MEDIA_DIR / f"{fid}_p")],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    try:
                        path.unlink()
                    except:
                        pass

            build_m3u_playlist(data.get("videos", []))
            start_player()
            return True
        return False
    except Exception as e:
        log(f"check_videos ошибка: {e}")
        return False


def main():
    config = load_config()
    log("Клиент запущен (стабильная версия)")

    signal.signal(signal.SIGINT, lambda *a: (stop_player(), stop_curtain()))
    signal.signal(signal.SIGTERM, lambda *a: (stop_player(), stop_curtain()))

    last_hb = 0
    last_check = 0

    while True:
        now = time.time()

        if now - last_hb > config.get("heartbeat_interval", 30):
            status = heartbeat(config)
            last_hb = now
            if status == "blocked":
                log("Устройство заблокировано")
                stop_player()
                start_curtain()
            elif status == "ok":
                stop_curtain()

        if now - last_check > config.get("check_videos_interval", 60):
            check_videos(config)
            last_check = now

        # Авторестарт mpv если упал
        global player_process
        if player_process and player_process.poll() is not None:
            log("mpv упал — перезапускаем")
            start_player()

        time.sleep(1)


if __name__ == "__main__":
    MEDIA_DIR.mkdir(exist_ok=True)
    main()