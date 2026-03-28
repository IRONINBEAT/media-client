import os
import re
import json
import time
import glob
import socket
import shutil
import subprocess
import sys
import tempfile
import requests
import tkinter as tk
import threading
from datetime import datetime, timedelta
import signal
from urllib.parse import urlparse

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')
DURATIONS_FILE = ".durations.json"
PDF_PAGE_RE = re.compile(r'^(.+)_p-(\d+)\.png$')
VIDEO_EXTENSIONS = ('.mp4', '.avi', '.mov', '.mkv', '.webm')
IMAGE_EXTENSIONS = ('.png', '.jpg', '.jpeg')
MPV_SOCKET_TIMEOUT = 5
MPV_EVENT_POLL_INTERVAL = 0.1
SCHEDULE_POWEROFF_MARGIN_MINUTES = 2

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


def parse_hhmm(value):
    if not isinstance(value, str):
        raise ValueError("Ожидается строка времени HH:MM")

    parts = value.split(":")
    if len(parts) != 2:
        raise ValueError(f"Некорректный формат времени: {value}")

    hours = int(parts[0])
    minutes = int(parts[1])
    if not (0 <= hours <= 23 and 0 <= minutes <= 59):
        raise ValueError(f"Время вне диапазона: {value}")

    return hours, minutes


def normalize_schedule(raw_schedule):
    if raw_schedule in (None, "", {}):
        return None

    if not isinstance(raw_schedule, dict):
        raise ValueError("schedule должен быть объектом")

    start_time = raw_schedule.get("start_time")
    end_time = raw_schedule.get("end_time")
    parse_hhmm(start_time)
    parse_hhmm(end_time)

    return {
        "start_time": start_time,
        "end_time": end_time,
    }


def schedule_to_datetimes(schedule, now=None):
    if schedule is None:
        return None

    now = now or datetime.now()
    start_h, start_m = parse_hhmm(schedule["start_time"])
    end_h, end_m = parse_hhmm(schedule["end_time"])

    start_today = now.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
    end_today = now.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
    is_24x7 = (start_h, start_m) == (end_h, end_m)

    if is_24x7:
        return {
            "is_active": True,
            "next_start": now,
            "next_end": None,
            "is_24x7": True,
        }

    if start_today < end_today:
        is_active = start_today <= now < end_today
        next_start = start_today if now < start_today else start_today + timedelta(days=1)
        next_end = end_today if is_active else None
    else:
        is_active = now >= start_today or now < end_today
        if is_active and now >= start_today:
            next_end = end_today + timedelta(days=1)
        elif is_active:
            next_end = end_today
        else:
            next_end = None

        if now < start_today and now >= end_today:
            next_start = start_today
        else:
            next_start = start_today + timedelta(days=1)

    return {
        "is_active": is_active,
        "next_start": next_start,
        "next_end": next_end,
        "is_24x7": False,
    }


def schedule_signature(schedule):
    if not schedule:
        return None
    return f"{schedule['start_time']}-{schedule['end_time']}"


def apply_schedule_to_config(config, raw_schedule):
    try:
        normalized = normalize_schedule(raw_schedule)
    except ValueError as e:
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        print(f"[Schedule {now}] Некорректное расписание от сервера: {e}")
        return config.get("schedule")

    current = config.get("schedule")
    if current != normalized:
        config["schedule"] = normalized
        save_config(config)
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        if normalized:
            print(
                f"[Schedule {now}] Новое расписание сохранено: "
                f"{normalized['start_time']} - {normalized['end_time']}"
            )
        else:
            print(f"[Schedule {now}] Расписание очищено.")

    return normalized


def is_linux():
    return sys.platform.startswith("linux")


def run_system_command(command):
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception as e:
        return False, str(e)

    if result.returncode == 0:
        return True, result.stdout.strip()

    error_text = result.stderr.strip() or result.stdout.strip() or f"код {result.returncode}"
    return False, error_text


def suspend_until(target_dt, mode="freeze"):
    if not is_linux():
        return False, "suspend через rtcwake поддерживается только на Linux"

    rtcwake_path = shutil.which("rtcwake")
    if not rtcwake_path:
        return False, "утилита rtcwake не найдена"

    wake_ts = int(target_dt.timestamp())
    return run_system_command([rtcwake_path, "-m", mode, "-t", str(wake_ts)])


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


