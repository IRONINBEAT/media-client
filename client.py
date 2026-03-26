import os
import re
import json
import time
import glob
import socket
import subprocess
import tempfile
import requests
import tkinter as tk
import threading
from datetime import datetime
import signal
from urllib.parse import urlparse

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')
DURATIONS_FILE = ".durations.json"
PDF_PAGE_RE = re.compile(r'^(.+)_p-(\d+)\.png$')
VIDEO_EXTENSIONS = ('.mp4', '.avi', '.mov', '.mkv', '.webm')
IMAGE_EXTENSIONS = ('.png', '.jpg', '.jpeg')
MPV_SOCKET_TIMEOUT = 5
MPV_EVENT_POLL_INTERVAL = 0.1

player_process = None
player_thread = None
player_socket_path = None
playback_stop_event = threading.Event()
curtain_state_lock = threading.Lock()
pending_curtain_state = None


def request_curtain(visible):
    global pending_curtain_state
    with curtain_state_lock:
        pending_curtain_state = visible


def consume_curtain_request():
    global pending_curtain_state
    with curtain_state_lock:
        requested_state = pending_curtain_state
        pending_curtain_state = None
    return requested_state


def cleanup_socket(socket_path):
    if socket_path and os.path.exists(socket_path):
        try:
            os.unlink(socket_path)
        except OSError:
            pass


def load_durations(media_dir, now):
    durations = {}
    durations_path = os.path.join(media_dir, DURATIONS_FILE)
    if os.path.exists(durations_path):
        try:
            with open(durations_path, 'r') as f:
                durations = json.load(f)
            print(f"[Player {now}] Загружено {len(durations)} записей длительностей")
        except Exception as e:
            print(f"[Player {now}] Ошибка чтения .durations.json: {e}")
    return durations


def build_playlist_entries(media_dir, image_display_duration, now):
    durations = load_durations(media_dir, now)
    entries = []

    for filename in sorted(os.listdir(media_dir)):
        if filename == DURATIONS_FILE:
            continue

        filepath = os.path.join(media_dir, filename)
        if not os.path.isfile(filepath):
            continue

        duration = durations.get(filename)
        ext = os.path.splitext(filepath)[1].lower()

        if duration is not None:
            duration = float(duration)
        elif ext in IMAGE_EXTENSIONS:
            duration = float(image_display_duration)

        entries.append({
            "path": filepath,
            "base": filename,
            "ext": ext,
            "duration": duration,
        })

    return entries


def wait_for_mpv_socket(socket_path, process, timeout=MPV_SOCKET_TIMEOUT):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"mpv завершился с кодом {process.returncode} до открытия IPC-сокета")
        if os.path.exists(socket_path):
            return
        time.sleep(0.05)
    raise TimeoutError(f"mpv не создал IPC-сокет за {timeout} сек.: {socket_path}")


class MpvIpcClient:
    def __init__(self, socket_path):
        self.socket_path = socket_path
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(MPV_EVENT_POLL_INTERVAL)
        self.sock.connect(socket_path)
        self.buffer = b""
        self.request_id = 1
        self.pending_events = []

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

    def _read_line(self, timeout=None):
        previous_timeout = self.sock.gettimeout()
        if timeout is not None:
            self.sock.settimeout(timeout)

        try:
            while True:
                if b'\n' in self.buffer:
                    line, self.buffer = self.buffer.split(b'\n', 1)
                    return line.decode('utf-8', errors='replace')

                chunk = self.sock.recv(65536)
                if not chunk:
                    raise ConnectionError("mpv IPC-соединение закрыто")
                self.buffer += chunk
        except socket.timeout:
            return None
        finally:
            if timeout is not None:
                self.sock.settimeout(previous_timeout)

    def _read_message(self, timeout=None):
        line = self._read_line(timeout=timeout)
        if line is None:
            return None
        return json.loads(line)

    def send_command(self, command, timeout=MPV_SOCKET_TIMEOUT):
        current_request_id = self.request_id
        self.request_id += 1

        payload = {
            "command": command,
            "request_id": current_request_id,
        }
        message = json.dumps(payload, separators=(',', ':')).encode('utf-8') + b'\n'
        self.sock.sendall(message)

        deadline = time.monotonic() + timeout
        while True:
            remaining = max(0.1, deadline - time.monotonic())
            response = self._read_message(timeout=remaining)
            if response is None:
                raise TimeoutError(f"mpv IPC не ответил на команду {command[0]!r}")

            if response.get("request_id") == current_request_id:
                if response.get("error") != "success":
                    raise RuntimeError(
                        f"mpv IPC команда {command[0]!r} завершилась ошибкой: {response.get('error')}"
                    )
                return response.get("data")

            if "event" in response:
                self.pending_events.append(response)

    def next_event(self, timeout=MPV_EVENT_POLL_INTERVAL):
        if self.pending_events:
            return self.pending_events.pop(0)

        while True:
            response = self._read_message(timeout=timeout)
            if response is None:
                return None
            if "event" in response:
                return response

    def load_file(self, filepath, ext, duration):
        if duration is not None and ext in IMAGE_EXTENSIONS:
            self.send_command(["set_property", "image-display-duration", duration])

        self.pending_events.clear()
        self.send_command(["loadfile", filepath, "replace"])

    def wait_until_file_ends(self, process, stop_event, ext, duration):
        stop_sent = False
        playback_started_at = time.monotonic()

        while not stop_event.is_set():
            if process.poll() is not None:
                raise RuntimeError(f"mpv неожиданно завершился с кодом {process.returncode}")

            if duration is not None and ext in VIDEO_EXTENSIONS and not stop_sent:
                if time.monotonic() - playback_started_at >= duration:
                    self.send_command(["stop"])
                    stop_sent = True

            event = self.next_event(timeout=MPV_EVENT_POLL_INTERVAL)
            if event is None:
                continue

            if event.get("event") == "end-file":
                return

        raise RuntimeError("Воспроизведение остановлено")


