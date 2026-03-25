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

# Пути всегда относительно директории скрипта
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(_SCRIPT_DIR, 'config.json')

# Паттерн страниц PDF: {file_id}_p-001.png
PDF_PAGE_RE = re.compile(r'^(.+)_p-\d+\.png$')

# Глобальное состояние плеера
player_process = None   # текущий subprocess mpv
player_running = False  # флаг для остановки цикла плеера
player_thread = None    # поток цикла плеера


# ============================================================
# Утилиты
# ============================================================

def resolve_media_dir(raw_path: str) -> str:
    """Превращает ./content в абсолютный путь относительно скрипта."""
    if os.path.isabs(raw_path):
        return raw_path
    return os.path.normpath(os.path.join(_SCRIPT_DIR, raw_path))


def load_config():
    with open(CONFIG_FILE, 'r') as f:
        cfg = json.load(f)
    cfg['media_dir'] = resolve_media_dir(cfg.get('media_dir', './content'))
    return cfg


def save_config(config):
    with open(CONFIG_FILE, 'w') as f:
        # Сохраняем media_dir как относительный путь обратно
        cfg_copy = dict(config)
        cfg_copy['media_dir'] = os.path.relpath(config['media_dir'], _SCRIPT_DIR)
        json.dump(cfg_copy, f, indent=4)


# ============================================================
# Плейлист
# ============================================================

PLAYLIST_FILE_NAME = 'playlist.json'


def save_playlist(media_dir: str, playlist: list):
    """Сохраняет упорядоченный плейлист с длительностями рядом с контентом."""
    path = os.path.join(media_dir, PLAYLIST_FILE_NAME)
    with open(path, 'w') as f:
        json.dump(playlist, f, indent=2)


def load_playlist(media_dir: str) -> list:
    """Загружает плейлист. Возвращает [] если файла нет."""
    path = os.path.join(media_dir, PLAYLIST_FILE_NAME)
    if not os.path.exists(path):
        return []
    with open(path, 'r') as f:
        return json.load(f)


def build_playlist_from_server(videos: list, media_dir: str) -> list:
    """Строит плейлист из ответа сервера (status=205).

    Каждый элемент: {"path": "/abs/path/to/file", "duration": N_или_None}
      duration=None  — для видео (играть до конца)
      duration=N     — секунды показа для фото и страниц PDF
    """
    DEFAULT_IMAGE_DURATION = 5
    playlist = []

    for v in videos:
        file_id = v['id']
        file_type = v.get('file_type', 'video')
        playback = v.get('playback') or {}
        duration_seconds = playback.get('duration_seconds')
        pdf_page_durations = playback.get('pdf_page_durations') or []

        if file_type == 'pdf':
            # Страницы PDF уже сконвертированы в PNG: {file_id}_p-001.png ...
            pages = sorted(glob.glob(
                os.path.join(media_dir, f"{file_id}_p-*.png")
            ))
            if pdf_page_durations:
                # Индивидуальная длительность каждой страницы
                page_dur_map = {
                    item['page']: item['duration']
                    for item in pdf_page_durations
                    if isinstance(item.get('duration'), (int, float))
                }
                for i, page_path in enumerate(pages):
                    dur = page_dur_map.get(i + 1, DEFAULT_IMAGE_DURATION)
                    playlist.append({"path": page_path, "duration": dur})
            else:
                dur = duration_seconds or DEFAULT_IMAGE_DURATION
                for page_path in pages:
                    playlist.append({"path": page_path, "duration": dur})

        elif file_type == 'image':
            for ext in ('.png', '.jpg', '.jpeg'):
                candidate = os.path.join(media_dir, f"{file_id}{ext}")
                if os.path.exists(candidate):
                    dur = duration_seconds or DEFAULT_IMAGE_DURATION
                    playlist.append({"path": candidate, "duration": dur})
                    break

        else:  # video
            for ext in ('.mp4', '.avi', '.mkv', '.mov'):
                candidate = os.path.join(media_dir, f"{file_id}{ext}")
                if os.path.exists(candidate):
                    # duration=None означает «играть до конца файла»
                    playlist.append({"path": candidate, "duration": None})
                    break

    return playlist