def has_playable_content(media_dir):
    if not os.path.exists(media_dir):
        return False

    for filename in os.listdir(media_dir):
        if filename == DURATIONS_FILE:
            continue
        if os.path.isfile(os.path.join(media_dir, filename)):
            return True
    return False


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
        "heartbeat_interval": (30, (int, float)),
        "check_videos_interval": (180, (int, float)),
        "image_display_duration": (5, (int, float)),
        "server_url": ("http://217.71.129.139:4085", (str,)),
        "device_id": ("NSTU_OrangePI2302", (str,)),
        "media_dir": ("./content", (str,)),
        "token": ("", (str,)),
        "schedule": (None, (dict, type(None))),
        "schedule_poweroff_enabled": (False, (bool,)),
        "schedule_suspend_enabled": (False, (bool,)),
        "schedule_suspend_mode": ("freeze", (str,)),
        "schedule_wakeup_margin_minutes": (SCHEDULE_POWEROFF_MARGIN_MINUTES, (int, float)),
    }

    for key, (default_value, allowed_types) in defaults.items():
        if key not in config or not isinstance(config[key], allowed_types):
            print(f"[Config] Предупреждение: ключ '{key}' отсутствует или некорректен. Используем значение по умолчанию: {default_value}")
            config[key] = default_value

    # Ограничения на интервалы
    config["heartbeat_interval"] = max(15, min(120, int(config["heartbeat_interval"])))      # 15..120 сек
    config["check_videos_interval"] = max(60, min(600, int(config["check_videos_interval"]))) # 1..10 минут
    config["image_display_duration"] = max(1, int(config["image_display_duration"]))
    config["schedule_poweroff_enabled"] = bool(config["schedule_poweroff_enabled"])
    if config["schedule_poweroff_enabled"] and not config["schedule_suspend_enabled"]:
        config["schedule_suspend_enabled"] = True
    config["schedule_wakeup_margin_minutes"] = max(0, min(60, int(config["schedule_wakeup_margin_minutes"])))
    config["schedule_suspend_mode"] = str(config["schedule_suspend_mode"]).strip() or "freeze"

    try:
        config["schedule"] = normalize_schedule(config.get("schedule"))
    except ValueError as e:
        print(f"[Config] Предупреждение: расписание в конфиге некорректно: {e}. Игнорируем.")
        config["schedule"] = None

    # Принудительно приводим media_dir к абсолютному пути
    if not os.path.isabs(config["media_dir"]):
        config["media_dir"] = os.path.abspath(config["media_dir"])

    print(f"[Config] Загружены интервалы:")
    print(f"   Heartbeat          → {config['heartbeat_interval']} сек")
    print(f"   Check videos       → {config['check_videos_interval']} сек")
    print(f"   Изображения по умолчанию → {config.get('image_display_duration', 5)} сек")
    print(f"   Media dir          → {config['media_dir']}")
    if config.get("schedule"):
        print(f"   Schedule           → {config['schedule']['start_time']} - {config['schedule']['end_time']}")
    print(f"   Suspend by schedule  → {config['schedule_suspend_enabled']}")
    print(f"   Suspend mode         → {config['schedule_suspend_mode']}")

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
        self.schedule_state = None
        self.last_power_action_key = None

    def show_curtain(self):
        self.root.deiconify()
        self.root.update()

    def hide_curtain(self):
        self.root.withdraw()
        self.root.update()

    def start_player_if_needed(self):
        global player_process
        if not has_playable_content(self.config['media_dir']):
            return

        if player_process is None or player_process.poll() is not None:
            start_player(
                self.config['media_dir'],
                image_display_duration=self.config.get('image_display_duration', 5)
            )

    def update_schedule(self, raw_schedule):
        if raw_schedule is None and "schedule" not in self.config:
            return
        apply_schedule_to_config(self.config, raw_schedule)

    def apply_schedule_state(self, now=None):
        global player_process
        now = now or datetime.now()
        schedule = self.config.get("schedule")

        if not schedule:
            if self.schedule_state != "no-schedule":
                print(f"[Schedule {now.strftime('%Y-%m-%d %H:%M:%S')}] Расписание не задано. Работаем без ограничений.")
            self.schedule_state = "no-schedule"
            self.last_power_action_key = None
            return True

        info = schedule_to_datetimes(schedule, now)
        if info["is_active"]:
            if self.schedule_state != "active":
                if info["is_24x7"]:
                    print(f"[Schedule {now.strftime('%Y-%m-%d %H:%M:%S')}] Режим 24/7 активен.")
                else:
                    print(
                        f"[Schedule {now.strftime('%Y-%m-%d %H:%M:%S')}] "
                        f"Входим в окно вещания до {info['next_end'].strftime('%Y-%m-%d %H:%M:%S')}."
                    )
                self.last_power_action_key = None
                if not self.is_blocked:
                    request_curtain(False)
                    self.start_player_if_needed()
            self.schedule_state = "active"
            return True

        next_start = info["next_start"]
        now_str = now.strftime('%Y-%m-%d %H:%M:%S')
        previous_state = self.schedule_state
        if previous_state != "inactive":
            print(
                f"[Schedule {now_str}] Вне окна вещания. "
                f"Следующий старт: {next_start.strftime('%Y-%m-%d %H:%M:%S')}."
            )
        self.schedule_state = "inactive"
        if player_process is not None or previous_state != "inactive":
            stop_player()
        request_curtain(True)

        if self.config.get("schedule_suspend_enabled") and next_start is not None:
            self.prepare_suspend_until(next_start, now)

        return False

    def prepare_suspend_until(self, next_start, now=None):
        now = now or datetime.now()
        power_key = next_start.isoformat()
        if self.last_power_action_key == power_key:
            return

        self.last_power_action_key = power_key
        wake_margin = self.config.get("schedule_wakeup_margin_minutes", SCHEDULE_POWEROFF_MARGIN_MINUTES)
        wake_time = next_start - timedelta(minutes=wake_margin)
        if wake_time <= now:
            wake_time = next_start

        now_str = now.strftime('%Y-%m-%d %H:%M:%S')
        suspend_mode = self.config.get("schedule_suspend_mode", "freeze")
        ok, info = suspend_until(wake_time, mode=suspend_mode)
        if ok:
            print(
                f"[Schedule {now_str}] Устройство переведено в {suspend_mode} "
                f"до {wake_time.strftime('%Y-%m-%d %H:%M:%S')}."
            )
        else:
            print(f"[Schedule {now_str}] Не удалось перевести устройство в {suspend_mode}: {info}")

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
            if self.apply_schedule_state():
                self.start_player_if_needed()
                time.sleep(2)
                request_curtain(False)

    def shutdown(self, *_):
        stop_player()
        self.root.after(0, self.root.destroy)

    def worker_loop(self):
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        self.apply_schedule_state()
        
        # Первая проверка контента сразу после старта
        print(f"[{now_str}] Начальная проверка контента после запуска...")
        self.process_check_videos(now_str)
        self.last_check = time.time()
        self.last_hb = time.time()

        print(f"[Info] Клиент запущен. Heartbeat: {self.config['heartbeat_interval']}с, Check: {self.config['check_videos_interval']}с")

        while True:
            now_ts = time.time()
            now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            self.apply_schedule_state()

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
            if "schedule" in data:
                self.update_schedule(data.get("schedule"))
            schedule_info = schedule_to_datetimes(self.config.get("schedule"), datetime.now()) if self.config.get("schedule") else None
            is_schedule_active = schedule_info["is_active"] if schedule_info else True

            if status == 205:
                print(f"[{now_str}] Обновление контента...")
                request_curtain(True)
                stop_player()
                download_content(
                    data.get("videos", []),
                    self.config['media_dir'],
                    self.config.get('image_display_duration', 5)
                )
                if is_schedule_active and not self.is_blocked:
                    self.start_player_if_needed()
                    time.sleep(3)
                    request_curtain(False)

            elif status == 204:
                if is_schedule_active and not self.is_blocked:
                    self.start_player_if_needed()

            elif status == 401:
                sync_token(self.config)

            elif status == 403:
                self.handle_blocked()

            self.apply_schedule_state()

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
