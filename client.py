import os
import re
import json
import time
import glob
import subprocess
import requests
import signal
import threading
from datetime import datetime
from urllib.parse import urlparse
from pathlib import Path

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')
player_process = None

# Паттерн для страниц PDF
PDF_PAGE_RE = re.compile(r'^(.+)_p-\d+\.png$')

def stop_player():
    global player_process
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    if player_process:
        try:
            os.killpg(os.getpgid(player_process.pid), signal.SIGTERM)
            player_process = None
            print(f"[Player {now}] Плеер остановлен")
        except Exception as e:
            print(f"[Player {now}] Ошибка остановки плеера: {e}")


def start_player(media_dir):
    global player_process
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    playlist = os.path.join(media_dir, "playlist.m3u")

    if not os.path.exists(playlist):
        print(f"[Player {now}] playlist.m3u не найден")
        return

    print(f"[Player {now}] Запуск mpv с DRM")

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
        f"--playlist={playlist}"
    ]

    try:
        player_process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            preexec_fn=os.setsid
        )
        print(f"[Player {now}] mpv запущен успешно")
    except Exception as e:
        print(f"[Player {now}] Ошибка запуска mpv: {e}")


def load_config():
    with open(CONFIG_FILE, 'r') as f:
        return json.load(f)


def save_config(config):
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=4)


def get_local_file_ids(media_dir):
    if not os.path.exists(media_dir):
        return []
    ids = set()
    for filename in os.listdir(media_dir):
        if not os.path.isfile(os.path.join(media_dir, filename)):
            continue
        m = PDF_PAGE_RE.match(filename)
        if m:
            ids.add(m.group(1))
        else:
            ids.add(os.path.splitext(filename)[0])
    return list(ids)