# ============================================================
# Плеер
# ============================================================

def _player_loop(playlist: list, show_cb, hide_cb):
    """Фоновый поток: бесконечно крутит плейлист.

    show_cb / hide_cb — колбэки App для показа/скрытия чёрного экрана.
    Чёрный экран поднимается перед каждым файлом и убирается
    как только mpv успел открыться — переход без мигания рабочего стола.
    """
    global player_process, player_running

    while player_running:
        if not playlist:
            time.sleep(1)
            continue

        for item in playlist:
            if not player_running:
                break

            path = item['path']
            duration = item['duration']

            if not os.path.exists(path):
                continue

            cmd = ["mpv", "--fs", "--no-osc", "--no-audio"]
            if duration is not None:
                cmd += [f"--image-display-duration={duration}", "--no-loop-file"]
            cmd.append(path)

            try:
                # Показываем чёрный экран перед стартом следующего файла
                show_cb()

                player_process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    preexec_fn=os.setsid
                )

                # Даём mpv ~0.3 сек чтобы открыть окно, затем убираем шторку
                time.sleep(0.3)
                hide_cb()

                player_process.wait()
                player_process = None

            except Exception as e:
                now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                print(f"[Player {now}] Ошибка mpv: {e}")


def start_player(media_dir: str, show_cb=None, hide_cb=None):
    """Запускает цикл воспроизведения плейлиста из media_dir."""
    global player_running, player_thread

    playlist = load_playlist(media_dir)
    if not playlist:
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        print(f"[Player {now}] Плейлист пуст, нечего воспроизводить.")
        return

    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[Player {now}] Запуск воспроизведения, {len(playlist)} позиций в плейлисте.")

    # Заглушки если колбэки не переданы
    _show = show_cb or (lambda: None)
    _hide = hide_cb or (lambda: None)

    player_running = True
    player_thread = threading.Thread(
        target=_player_loop, args=(playlist, _show, _hide), daemon=True
    )
    player_thread.start()


def stop_player():
    """Останавливает цикл воспроизведения и текущий процесс mpv."""
    global player_process, player_running, player_thread

    player_running = False
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    if player_process:
        try:
            os.killpg(os.getpgid(player_process.pid), signal.SIGTERM)
            player_process = None
            print(f"[Player {now}] Процесс плеера остановлен.")
        except Exception as e:
            print(f"[Player {now}] Ошибка при остановке плеера: {e}")

    if player_thread and player_thread.is_alive():
        player_thread.join(timeout=3)
        player_thread = None


# ============================================================
# Работа с файлами
# ============================================================

def get_local_file_ids(media_dir: str) -> list:
    """Возвращает список file_id из media_dir.

    Страницы PDF ({file_id}_p-001.png) схлопываются обратно в исходный file_id.
    playlist.json пропускается.
    """
    if not os.path.exists(media_dir):
        os.makedirs(media_dir)
        return []

    ids = set()
    for filename in os.listdir(media_dir):
        if filename == PLAYLIST_FILE_NAME:
            continue
        if not os.path.isfile(os.path.join(media_dir, filename)):
            continue
        m = PDF_PAGE_RE.match(filename)
        if m:
            ids.add(m.group(1))
        else:
            ids.add(os.path.splitext(filename)[0])
    return list(ids)


def convert_pdf_to_images(pdf_path: str, file_id: str, media_dir: str):
    """Конвертирует PDF в PNG-страницы через pdftoppm (poppler-utils)."""
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


