import os
import re
import json
import time
import glob
import subprocess
import requests
import tkinter as tk
import threading
from datetime import datetime
import signal
from urllib.parse import urlparse

CONFIG_FILE = 'config.json'
player_process = None

# Паттерн имён файлов, полученных из PDF: {file_id}_p-001.png
PDF_PAGE_RE = re.compile(r'^(.+)_p-\d+\.png$')


class BlackCurtain:
    def __init__(self):
        self.root = None
        self.thread = None

    def _create_window(self):
        self.root = tk.Tk()
        self.root.attributes('-fullscreen', True)
        self.root.configure(background='black')
        self.root.config(cursor="none")
        self.root.bind("<Escape>", lambda e: self.stop())
        self.root.mainloop()

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._create_window, daemon=True)
        self.thread.start()
        time.sleep(1)

    def stop(self):
        if self.root:
            self.root.after(0, self.root.destroy)
            self.thread.join()
            self.root = None


curtain = BlackCurtain()


def stop_player():
    global player_process
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    if player_process:
        try:
            os.killpg(os.getpgid(player_process.pid), signal.SIGTERM)
            player_process = None
            print(f"[Player {now}] Предыдущий процесс плеера остановлен.")
        except Exception as e:
            print(f"[Player {now}] Ошибка при остановке плеера: {e}")


def start_player(media_dir, image_duration=5):
    global player_process
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    files = sorted([
        os.path.join(media_dir, f)
        for f in os.listdir(media_dir)
        if os.path.isfile(os.path.join(media_dir, f))
    ])

    if not files:
        print(f"[Player {now}] Нет файлов для воспроизведения.")
        return

    print(f"[Player {now}] Запуск воспроизведения {len(files)} файлов.")
    cmd = [
        "mpv", "--fs", "--loop-playlist", "--no-osc", "--no-audio",
        f"--image-display-duration={image_duration}",
    ] + files

    try:
        player_process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            preexec_fn=os.setsid
        )
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
        os.makedirs(media_dir)
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
        pages = glob.glob(f"{prefix}-*.png")
        print(f"[* {now}] PDF {file_id} → {len(pages)} стр.")
    except FileNotFoundError:
        print(f"[! {now}] pdftoppm не найден. Установите: sudo apt install poppler-utils")
    except subprocess.CalledProcessError as e:
        print(f"[! {now}] Ошибка конвертации PDF {file_id}: {e}")
    finally:
        try:
            os.remove(pdf_path)
        except OSError:
            pass


def sync_token(config):
    url = f"{config['server_url']}/api/sync-token"
    payload = {"token": config['token'], "id": config['device_id']}
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    try:
        response = requests.post(url, json=payload, timeout=10)
        data = response.json()
        if data.get("success") and data.get("status") == "updated":
            config['token'] = data['new_token']
            save_config(config)
            print(f"[* {now}] Токен успешно обновлен: {config['token']}")
            return True
        return False
    except Exception as e:
        print(f"[! {now}] Ошибка синхронизации: {e}")
        return False


def heartbeat(config):
    """Отправляет пинг серверу и возвращает нормализованный статус:
      "ok"      — устройство активно, токен валиден
      "blocked" — устройство заблокировано
      "invalid" — токен недействителен, нужен sync-token
      None      — сервер недоступен
    """
    url = f"{config['server_url']}/api/heartbeat"
    payload = {"token": config['token'], "id": config['device_id']}
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    try:
        resp = requests.post(url, json=payload, timeout=10)
        data = resp.json()
        status = data.get("status")
        success = data.get("success")
        message = data.get("message", "")
        print(f"[Heartbeat {now}] success={success} status={status} msg={message}")

        # Токен обновлён — сохраняем новый
        if status == "updated":
            new_token = data.get("new_token")
            if new_token:
                config['token'] = new_token
                save_config(config)
                print(f"[* {now}] Токен обновлён через heartbeat: {new_token}")
            return "ok"

        # Устройство заблокировано
        if status == 403 or status == "403" or str(status) == "403":
            return "blocked"

        # Любой признак успеха: "actual", 200, success=True
        if status == "actual" or status == 200 or success is True:
            return "ok"

        # Всё остальное — токен недействителен
        return "invalid"

    except Exception as e:
        print(f"[Heartbeat {now}] Error: {e}")
        return None


