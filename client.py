import os
import json
import time
import subprocess
import requests
import tkinter as tk
import threading
from datetime import datetime
import signal
from urllib.parse import urlparse

CONFIG_FILE = 'config.json'
# Глобальная переменная для хранения процесса плеера
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
        """Запуск черного окна в отдельном потоке."""
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._create_window, daemon=True)
        self.thread.start()
        time.sleep(1)

    def stop(self):
        """Закрытие черного окна."""
        if self.root:
            self.root.after(0, self.root.destroy)
            self.thread.join()
            self.root = None


curtain = BlackCurtain()


def stop_player():
    """Остановка плеера, если он запущен."""
    global player_process
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    if player_process:
        try:
            # Отправляем сигнал завершения группе процессов
            os.killpg(os.getpgid(player_process.pid), signal.SIGTERM)
            player_process = None
            print(f"[Player {now}] Предыдущий процесс плеера остановлен.")
        except Exception as e:
            print(f"[Player {now}] Ошибка при остановке плеера: {e}")


def start_player(media_dir):
    """Запуск цикличного воспроизведения всех файлов в папке."""
    global player_process
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # Получаем список всех файлов в директории
    files = [os.path.join(media_dir, f) for f in os.listdir(media_dir) 
             if os.path.isfile(os.path.join(media_dir, f))]

    if not files:
        print(f"[Player {now}] Нет файлов для воспроизведения.")
        return

    print(f"[Player {now}] Запуск воспроизведения {len(files)} файлов.")

    # Команда mpv:
    # --fs: полноэкранный режим
    # --loop-playlist: крутить список бесконечно
    # --no-osc: убрать элементы управления с экрана
    # --no-input-default-bindings: отключить реакцию на клавиатуру
    cmd = ["mpv", "--fs", "--loop-playlist", "--no-osc", "--no-audio"] + files

    try:
        # Запускаем в новой группе процессов, чтобы удобно было убивать
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


def get_local_video_ids(media_dir):
    """
    Сканирует папку и возвращает список ID (имен файлов без расширения).
    """
    if not os.path.exists(media_dir):
        os.makedirs(media_dir)
        return []

    video_ids = []
    for filename in os.listdir(media_dir):
        if os.path.isfile(os.path.join(media_dir, filename)):
            file_id = os.path.splitext(filename)[0]
            video_ids.append(file_id)
    return video_ids


def sync_token(config):
    """Обновление токена в случае его устаревания."""
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
    """Уведомление сервера о том, что устройство в сети."""
    url = f"{config['server_url']}/api/heartbeat"
    payload = {"token": config['token'], "id": config['device_id']}
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    try:
        resp = requests.post(url, json=payload, timeout=10)
        data = resp.json()
        print(f"[Heartbeat {now}] Status: {data.get('status')} ({data.get('message')})")

        if data.get("status") in [401, 403]:
            sync_token(config)
    except Exception as e:
        print(f"[Heartbeat {now}] Error: {e}")


def download_content(videos, media_dir):
    """Очистка папки и загрузка новых файлов с переименованием в ID."""
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    print(f"[* {now}] Очистка локального контента...")
    if os.path.exists(media_dir):
        for file in os.listdir(media_dir):
            file_path = os.path.join(media_dir, file)
            try:
                if os.path.isfile(file_path):
                    os.unlink(file_path)
            except Exception as e:
                print(f"[! {now}]Ошибка удаления {file_path}: {e}")
    else:
        os.makedirs(media_dir)

    for v in videos:
        v_id = v['id']
        v_url = v['url']

        # Определяем расширение файла из URL
        ext = os.path.splitext(urlparse(v_url).path)[1]
        target_filename = f"{v_id}{ext}"
        target_path = os.path.join(media_dir, target_filename)

        print(f"[* {now}] Загрузка {v_id} -> {target_filename}")
        try:
            subprocess.run(['wget', '-O', target_path, v_url], check=True)
        except subprocess.CalledProcessError as e:
            print(f"[! {now}] Ошибка скачивания {v_id}: {e}")


def check_videos(config):
    """Проверка необходимости обновления контента."""
    url = f"{config['server_url']}/api/check-videos"
    current_ids = get_local_video_ids(config['media_dir'])
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    payload = {
        "token": config['token'],
        "id": config['device_id'],
        "videos": current_ids
    }

    try:
        resp = requests.post(url, json=payload, timeout=10)
        data = resp.json()

        status = data.get("status")
        if status == 205:
            print(f"[! {now}] Контент не актуален. Запуск обновления...")
            curtain.start()
            stop_player()
            download_content(data.get("videos", []), config['media_dir'])
            start_player(config['media_dir'])
            time.sleep(3)
            curtain.stop()
        elif status == 204:
            global player_process
            if player_process is None or player_process.poll() is not None:
                start_player(config['media_dir'])
            print(f"[OK {now}] Контент на устройстве актуален.")
        elif status in [401, 403]:
            sync_token(config)

    except Exception as e:
        print(f"[CheckVideos {now}] Error: {e}")


class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.attributes('-fullscreen', True)
        self.root.configure(background='black')
        self.root.config(cursor="none")
        self.root.withdraw() # По умолчанию скрыто
        
        self.config = load_config()
        self.last_hb = 0
        self.last_check = 0

    def show_curtain(self):
        self.root.deiconify()
        self.root.update()

    def hide_curtain(self):
        self.root.withdraw()
        self.root.update()

    def worker_loop(self):
        """Фоновый поток для работы с API и скачивания"""
        while True:
            now_ts = time.time()
            now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

            # Heartbeat
            if now_ts - self.last_hb > self.config.get('heartbeat_interval', 30):
                heartbeat(self.config)
                self.last_hb = now_ts

            # Check Videos
            if now_ts - self.last_check > self.config.get('check_videos_interval', 60):
                self.process_check_videos(now_str)
                self.last_check = now_ts

            time.sleep(1)

    def process_check_videos(self, now_str):
        url = f"{self.config['server_url']}/api/check-videos"
        current_ids = get_local_video_ids(self.config['media_dir'])
        payload = {"token": self.config['token'], "id": self.config['device_id'], "videos": current_ids}

        try:
            resp = requests.post(url, json=payload, timeout=10)
            data = resp.json()
            if data.get("status") == 205:
                print(f"[{now_str}] Обновление контента...")
                
                self.root.after(0, self.show_curtain)
                
                stop_player()
                download_content(data.get("videos", []), self.config['media_dir'])
                start_player(self.config['media_dir'])
                
                time.sleep(3) 
                self.root.after(0, self.hide_curtain)
            
            elif data.get("status") == 204:
                global player_process
                if player_process is None or player_process.poll() is not None:
                    start_player(self.config['media_dir'])

        except Exception as e:
            print(f"[{now_str}] Ошибка check_videos: {e}")

    def run(self):
        # Запускаем логику в отдельном потоке
        t = threading.Thread(target=self.worker_loop, daemon=True)
        t.start()
        # Запускаем Tkinter в главном потоке
        self.root.mainloop()


if __name__ == "__main__":
    app = App()
    app.run()
