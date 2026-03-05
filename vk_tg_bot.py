import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

import requests

VK_API_VERSION = "5.199" 
VK_API_BASE= "https://api.vk.com/method"
UA = "VKAndroidApp/7.16-13360 (Android 12; SDK 32)"

# Токены
#VK_TOKEN = "" # Сюда нужно вставить свой токен от VK. Получить можно https://vkhost.github.io/ (выбрать Kate Mobile, подтвердить вход, в ссылке пример: "https://oauth.vk.com/blank.html#access_token=ваш_токен&expires_in=0&user_id=ваш_id_аккаунта&email=ваш_майл@gmail.com" будет токен, нужен токен начиная от "access_token=" (не включительно) до "&expires_in" (не включительно))
#TG_TOKEN = "" # Сюда нужно вставить свой токен от TG бота. Получить можно у @BotFather 

VK_TOKEN = input ("Введите токен VK: ")
TG_TOKEN = input ("Введите токен TG: ")

# VK API
def vk(method: str, token: str, **params):
    data = requests.get(
        f"{VK_API_BASE}/{method}",
        params={"access_token": token, "v": VK_API_VERSION, **params},
        headers={"User-Agent": UA}, timeout=30,
    ).json()
    if "error" in data:
        err = data["error"]
        raise RuntimeError(f"VK {err.get('error_code')}: {err.get('error_msg')}")
    return data.get("response")

def get_self_id(token: str) -> int:
    return vk("users.get", token)[0]["id"]

def send_audio_to_self(token: str, user_id: int, owner_id: int, audio_id: int):
    vk("messages.send", token, user_id=user_id,
       attachment=f"audio{owner_id}_{audio_id}",
       random_id=int(time.time() * 1000) % 2147483647)

def delete_vk_message(token: str, msg_id: int, peer_id: int):
    try:
        msgs = vk("messages.getById", token, message_ids=msg_id)
        if msgs and msgs.get("items"):
            cmid = msgs["items"][0].get("conversation_message_id")
            if cmid:
                vk("messages.delete", token,
                   peer_id=peer_id,
                   cmids=cmid)
                return
        # Fallback
        vk("messages.delete", token, message_ids=msg_id, peer_id=peer_id)
    except Exception as e:
        print(f"Не удалось удалить сообщение VK: {e}")

def parse_vk_audio_url(text: str):
    match = re.search(r"audio(-?\d+)_(\d+)", text)
    return (int(match.group(1)), int(match.group(2))) if match else None


# Загрузка
def sanitize(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "_", name).strip()

def resolve_m3u8(url: str, session: requests.Session) -> list:
    resp = session.get(url, timeout=30)
    resp.raise_for_status()
    base = url.rsplit("/", 1)[0] + "/"
    for line in resp.text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        abs_url = line if line.startswith("http") else base + line
        if ".m3u8" in abs_url.split("?")[0]:
            return resolve_m3u8(abs_url, session)
        yield abs_url

def download_audio(audio_url: str, out_path: str) -> bool:
    session = requests.Session()
    session.headers["User-Agent"] = UA
    if shutil.which("ffmpeg"):
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", audio_url, "-acodec", "libmp3lame",
             "-q:a", "2", "-loglevel", "error", out_path],
            timeout=180, capture_output=True)
        if r.returncode == 0 and os.path.exists(out_path):
            return True
    if ".m3u8" in audio_url:
        segments = list(resolve_m3u8(audio_url, session))
        if not segments:
            return False
        with open(out_path, "wb") as f:
            for seg in segments:
                for attempt in range(3):
                    try:
                        r = session.get(seg, timeout=30)
                        r.raise_for_status()
                        f.write(r.content)
                        break
                    except Exception:
                        time.sleep(0.5 * (attempt + 1))
        return os.path.exists(out_path)
    r = session.get(audio_url, stream=True, timeout=60)
    r.raise_for_status()
    with open(out_path, "wb") as f:
        for chunk in r.iter_content(65536):
            if chunk:
                f.write(chunk)
    return os.path.exists(out_path)

# Telegram API
def tg(method: str, tg_token: str, **kwargs):
    resp = requests.post(
        f"https://api.telegram.org/bot{tg_token}/{method}",
        timeout=60, **kwargs)
    return resp.json()

def tg_send(tg_token: str, chat_id, text: str):
    tg("sendMessage", tg_token, json={"chat_id": chat_id, "text": text})

def tg_send_audio(tg_token: str, chat_id, filepath: str, performer: str, title: str):
    with open(filepath, "rb") as f:
        tg("sendAudio", tg_token,
           data={"chat_id": chat_id, "performer": performer, "title": title},
           files={"audio": (os.path.basename(filepath), f, "audio/mpeg")})

def tg_get_updates(tg_token: str, offset: int) -> list:
    data = tg("getUpdates", tg_token,
              json={"offset": offset, "timeout": 25, "allowed_updates": ["message"]})
    return data.get("result", []) if data.get("ok") else []

