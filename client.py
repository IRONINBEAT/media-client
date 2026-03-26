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
playback_stop_event = threading.Event()


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
    
    # Сигналим всем потокам остановки
    playback_stop_event.set()
    
    if player_process:
        try:
            os.killpg(os.getpgid(player_process.pid), signal.SIGTERM)
            player_process = None
            print(f"[Player {now}] Предыдущий процесс mpv остановлен.")
        except Exception as e:
            print(f"[Player {now}] Ошибка остановки mpv: {e}")


def start_player(media_dir, image_display_duration=5):
    global player_process
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # Сначала гарантированно останавливаем всё старое
    stop_player()
    playback_stop_event.clear()

    def _sequential_playback():
        global player_process
        print(f"[Player {now}] Запущен последовательный плеер (скрытый режим)")

        # Загружаем длительности один раз
        durations = {}
        durations_path = os.path.join(media_dir, DURATIONS_FILE)
        if os.path.exists(durations_path):
            try:
                with open(durations_path, 'r') as f:
                    durations = json.load(f)
                print(f"[Player {now}] Загружено {len(durations)} записей длительностей")
            except Exception as e:
                print(f"[Player {now}] Ошибка чтения .durations.json: {e}")

        # Собираем все файлы
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

        while not playback_stop_event.is_set():
            for filepath in all_files:
                if playback_stop_event.is_set():
                    break

                base = os.path.basename(filepath)
                dur = durations.get(base)
                ext = os.path.splitext(filepath)[1].lower()

                # === УЛУЧШЕННАЯ КОМАНДА MPV ===
                cmd = [
                    "mpv",
                    "--fs",                    # fullscreen
                    "--no-osc",                # без интерфейса
                    "--no-audio",
                    "--really-quiet",          # минимум логов
                    "--force-window=immediate",# сразу создаём окно
                    "--wid=0",                 # без родительского окна (важно!)
                    "--input-conf=/dev/null",  # отключить все горячие клавиши
                    "--cursor-autohide=0",     # не прятать курсор
                ]

                # Длительность
                if dur is not None:
                    if ext in ('.mp4', '.avi', '.mov', '.mkv', '.webm'):
                        cmd.extend([f'--length={float(dur)}', filepath])
                    else:
                        cmd.extend([f'--image-display-duration={float(dur)}', filepath])
                else:
                    if ext in ('.png', '.jpg', '.jpeg'):
                        cmd.extend([f'--image-display-duration={float(image_display_duration)}', filepath])
                    else:
                        cmd.append(filepath)

                print(f"[Player {now}] → {base}  длительность: {dur or image_display_duration} сек.")

                try:
                    # Запуск без создания окна терминала (самое важное для macOS)
                    startupinfo = None
                    if os.name == 'nt':  # Windows
                        startupinfo = subprocess.STARTUPINFO()
                        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                        startupinfo.wShowWindow = subprocess.SW_HIDE

                    player_process = subprocess.Popen(
                        cmd,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        preexec_fn=os.setsid if os.name != 'nt' else None,
                        startupinfo=startupinfo
                    )

                    # Ждём завершения файла
                    while player_process.poll() is None:
                        if playback_stop_event.is_set():
                            try:
                                os.killpg(os.getpgid(player_process.pid), signal.SIGTERM)
                            except:
                                pass
                            break
                        time.sleep(0.15)

                    player_process = None

                except Exception as e:
                    print(f"[Player {now}] Ошибка запуска mpv для {base}: {e}")
                    player_process = None
                    time.sleep(0.5)

    # Запускаем в отдельном потоке
    threading.Thread(target=_sequential_playback, daemon=True).start()
    print(f"[Player {now}] Последовательный плеер запущен в скрытом режиме")


def load_config():
    """Загружает и валидирует конфигурацию клиента"""
    if not os.path.exists(CONFIG_FILE):
        raise FileNotFoundError(f"Файл конфигурации не найден: {CONFIG_FILE}")
    
    with open(CONFIG_FILE, 'r') as f:
        config = json.load(f)

    # === ВАЛИДАЦИЯ ИНТЕРВАЛОВ ===
    defaults = {
        "heartbeat_interval": 30,      # секунд
        "check_videos_interval": 180,  # секунд (рекомендую 3 минуты)
        "image_display_duration": 5,
        "server_url": "http://217.71.129.139:4085",
        "device_id": "NSTU_OrangePI2302",
        "media_dir": "./content",
        "token": ""
    }

    for key, default_value in defaults.items():
        if key not in config or not isinstance(config[key], (int, float, str)):
            print(f"[Config] Предупреждение: ключ '{key}' отсутствует или некорректен. Используем значение по умолчанию: {default_value}")
            config[key] = default_value

    # Ограничения на интервалы
    config["heartbeat_interval"] = max(15, min(120, int(config["heartbeat_interval"])))      # 15..120 сек
    config["check_videos_interval"] = max(60, min(600, int(config["check_videos_interval"]))) # 1..10 минут

    # Принудительно приводим media_dir к абсолютному пути
    if not os.path.isabs(config["media_dir"]):
        config["media_dir"] = os.path.abspath(config["media_dir"])

    print(f"[Config] Загружены интервалы:")
    print(f"   Heartbeat          → {config['heartbeat_interval']} сек")
    print(f"   Check videos       → {config['check_videos_interval']} сек")
    print(f"   Изображения по умолчанию → {config.get('image_display_duration', 5)} сек")
    print(f"   Media dir          → {config['media_dir']}")

    return config


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
            # 1. Извлекаем список длительностей для страниц из конфига видео
            page_durs = None
            if dur_config and isinstance(dur_config, dict):
                page_durs = dur_config.get('pages') # Ожидаем список [5, 20, 1]

            # 2. Конвертируем PDF. Функция вернет список кортежей (путь, dur_из_списка)
            pages = convert_pdf_to_images(target_path, v_id, media_dir, page_durs)
            
            for png_path, dur in pages:
                # 3. Если для конкретной страницы длительность НЕ задана в page_durs, 
                # только тогда берем общую длительность из dur_config['duration'] 
                # или, в крайнем случае, дефолт системы.
                if dur is None:
                    dur = dur_config.get('duration') if isinstance(dur_config, dict) else None
                
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
        
        self.root.withdraw()                    # главное окно скрыто
        self.root.overrideredirect(True)        # убираем рамку окна
        self.root.attributes('-alpha', 0.0)    
        
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
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        # Первая проверка контента сразу после старта
        print(f"[{now_str}] Начальная проверка контента после запуска...")
        self.process_check_videos(now_str)
        self.last_check = time.time()
        self.last_hb = time.time()

        print(f"[Info] Клиент запущен. Heartbeat: {self.config['heartbeat_interval']}с, Check: {self.config['check_videos_interval']}с")

        while True:
            now_ts = time.time()
            now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

            # === HEARTBEAT ===
            if now_ts - self.last_hb >= self.config['heartbeat_interval']:
                status = heartbeat(self.config)
                self.last_hb = now_ts

                if status == "blocked":
                    self.handle_blocked()
                elif status in ("unauthorized", "invalid"):
                    self.handle_blocked()
                    sync_token(self.config)
                elif status == "ok":
                    self.handle_unblocked()

            # === CHECK VIDEOS ===
            if (not self.is_blocked and 
                now_ts - self.last_check >= self.config['check_videos_interval']):
                
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