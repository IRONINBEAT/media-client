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

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')
PLAYLIST_STATE_FILENAME = "playlist_state.json"

# Паттерн имён файлов, полученных из PDF: {file_id}_p-001.png
PDF_PAGE_RE = re.compile(r'^(.+)_p-\d+\.png$')
PDF_PAGE_INDEX_RE = re.compile(r'_p-(\d+)\.png$')


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


def get_playlist_state_path(media_dir):
    return os.path.join(media_dir, PLAYLIST_STATE_FILENAME)


def normalize_duration(raw_duration, fallback_duration):
    try:
        value = int(raw_duration)
        if value > 0:
            return value
    except (TypeError, ValueError):
        pass
    return int(fallback_duration)


def extract_pdf_page_index(path):
    name = os.path.basename(path)
    match = PDF_PAGE_INDEX_RE.search(name)
    if not match:
        return -1
    try:
        return int(match.group(1))
    except ValueError:
        return -1


def save_playlist_state(media_dir, playlist):
    os.makedirs(media_dir, exist_ok=True)
    path = get_playlist_state_path(media_dir)
    with open(path, 'w') as f:
        json.dump(playlist, f, indent=4)


def load_playlist_state(media_dir):
    path = get_playlist_state_path(media_dir)
    if not os.path.exists(path):
        return []
    try:
        with open(path, 'r') as f:
            playlist = json.load(f)
    except Exception:
        return []

    if not isinstance(playlist, list):
        return []

    valid_items = []
    for item in playlist:
        if not isinstance(item, dict):
            continue
        paths = item.get("paths", [])
        if not isinstance(paths, list):
            continue
        existing_paths = [p for p in paths if isinstance(p, str) and os.path.exists(p)]
        if not existing_paths:
            continue
        valid_items.append({
            "id": str(item.get("id", "")),
            "file_type": str(item.get("file_type", "video")),
            "paths": existing_paths,
            "duration_seconds": item.get("duration_seconds"),
            "page_durations": item.get("page_durations", []),
        })
    return valid_items


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
        if filename == PLAYLIST_STATE_FILENAME:
            continue
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
    pages = []
    try:
        subprocess.run(
            ['pdftoppm', '-r', '150', '-png', pdf_path, prefix],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        pages = sorted(glob.glob(f"{prefix}-*.png"), key=extract_pdf_page_index)
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
    return pages


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

        # Токен недействителен (unauthorized)
        if status == 401 or status == "401" or str(status) == "401":
            return "unauthorized"

        # Любой признак успеха: "actual", 200, success=True
        if status == "actual" or status == 200 or success is True:
            return "ok"

        # Всё остальное — токен недействителен
        return "invalid"

    except Exception as e:
        print(f"[Heartbeat {now}] Error: {e}")
        return None


def download_content(videos, media_dir, fallback_duration):
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

    playlist = []
    for v in videos:
        v_id = v['id']
        v_url = v['url']
        file_type = v.get('file_type', 'video')
        playback = v.get('playback') or {}
        duration_seconds = normalize_duration(
            playback.get("duration_seconds"),
            fallback_duration
        )
        ext = os.path.splitext(urlparse(v_url).path)[1].lower() or ".mp4"
        if file_type == "pdf":
            ext = ".pdf"
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
        if file_type == 'pdf':
            page_paths = convert_pdf_to_images(target_path, v_id, media_dir)
            page_durations_raw = playback.get("pdf_page_durations")
            if not isinstance(page_durations_raw, list):
                page_durations_raw = []
            page_durations = [
                normalize_duration(d, fallback_duration) for d in page_durations_raw
            ]
            playlist.append({
                "id": v_id,
                "file_type": file_type,
                "paths": page_paths,
                "duration_seconds": duration_seconds,
                "page_durations": page_durations,
            })
        else:
            playlist.append({
                "id": v_id,
                "file_type": file_type,
                "paths": [target_path],
                "duration_seconds": duration_seconds,
                "page_durations": [],
            })
    save_playlist_state(media_dir, playlist)
    return playlist


class PlaybackManager:
    def __init__(self, media_dir, default_duration, on_transition_start=None, on_transition_end=None):
        self.media_dir = media_dir
        self.default_duration = int(default_duration)
        self.playlist = []
        self.thread = None
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.current_process = None
        self.on_transition_start = on_transition_start
        self.on_transition_end = on_transition_end
        self.transition_curtain_visible = False

    def is_running(self):
        return self.thread is not None and self.thread.is_alive()

    def update_default_duration(self, duration):
        self.default_duration = int(duration)

    def set_playlist(self, playlist):
        with self.lock:
            self.playlist = playlist or []

    def start(self):
        if self.is_running():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._worker_loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self._show_transition_curtain()
        self._terminate_current_process()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=3)
        self.thread = None
        self._hide_transition_curtain()

    def _show_transition_curtain(self):
        if self.transition_curtain_visible:
            return
        if self.on_transition_start:
            self.on_transition_start()
        self.transition_curtain_visible = True

    def _hide_transition_curtain(self):
        if not self.transition_curtain_visible:
            return
        if self.on_transition_end:
            self.on_transition_end()
        self.transition_curtain_visible = False

    def _terminate_current_process(self):
        with self.lock:
            process = self.current_process
        if not process:
            return
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            print(f"[Player {now}] Процесс плеера остановлен.")
        except Exception as e:
            print(f"[Player {now}] Ошибка остановки плеера: {e}")
        finally:
            with self.lock:
                self.current_process = None

    def _play_single_path(self, path, duration_seconds):
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        self._show_transition_curtain()
        cmd = [
            "mpv",
            "--fs",
            "--no-osc",
            "--no-audio",
            "--loop-file=inf",
            f"--image-display-duration={duration_seconds}",
            path,
        ]
        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                preexec_fn=os.setsid
            )
        except Exception as e:
            print(f"[Player {now}] Ошибка запуска mpv для {path}: {e}")
            return

        with self.lock:
            self.current_process = process
        # Даем mpv занять fullscreen и только потом скрываем шторку.
        time.sleep(0.15)
        self._hide_transition_curtain()

        deadline = time.time() + duration_seconds
        while not self.stop_event.is_set() and time.time() < deadline:
            if process.poll() is not None:
                break
            time.sleep(0.2)

        if process.poll() is None:
            try:
                self._show_transition_curtain()
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            except Exception:
                pass

        with self.lock:
            self.current_process = None

    def _worker_loop(self):
        while not self.stop_event.is_set():
            with self.lock:
                playlist_snapshot = list(self.playlist)

            if not playlist_snapshot:
                time.sleep(0.5)
                continue

            for item in playlist_snapshot:
                if self.stop_event.is_set():
                    break

                base_duration = normalize_duration(
                    item.get("duration_seconds"),
                    self.default_duration
                )
                file_type = item.get("file_type")
                paths = item.get("paths", [])
                if not paths:
                    continue

                if file_type == "pdf":
                    page_durations = item.get("page_durations", [])
                    for idx, page_path in enumerate(paths):
                        if self.stop_event.is_set():
                            break
                        if idx < len(page_durations):
                            page_duration = normalize_duration(
                                page_durations[idx],
                                self.default_duration
                            )
                        else:
                            page_duration = base_duration
                        self._play_single_path(page_path, page_duration)
                else:
                    self._play_single_path(paths[0], base_duration)


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
        self.default_duration = self.config.get('image_display_duration', 5)
        self.playback_manager = PlaybackManager(
            self.config['media_dir'],
            self.default_duration,
            on_transition_start=lambda: self.root.after(0, self.show_curtain),
            on_transition_end=lambda: self.root.after(0, self.hide_curtain),
        )
        self.playback_manager.set_playlist(
            load_playlist_state(self.config['media_dir'])
        )

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
            self.playback_manager.stop()
            self.root.after(0, self.show_curtain)

    def handle_unblocked(self):
        if self.is_blocked:
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            print(f"[* {now}] Устройство разблокировано. Запуск воспроизведения.")
            self.is_blocked = False
            self.playback_manager.update_default_duration(
                self.config.get('image_display_duration', 5)
            )
            self.playback_manager.start()
            time.sleep(2)
            self.root.after(0, self.hide_curtain)

    def shutdown(self, *_):
        self.playback_manager.stop()
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
                elif status == "unauthorized":
                    # Останавливаем воспроизведение и пробуем обновить токен
                    self.handle_blocked()
                    sync_token(self.config)
                elif status == "ok":
                    self.handle_unblocked()
                elif status == "invalid":
                    self.handle_blocked()
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
                self.playback_manager.stop()
                playlist = download_content(
                    data.get("videos", []),
                    self.config['media_dir'],
                    self.config.get('image_display_duration', 5)
                )
                self.playback_manager.set_playlist(playlist)
                self.playback_manager.start()
                time.sleep(3)
                self.root.after(0, self.hide_curtain)

            elif status == 204:
                self.playback_manager.update_default_duration(
                    self.config.get('image_display_duration', 5)
                )
                if not self.playback_manager.is_running():
                    self.playback_manager.set_playlist(
                        load_playlist_state(self.config['media_dir'])
                    )
                    self.playback_manager.start()

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