# Основная логика
def process_vk_audio(audio: dict, tg_token: str, tg_chat_id, vk_token: str, vk_msg_id: int, vk_peer_id: int):
    """Скачивает трек и отправляет в Telegram."""
    artist = audio.get("artist", "Unknown")
    title  = audio.get("title",  "Unknown")
    url    = audio.get("url",    "")

    if not url:
        tg_send(tg_token, tg_chat_id, f"Нет ссылки на трек: {artist} — {title}")
        delete_vk_message(vk_token, vk_msg_id, vk_peer_id)
        return

    with tempfile.TemporaryDirectory() as tmp:
        out_path = os.path.join(tmp, "audio.mp3")
        ok = download_audio(url, out_path)
        if not ok:
            tg_send(tg_token, tg_chat_id, f"Не удалось скачать: {artist} — {title}")
            delete_vk_message(vk_token, vk_msg_id, vk_peer_id)
            return
        size_mb = os.path.getsize(out_path) / 1024 / 1024
        if size_mb > 50:
            tg_send(tg_token, tg_chat_id,
                    f"Файл слишком большой ({size_mb:.0f} МБ, лимит Telegram 50 МБ)")
            delete_vk_message(vk_token, vk_msg_id, vk_peer_id)
            return
        try:
            tg_send_audio(tg_token, tg_chat_id, out_path, performer=artist, title=title)
        except Exception as e:
            tg_send(tg_token, tg_chat_id, f"Ошибка отправки: {e}")

    delete_vk_message(vk_token, vk_msg_id, vk_peer_id)

def run(vk_token: str, tg_token: str):
    self_id = get_self_id(vk_token)
    pending = {}
    print("Бот запущен. Ctrl+C для остановки")

    # VK Longpoll в фоне
    def vk_longpoll():
        lp = vk("messages.getLongPollServer", vk_token, lp_version=3, need_pts=0)
        server, lp_key, ts = lp["server"], lp["key"], lp["ts"]
        while True:
            try:
                data = requests.get(
                    f"https://{server}",
                    params={"act": "a_check", "key": lp_key, "ts": ts,
                            "wait": 25, "mode": 2, "version": 3},
                    timeout=35).json()
                if data.get("failed") == 1:
                    ts = data["ts"]; continue
                if data.get("failed") in (2, 3):
                    lp2 = vk("messages.getLongPollServer", vk_token, lp_version=3, need_pts=0)
                    server, lp_key, ts = lp2["server"], lp2["key"], lp2["ts"]
                    continue
                ts = data["ts"]
                for update in data.get("updates", []):
                    if update[0] != 4:
                        continue
                    msg_id = update[1]
                    msgs = vk("messages.getById", vk_token, message_ids=msg_id)
                    if not msgs or not msgs.get("items"):
                        continue
                    msg = msgs["items"][0]
                    audio_list = [a["audio"] for a in msg.get("attachments", [])
                                  if a.get("type") == "audio"]
                    peer_id = msg.get("peer_id", msg.get("from_id", self_id))
                    for audio in audio_list:
                        k = f"{audio.get('owner_id')}_{audio.get('id')}"
                        if k in pending:
                            tg_chat_id = pending.pop(k)
                            threading.Thread(
                                target=process_vk_audio,
                                args=(audio, tg_token, tg_chat_id, vk_token, msg_id, peer_id),
                                daemon=True
                            ).start()
            except Exception:
                time.sleep(5)
    threading.Thread(target=vk_longpoll, daemon=True).start()

    # Telegram polling
    tg_offset = 0
    while True:
        try:
            updates = tg_get_updates(tg_token, tg_offset)
            for update in updates:
                tg_offset = update["update_id"] + 1
                msg = update.get("message", {})
                chat_id = msg.get("chat", {}).get("id")
                text    = msg.get("text", "").strip()
                if not chat_id or not text:
                    continue

                if text == "/start":
                    tg_send(tg_token, chat_id,
                            "Отправь ссылку на трек из VK, формат:\n"
                            "https://vk.com/audio-XXX_YYY")
                    continue

                parsed = parse_vk_audio_url(text)
                if not parsed:
                    tg_send(tg_token, chat_id,
                            "Не правильная ссылка.\n"
                            "Формат: https://vk.com/audio-XXX_YYY")
                    continue

                owner_id, audio_id = parsed
                k = f"{owner_id}_{audio_id}"
                if k in pending:
                    tg_send(tg_token, chat_id, "Трек в очереди")
                    continue

                pending[k] = chat_id
                try:
                    send_audio_to_self(vk_token, self_id, owner_id, audio_id)
                except RuntimeError as e:
                    tg_send(tg_token, chat_id, f"Ошибка VK: {e}")
                    del pending[k]

        except KeyboardInterrupt:
            print("\nБот остановлен")
            break
        except Exception as e:
            print(f"[TG] ошибка: {e}")
            time.sleep(5)

def main():
    vk_token = VK_TOKEN
    tg_token = TG_TOKEN

    resp = requests.get(f"https://api.telegram.org/bot{tg_token}/getMe", timeout=10).json()
    if not resp.get("ok"):
        print(f"TG токен не работает: {resp.get('description')}")
        sys.exit(1)
    print(f"TG бот: @{resp['result']['username']}")

    run(vk_token, tg_token)


if __name__ == "__main__":
    main()