def convert_pdf_to_images(pdf_path, file_id, media_dir):
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    prefix = os.path.join(media_dir, f"{file_id}_p")
    try:
        subprocess.run(
            ['pdftoppm', '-r', '150', '-png', pdf_path, prefix],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        print(f"[PDF {now}] {file_id} → страницы созданы")
    except Exception as e:
        print(f"[PDF {now}] Ошибка конвертации PDF: {e}")
    finally:
        try:
            os.remove(pdf_path)
        except:
            pass


def build_m3u_playlist(media_dir, videos_data):
    playlist_path = os.path.join(media_dir, "playlist.m3u")
    with open(playlist_path, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for v in videos_data:
            file_id = v["id"]
            playback = v.get("playback", {})
            ftype = v.get("file_type", "video")

            if ftype == "pdf":
                pages = playback.get("pdf_page_durations", [])
                if not pages:
                    pages = [{"page": 1, "duration": 5}]
                for p in pages:
                    page_path = os.path.join(media_dir, f"{file_id}_p-{p['page']:03d}.png")
                    if os.path.exists(page_path):
                        f.write(f"#EXTINF:{p['duration']},{file_id}_page{p['page']}\n")
                        f.write(f"{page_path}\n")
            else:
                # video или image
                file_path = None
                for ext in [".mp4", ".png", ".jpg", ".jpeg"]:
                    candidate = os.path.join(media_dir, f"{file_id}{ext}")
                    if os.path.exists(candidate):
                        file_path = candidate
                        break
                if file_path:
                    duration = playback.get("duration_seconds")
                    dur = duration if duration and duration > 0 else -1
                    f.write(f"#EXTINF:{dur},{file_id}\n")
                    f.write(f"{file_path}\n")
    return playlist_path


def sync_token(config):
    url = f"{config['server_url']}/api/sync-token"
    payload = {"token": config['token'], "id": config['device_id']}
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    try:
        r = requests.post(url, json=payload, timeout=10)
        data = r.json()
        if data.get("success") and data.get("status") == "updated":
            config['token'] = data['new_token']
            save_config(config)
            print(f"[* {now}] Токен обновлён")
            return True
    except Exception as e:
        print(f"[! {now}] sync_token ошибка: {e}")
    return False


def heartbeat(config):
    url = f"{config['server_url']}/api/heartbeat"
    payload = {"token": config['token'], "id": config['device_id']}
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    try:
        resp = requests.post(url, json=payload, timeout=10)
        data = resp.json()
        status = data.get("status")

        if status in (200, "actual", "ok") or data.get("success") is True:
            return "ok"
        if status in (403, "403", "blocked"):
            return "blocked"
        return "invalid"
    except Exception as e:
        print(f"[Heartbeat {now}] ошибка: {e}")
        return None


def download_content(videos, media_dir):
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[* {now}] Очистка и загрузка нового контента...")

    for f in os.listdir(media_dir):
        try:
            os.unlink(os.path.join(media_dir, f))
        except:
            pass

    for v in videos:
        v_id = v['id']
        v_url = v['url']
        ftype = v.get('file_type', 'video')

        ext = os.path.splitext(urlparse(v_url).path)[1].lower() or ".mp4"
        target_path = os.path.join(media_dir, f"{v_id}{ext}")

        print(f"[* {now}] Загрузка {v_id}")
        try:
            subprocess.run(['wget', '-O', target_path, v_url], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            print(f"[!] Ошибка загрузки {v_id}: {e}")
            continue

        if ftype == "pdf":
            convert_pdf_to_images(target_path, v_id, media_dir)


class App:
    def __init__(self):
        self.config = load_config()
        self.last_hb = 0
        self.last_check = 0
        self.is_blocked = False

    def handle_blocked(self):
        if not self.is_blocked:
            print(f"[!] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} Устройство заблокировано")
            self.is_blocked = True
            stop_player()

    def handle_unblocked(self):
        if self.is_blocked:
            print(f"[*] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} Устройство разблокировано")
            self.is_blocked = False
            start_player(self.config['media_dir'])

    def worker_loop(self):
        while True:
            now_ts = time.time()
            now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

            # Heartbeat
            if now_ts - self.last_hb > self.config.get('heartbeat_interval', 30):
                status = heartbeat(self.config)
                self.last_hb = now_ts

                if status == "blocked":
                    self.handle_blocked()
                elif status == "invalid":
                    self.handle_blocked()
                    sync_token(self.config)
                elif status == "ok":
                    self.handle_unblocked()

            # Check-videos
            if not self.is_blocked and now_ts - self.last_check > self.config.get('check_videos_interval', 60):
                self.process_check_videos(now_str)
                self.last_check = now_ts

            time.sleep(1)

    def process_check_videos(self, now_str):
        url = f"{self.config['server_url']}/api/check-videos"
        current_ids = get_local_file_ids(self.config['media_dir'])

        payload = {
            "token": self.config['token'],
            "id": self.config['device_id'],
            "videos": current_ids
        }

        try:
            resp = requests.post(url, json=payload, timeout=15)
            data = resp.json()
            status = data.get("status")

            if status == 205:
                print(f"[{now_str}] Новый контент (205)")
                stop_player()
                download_content(data.get("videos", []), self.config['media_dir'])
                build_m3u_playlist(self.config['media_dir'], data.get("videos", []))
                start_player(self.config['media_dir'])

            elif status == 204:
                global player_process
                if player_process is None or player_process.poll() is not None:
                    start_player(self.config['media_dir'])

        except Exception as e:
            print(f"[{now_str}] check_videos ошибка: {e}")

    def run(self):
        signal.signal(signal.SIGINT, lambda *a: stop_player())
        signal.signal(signal.SIGTERM, lambda *a: stop_player())

        print("🚀 Клиент запущен (headless режим)")
        t = threading.Thread(target=self.worker_loop, daemon=True)
        t.start()
        t.join()   # держим главный поток живым


if __name__ == "__main__":
    app = App()
    app.run()