def stop_player():
    global player_process, player_thread, player_socket_path
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

    if player_thread and player_thread.is_alive() and player_thread is not threading.current_thread():
        player_thread.join(timeout=2)
    player_thread = None

    cleanup_socket(player_socket_path)
    player_socket_path = None


def start_player(media_dir, image_display_duration=5):
    global player_process, player_thread, player_socket_path
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # Сначала гарантированно останавливаем всё старое
    stop_player()
    playback_stop_event.clear()

    def _sequential_playback():
        global player_process, player_socket_path
        print(f"[Player {now}] Запущен последовательный плеер")
        entries = build_playlist_entries(media_dir, image_display_duration, now)
        if not entries:
            print(f"[Player {now}] Нет файлов для воспроизведения.")
            return

        socket_path = os.path.join(tempfile.gettempdir(), f"media-client-mpv-{os.getpid()}.sock")
        cleanup_socket(socket_path)
        player_socket_path = socket_path

        client = None
        try:
            player_process = subprocess.Popen(
                [
                    "mpv",
                    "--fs",
                    "--force-window=yes",
                    "--idle=yes",
                    "--no-osc",
                    "--no-audio",
                    "--no-terminal",
                    f"--input-ipc-server={socket_path}",
                    "--cursor-autohide=always",
                    "--autofit-larger=100%x100%",
                    "--keep-open=no",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )

            wait_for_mpv_socket(socket_path, player_process)
            client = MpvIpcClient(socket_path)

            while not playback_stop_event.is_set():
                for entry in entries:
                    if playback_stop_event.is_set():
                        break

                    duration_text = entry["duration"] if entry["duration"] is not None else "native"
                    print(f"[Player {now}] → {entry['base']}  длительность: {duration_text} сек.")

                    try:
                        client.load_file(entry["path"], entry["ext"], entry["duration"])
                        client.wait_until_file_ends(
                            player_process,
                            playback_stop_event,
                            entry["ext"],
                            entry["duration"],
                        )
                    except RuntimeError as e:
                        if str(e) != "Воспроизведение остановлено":
                            print(f"[Player {now}] Ошибка IPC/mpv для {entry['base']}: {e}")
                        return
                    except Exception as e:
                        print(f"[Player {now}] Ошибка воспроизведения {entry['base']}: {e}")
                        return
        except Exception as e:
            print(f"[Player {now}] Ошибка запуска mpv: {e}")
        finally:
            if client:
                client.close()

            proc = player_process
            if proc and proc.poll() is None:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except Exception:
                    pass

            player_process = None
            cleanup_socket(player_socket_path)
            player_socket_path = None

    # Запускаем последовательное воспроизведение в отдельном потоке
    player_thread = threading.Thread(target=_sequential_playback, daemon=True)
    player_thread.start()
    print(f"[Player {now}] Последовательный плеер запущен")


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
            request_curtain(True)

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
            request_curtain(False)

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
                request_curtain(True)
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
                request_curtain(False)

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
            requested_state = consume_curtain_request()
            if requested_state is True:
                self.show_curtain()
            elif requested_state is False:
                self.hide_curtain()
            self.root.after(200, _poll)
        _poll()

        t = threading.Thread(target=self.worker_loop, daemon=True)
        t.start()
        self.root.mainloop()


if __name__ == "__main__":
    app = App()
    app.run()
