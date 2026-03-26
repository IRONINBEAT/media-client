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
DURATIONS_FILE = ".durations.json"
PDF_PAGE_RE = re.compile(r'^(.+)_p-(\d+)\.png$')

player_process = None


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


def start_player(media_dir, image_display_duration=5):
    global player_process
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # Список всех файлов в папке, исключая служебные
    all_files = []
    for f in os.listdir(media_dir):
        if f == DURATIONS_FILE:
            continue
        full = os.path.join(media_dir, f)
        if os.path.isfile(full):
            all_files.append(full)
    all_files.sort()

    if not all_files:
        print(f"[Player {now}] Нет файлов для воспроизведения.")
        return

    # Загружаем длительности, если есть
    durations = {}
    durations_path = os.path.join(media_dir, DURATIONS_FILE)
    if os.path.exists(durations_path):
        try:
            with open(durations_path, 'r') as f:
                durations = json.load(f)
            print(f"[Player {now}] Загружены длительности для {len(durations)} файлов.")
        except Exception as e:
            print(f"[Player {now}] Ошибка загрузки длительностей: {e}")

    # Формируем команду mpv
    cmd = ["mpv", "--fs", "--loop-playlist", "--no-osc", "--no-audio", "--vo=gpu"]
    for filepath in all_files:
        base = os.path.basename(filepath)
        dur = durations.get(base)
        ext = os.path.splitext(filepath)[1].lower()

        if dur is not None:
            # Если длительность задана, применяем опцию в зависимости от типа файла
            if ext in ('.mp4', '.avi', '.mov', '.mkv', '.webm'):
                cmd.extend(['--length', str(dur)])
            else:
                # Изображения, png, jpg и т.д.
                cmd.extend(['--image-display-duration', str(dur)])
        else:
            # Нет данных о длительности – используем старую логику
            if ext in ('.png', '.jpg', '.jpeg'):
                # Для изображений применяем глобальную настройку из конфига
                # Чтобы она действовала только на этот файл, нужно указать перед ним
                cmd.extend(['--image-display-duration', str(image_display_duration)])
            # Для видео без ограничения – не добавляем опций
        cmd.append(filepath)

    print(f"[Player {now}] Команда: {' '.join(cmd)}")
    print(f"[Player {now}] DISPLAY: {os.environ.get('DISPLAY')}")
    
    print(f"[Player {now}] Запуск воспроизведения {len(all_files)} файлов.")
    try:
        player_process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            preexec_fn=os.setsid
        )
        print(f"[Player {now}] Запущен процесс {player_process.pid}")
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


def convert_pdf_to_images(pdf_path, file_id, media_dir, page_durations=None):
    """
    Конвертирует PDF в PNG и возвращает список (путь к PNG, длительность) для каждой страницы.
    page_durations: список длительностей страниц, если задан.
    """
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    prefix = os.path.join(media_dir, f"{file_id}_p")
    try:
        subprocess.run(
            ['pdftoppm', '-r', '150', '-png', pdf_path, prefix],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        pages = sorted(glob.glob(f"{prefix}-*.png"))
        print(f"[* {now}] PDF {file_id} → {len(pages)} стр.")

        result = []
        for i, png_path in enumerate(pages):
            dur = page_durations[i] if page_durations and i < len(page_durations) else None
            result.append((png_path, dur))
        return result
    except FileNotFoundError:
        print(f"[! {now}] pdftoppm не найден. Установите: sudo apt install poppler-utils")
    except subprocess.CalledProcessError as e:
        print(f"[! {now}] Ошибка конвертации PDF {file_id}: {e}")
    finally:
        try:
            os.remove(pdf_path)
        except OSError:
            pass
    return []


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


def download_content(videos, media_dir, default_image_duration):
    """
    Загружает контент из списка videos, обрабатывает PDF и возвращает словарь
    {имя_файла: длительность} для всех файлов, которые будут воспроизводиться.
    """
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

    durations = {}

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

        dur_config = v.get('duration_config')
        if ext == '.pdf':
            # Обработка PDF
            if dur_config and isinstance(dur_config, dict) and 'pages' in dur_config:
                page_durs = dur_config['pages']
            else:
                page_durs = None
            pages = convert_pdf_to_images(target_path, v_id, media_dir, page_durs)
            for png_path, dur in pages:
                if dur is None:
                    dur = default_image_duration
                durations[os.path.basename(png_path)] = dur
        else:
            # Видео или изображение
            if dur_config and isinstance(dur_config, dict):
                dur = dur_config.get('duration')
            else:
                dur = None
            # Для изображений, если длительность не указана, подставляем значение по умолчанию
            if ext in ('.png', '.jpg', '.jpeg') and dur is None:
                dur = default_image_duration
            durations[target_filename] = dur

    # Сохраняем длительности в файл
    durations_path = os.path.join(media_dir, DURATIONS_FILE)
    try:
        with open(durations_path, 'w') as f:
            json.dump(durations, f)
        print(f"[* {now}] Длительности сохранены в {durations_path}")
    except Exception as e:
        print(f"[! {now}] Ошибка сохранения длительностей: {e}")

    return durations


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
                image_display_duration=self.config.get('image_display_duration', 5)
            )
            time.sleep(2)
            self.root.after(0, self.hide_curtain)

    def shutdown(self, *_):
        stop_player()
        self.root.after(0, self.root.destroy)

    def worker_loop(self):
        # Принудительно проверим контент сразу после старта
        self.process_check_videos(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        self.last_check = time.time()

        while True:
            now_ts = time.time()
            now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

            if now_ts - self.last_hb > self.config.get('heartbeat_interval', 30):
                status = heartbeat(self.config)
                self.last_hb = now_ts

                if status == "blocked":
                    self.handle_blocked()
                elif status == "unauthorized":
                    self.handle_blocked()
                    sync_token(self.config)
                elif status == "ok":
                    self.handle_unblocked()
                elif status == "invalid":
                    self.handle_blocked()
                    sync_token(self.config)
                # None — сервер недоступен, ждём следующего heartbeat

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
                download_content(
                    data.get("videos", []),
                    self.config['media_dir'],
                    self.config.get('image_display_duration', 5)
                )
                start_player(
                    self.config['media_dir'],
                    image_display_duration=self.config.get('image_display_duration', 5)
                )
                time.sleep(3)
                self.root.after(0, self.hide_curtain)

            elif status == 204:
                global player_process
                if player_process is None or player_process.poll() is not None:
                    start_player(
                        self.config['media_dir'],
                        image_display_duration=self.config.get('image_display_duration', 5)
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