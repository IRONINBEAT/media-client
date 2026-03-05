import os
import json
import time
import subprocess
import requests
from datetime import datetime
import signal
from urllib.parse import urlparse

CONFIG_FILE = 'config.json'
# Глобальная переменная для хранения процесса плеера
player_process = None


def stop_player():
    """Остановка cvlc."""
    global player_process
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    if player_process:
        try:
            # Убиваем всю группу процессов vlc
            os.killpg(os.getpgid(player_process.pid), signal.SIGTERM)
            player_process = None
            print(f"[Player {now}] Процесс cvlc остановлен.")
        except Exception as e:
            print(f"[Player {now}] Ошибка при остановке: {e}")


def start_player(media_dir):
    """Запуск цикличного воспроизведения всех файлов в папке через cvlc."""
    global player_process
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # Получаем список всех файлов
    files = [os.path.join(media_dir, f) for f in os.listdir(media_dir) 
             if os.path.isfile(os.path.join(media_dir, f))]

    if not files:
        print(f"[Player {now}] Нет файлов для воспроизведения.")
        return

    print(f"[Player {now}] Запуск CVLC: {len(files)} файлов.")

    # Формируем команду для cvlc
    # --fullscreen: во весь экран
    # --loop: крутить плейлист бесконечно
    # --no-video-title-show: не показывать название файла при старте
    # --no-osd: убрать значки громкости/паузы
    cmd = ["cvlc", "--fullscreen", "--loop", "--no-video-title-show", "--no-osd"] + files

    try:
        # Используем os.setsid, чтобы можно было убить дерево процессов VLC
        player_process = subprocess.Popen(
            cmd, 
            stdout=subprocess.DEVNULL, 
            stderr=subprocess.DEVNULL,
            preexec_fn=os.setsid 
        )
    except Exception as e:
        print(f"[Player {now}] Ошибка запуска cvlc: {e}")


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
            stop_player()
            download_content(data.get("videos", []), config['media_dir'])
            start_player(config['media_dir'])
        elif status == 204:
            global player_process
            if player_process is None or player_process.poll() is not None:
                start_player(config['media_dir'])
            print(f"[OK {now}] Контент на устройстве актуален.")
        elif status in [401, 403]:
            sync_token(config)

    except Exception as e:
        print(f"[CheckVideos {now}] Error: {e}")


def main():
    if not os.path.exists(CONFIG_FILE):
        print(f"Файл {CONFIG_FILE} не найден!")
        return

    config = load_config()
    last_hb = 0
    last_check = 0

    print(f"--- Клиент запущен (ID: {config['device_id']}) ---")

    while True:
        now = time.time()

        # Heartbeat по таймеру
        if now - last_hb > config.get('heartbeat_interval', 30):
            heartbeat(config)
            last_hb = now

        # Проверка видео по таймеру
        if now - last_check > config.get('check_videos_interval', 60):
            check_videos(config)
            last_check = now

        time.sleep(1)


if __name__ == "__main__":
    main()
