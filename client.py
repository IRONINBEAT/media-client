import os
import re
import json
import time
import glob
import subprocess
import requests
import threading
from datetime import datetime
import signal
from urllib.parse import urlparse

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')
DURATIONS_FILE = ".durations.json"
PDF_PAGE_RE = re.compile(r'^(.+)_p-(\d+)\.png$')

player_process = None


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

    # Проверяем существование папки
    if not os.path.exists(media_dir):
        print(f"[Player {now}] Папка {media_dir} не существует")
        return

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

    # Загружаем длительности
    durations = {}
    durations_path = os.path.join(media_dir, DURATIONS_FILE)
    if os.path.exists(durations_path):
        try:
            with open(durations_path, 'r') as f:
                durations = json.load(f)
            print(f"[Player {now}] Загружены длительности для {len(durations)} файлов.")
        except Exception as e:
            print(f"[Player {now}] Ошибка загрузки длительностей: {e}")

    # Создаем временный файл плейлиста с командами
    playlist_file = os.path.join(media_dir, "_playlist.txt")
    with open(playlist_file, 'w') as f:
        for filepath in all_files:
            base = os.path.basename(filepath)
            dur = durations.get(base)
            ext = os.path.splitext(filepath)[1].lower()
            
            if dur is not None and dur > 0:
                if ext in ('.mp4', '.avi', '.mov', '.mkv', '.webm', '.m4v'):
                    # Для видео используем опцию length
                    f.write(f"{filepath}\n")
                    f.write(f"set playback-time {dur}\n")
                else:
                    # Для изображений используем image-display-duration
                    f.write(f"{filepath}\n")
                    f.write(f"set image-display-duration {dur}\n")
            else:
                f.write(f"{filepath}\n")
                if ext in ('.png', '.jpg', '.jpeg', '.gif', '.bmp'):
                    f.write(f"set image-display-duration {image_display_duration}\n")
    
    # Запускаем mpv с плейлистом
    cmd = [
        "mpv", "--fs", "--loop-playlist", "--no-osc", "--no-audio",
        f"--vo={os.environ.get('MPV_VO', 'x11')}",
        f"--playlist={playlist_file}"
    ]
    
    print(f"[Player {now}] Команда: {' '.join(cmd)}")
    print(f"[Player {now}] Плейлист создан: {playlist_file}")
    
    try:
        env = os.environ.copy()
        if 'DISPLAY' not in env:
            env['DISPLAY'] = ':0.0'
            
        player_process = subprocess.Popen(
            cmd,
            stdout=None,
            stderr=None,
            preexec_fn=os.setsid,
            env=env
        )
        print(f"[Player {now}] Запущен процесс {player_process.pid}")
        
        time.sleep(2)
        if player_process.poll() is not None:
            print(f"[Player {now}] Плеер завершился с кодом {player_process.returncode}")
            
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
    """Отправляет пинг серверу и возвращает нормализованный статус"""
    url = f"{config['server_url']}/api/heartbeat"
    payload = {"token": config['token'], "id": config['device_id']}
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    try:
        resp = requests.post(url, json=payload, timeout=10)
        data = resp.json()
        status = data.get("status")
        success = data.get("success")
        print(f"[Heartbeat {now}] status={status} success={success}")

        if status == "updated":
            new_token = data.get("new_token")
            if new_token:
                config['token'] = new_token
                save_config(config)
                print(f"[* {now}] Токен обновлён через heartbeat: {new_token}")
            return "ok"
        elif status == 403 or str(status) == "403":
            return "blocked"
        elif status == 401 or str(status) == "401":
            return "unauthorized"
        elif status == "actual" or status == 200 or success is True:
            return "ok"
        else:
            return "invalid"
    except Exception as e:
        print(f"[Heartbeat {now}] Error: {e}")
        return None


def download_content(videos, media_dir, default_image_duration):
    """Загружает контент и возвращает словарь длительностей"""
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
                check=True, 
                stdout=subprocess.DEVNULL, 
                stderr=subprocess.DEVNULL
            )
        except subprocess.CalledProcessError as e:
            print(f"[! {now}] Ошибка скачивания {v_id}: {e}")
            continue

        dur_config = v.get('duration_config')
        if ext == '.pdf':
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
            if dur_config and isinstance(dur_config, dict):
                dur = dur_config.get('duration')
            else:
                dur = None
            if ext in ('.png', '.jpg', '.jpeg') and dur is None:
                dur = default_image_duration
            durations[target_filename] = dur

    # Сохраняем длительности
    durations_path = os.path.join(media_dir, DURATIONS_FILE)
    try:
        with open(durations_path, 'w') as f:
            json.dump(durations, f)
        print(f"[* {now}] Длительности сохранены в {durations_path}")
    except Exception as e:
        print(f"[! {now}] Ошибка сохранения длительностей: {e}")

    return durations


class SimpleClient:
    def __init__(self):
        self.config = load_config()
        self.last_hb = 0
        self.last_check = 0
        self.is_blocked = False
        self.running = True
        
        # Проверяем DISPLAY
        display = os.environ.get('DISPLAY')
        if not display:
            print("[!] Внимание: переменная DISPLAY не установлена. Плеер может не работать.")
            print("[!] Установите: export DISPLAY=:0.0")

    def handle_blocked(self):
        if not self.is_blocked:
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            print(f"[! {now}] Устройство заблокировано. Остановка воспроизведения.")
            self.is_blocked = True
            stop_player()

    def handle_unblocked(self):
        if self.is_blocked:
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            print(f"[* {now}] Устройство разблокировано. Запуск воспроизведения.")
            self.is_blocked = False
            start_player(
                self.config['media_dir'],
                image_display_duration=self.config.get('image_display_duration', 5)
            )

    def shutdown(self, signum=None, frame=None):
        print("\n[*] Завершение работы...")
        self.running = False
        stop_player()
        if signum:
            print(f"[*] Получен сигнал {signum}")
        exit(0)

    def worker_loop(self):
        # Проверяем контент сразу после старта
        self.process_check_videos()
        self.last_check = time.time()

        while self.running:
            now_ts = time.time()
            now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

            # Heartbeat
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

            # Check videos
            if not self.is_blocked and now_ts - self.last_check > self.config.get('check_videos_interval', 60):
                self.process_check_videos()
                self.last_check = now_ts

            time.sleep(1)

    def process_check_videos(self):
        url = f"{self.config['server_url']}/api/check-videos"
        current_ids = get_local_file_ids(self.config['media_dir'])
        payload = {
            "token": self.config['token'],
            "id": self.config['device_id'],
            "videos": current_ids
        }

        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        try:
            resp = requests.post(url, json=payload, timeout=10)
            data = resp.json()
            status = data.get("status")

            if status == 205:
                print(f"[{now_str}] Обновление контента...")
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
        # Устанавливаем обработчики сигналов
        signal.signal(signal.SIGINT, self.shutdown)
        signal.signal(signal.SIGTERM, self.shutdown)
        
        print("[*] Запуск клиента...")
        print(f"[*] Media directory: {self.config['media_dir']}")
        print(f"[*] Server: {self.config['server_url']}")
        
        # Запускаем рабочий цикл
        t = threading.Thread(target=self.worker_loop, daemon=True)
        t.start()
        
        # Держим основной поток живым
        try:
            while self.running:
                time.sleep(1)
        except KeyboardInterrupt:
            self.shutdown()


if __name__ == "__main__":
    client = SimpleClient()
    client.run()