def download_content(videos: list, media_dir: str):
    """Очищает папку, скачивает файлы и сохраняет playlist.json."""
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    print(f"[* {now}] Очистка локального контента...")
    if os.path.exists(media_dir):
        for fname in os.listdir(media_dir):
            fpath = os.path.join(media_dir, fname)
            try:
                if os.path.isfile(fpath):
                    os.unlink(fpath)
            except Exception as e:
                print(f"[! {now}] Ошибка удаления {fpath}: {e}")
    else:
        os.makedirs(media_dir)

    # Скачиваем файлы
    for v in videos:
        v_id = v['id']
        v_url = v['url']
        ext = os.path.splitext(urlparse(v_url).path)[1].lower()
        target_path = os.path.join(media_dir, f"{v_id}{ext}")

        print(f"[* {now}] Загрузка {v_id} ({ext})")
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

    # Строим и сохраняем плейлист с длительностями
    playlist = build_playlist_from_server(videos, media_dir)
    save_playlist(media_dir, playlist)
    print(f"[* {now}] Плейлист сохранён: {len(playlist)} позиций.")


# ============================================================
# Токены
# ============================================================

def sync_token(config: dict) -> bool:
    url = f"{config['server_url']}/api/sync-token"
    payload = {"token": config['token'], "id": config['device_id']}
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    try:
        response = requests.post(url, json=payload, timeout=10)
        data = response.json()
        if data.get("success") and data.get("status") == "updated":
            config['token'] = data['new_token']
            save_config(config)
            print(f"[* {now}] Токен обновлён: {config['token']}")
            return True
        return False
    except Exception as e:
        print(f"[! {now}] Ошибка синхронизации токена: {e}")
        return False


def heartbeat(config: dict):
    """Возвращает нормализованный статус:
      "ok"           — устройство активно
      "blocked"      — устройство заблокировано (403)
      "unauthorized" — устройство не подтверждено (401)
      "invalid"      — токен не найден
      None           — сервер недоступен
    """
    url = f"{config['server_url']}/api/heartbeat"
    payload = {"token": config['token'], "id": config['device_id']}
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    try:
        resp = requests.post(url, json=payload, timeout=10)
        data = resp.json()
        status = data.get("status")
        success = data.get("success") if "success" in data else data.get("answer")
        message = data.get("message", "")
        print(f"[Heartbeat {now}] status={status} msg={message}")

        if status == "updated":
            new_token = data.get("new_token")
            if new_token:
                config['token'] = new_token
                save_config(config)
                print(f"[* {now}] Токен обновлён через heartbeat: {new_token}")
            return "ok"

        if status == 403 or str(status) == "403":
            return "blocked"

        if status == 401 or str(status) == "401":
            return "unauthorized"

        if status == "actual" or status == 200 or success is True:
            return "ok"

        return "invalid"

    except Exception as e:
        print(f"[Heartbeat {now}] Error: {e}")
        return None


# ============================================================
# Приложение
# ============================================================

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

        # Колбэки для _player_loop — вызываются из фонового потока через after()
        self._show_cb = lambda: self.root.after(0, self.show_curtain)
        self._hide_cb = lambda: self.root.after(0, self.hide_curtain)

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
            start_player(self.config['media_dir'], self._show_cb, self._hide_cb)
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
                elif status in ("unauthorized", "invalid"):
                    self.handle_blocked()
                    sync_token(self.config)
                elif status == "ok":
                    self.handle_unblocked()
                # None — сервер недоступен, ждём

            if not self.is_blocked and now_ts - self.last_check > self.config.get('check_videos_interval', 60):
                self.process_check_videos(now_str)
                self.last_check = now_ts

            time.sleep(1)

    def process_check_videos(self, now_str: str):
        url = f"{self.config['server_url']}/api/check-videos"
        current_ids = get_local_file_ids(self.config['media_dir'])
        payload = {
            "token": self.config['token'],
            "id": self.config['device_id'],
            "videos": current_ids,
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
                start_player(self.config['media_dir'], self._show_cb, self._hide_cb)
                time.sleep(3)
                self.root.after(0, self.hide_curtain)

            elif status == 204:
                global player_running
                if not player_running:
                    start_player(self.config['media_dir'], self._show_cb, self._hide_cb)

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