def download_content(videos, media_dir):
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[* {now}] Очистка локального контента...")
    if os.path.exists(media_dir):
        for file in os.listdir(media_dir):
            file_path = os.path.join(media_dir, file)
            try:
                if os.path.isfile(file_path):
                    os.unlink(file_path)
            except Exception as e:
                print(f"[! {now}] Ошибка удаления {file_path}: {e}")
    else:
        os.makedirs(media_dir)

    for v in videos:
        v_id = v['id']
        v_url = v['url']
        ext = os.path.splitext(urlparse(v_url).path)[1].lower()
        target_filename = f"{v_id}{ext}"
        target_path = os.path.join(media_dir, target_filename)
        print(f"[* {now}] Загрузка {v_id} ({ext}) -> {target_filename}")
        try:
            subprocess.run(
                ['wget', '-O', target_path, v_url],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        except subprocess.CalledProcessError as e:
            print(f"[! {now}] Ошибка скачивания {v_id}: {e}")
            continue
        if ext == '.pdf':
            convert_pdf_to_images(target_path, v_id, media_dir)


class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.attributes('-fullscreen', True)
        self.root.configure(background='black')
        self.root.config(cursor="none")
        self.root.withdraw()

        self.config = load_config()
        self.last_hb = 0
        self.last_check = 0
        self.is_blocked = False

    def show_curtain(self):
        self.root.deiconify()
        self.root.update()

    def hide_curtain(self):
        self.root.withdraw()
        self.root.update()

    def handle_blocked(self):
        if not self.is_blocked:
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            print(f"[! {now}] Устройство заблокировано. Остановка воспроизведения.")
            self.is_blocked = True
            stop_player()
            self.root.after(0, self.show_curtain)

    def handle_unblocked(self):
        if self.is_blocked:
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            print(f"[* {now}] Устройство разблокировано. Запуск воспроизведения.")
            self.is_blocked = False
            start_player(
                self.config['media_dir'],
                image_duration=self.config.get('image_display_duration', 5)
            )
            time.sleep(2)
            self.root.after(0, self.hide_curtain)

    def shutdown(self, *_):
        stop_player()
        self.root.after(0, self.root.destroy)

    def worker_loop(self):
        while True:
            now_ts = time.time()
            now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

            if now_ts - self.last_hb > self.config.get('heartbeat_interval', 30):
                status = heartbeat(self.config)
                self.last_hb = now_ts

                if status == "blocked":
                    self.handle_blocked()
                elif status == "ok":
                    self.handle_unblocked()
                elif status == "invalid":
                    sync_token(self.config)
                # None — сервер недоступен, ждём следующего heartbeat

            # check-videos пропускаем пока устройство заблокировано
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
            resp = requests.post(url, json=payload, timeout=10)
            data = resp.json()
            status = data.get("status")

            if status == 205:
                print(f"[{now_str}] Обновление контента...")
                self.root.after(0, self.show_curtain)
                stop_player()
                download_content(data.get("videos", []), self.config['media_dir'])
                start_player(
                    self.config['media_dir'],
                    image_duration=self.config.get('image_display_duration', 5)
                )
                time.sleep(3)
                self.root.after(0, self.hide_curtain)

            elif status == 204:
                global player_process
                if player_process is None or player_process.poll() is not None:
                    start_player(
                        self.config['media_dir'],
                        image_duration=self.config.get('image_display_duration', 5)
                    )

            elif status == 401:
                sync_token(self.config)

            elif status == 403:
                self.handle_blocked()

        except Exception as e:
            print(f"[{now_str}] Ошибка check_videos: {e}")

    def run(self):
        signal.signal(signal.SIGINT, self.shutdown)
        signal.signal(signal.SIGTERM, self.shutdown)

        def _poll():
            self.root.after(200, _poll)
        _poll()

        t = threading.Thread(target=self.worker_loop, daemon=True)
        t.start()
        self.root.mainloop()


if __name__ == "__main__":
    app = App()
    app.run()