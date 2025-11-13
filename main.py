from telethon import events, TelegramClient
import asyncio
import random
from datetime import datetime, timedelta
import json
from rapidfuzz import process, fuzz
from collections import deque
from pathlib import Path
from openai import OpenAI
import data

SYSTEM_PROMPT = """
Ты — ИИ-ассистент техподдержки AtlantaVPN.
Отвечай спокойно, уверенно, по делу. 1-3 предложения.
Если есть решение в базе — используй его.
Если нет — дай короткий полезный ответ.
Без извинений и официоза.
"""

with open("solutions.json", "r", encoding="utf-8") as f:
    SOLUTIONS = json.load(f)

def find_solution(message):
    keys = list(SOLUTIONS.keys())
    match, score, _ = process.extractOne(message.lower(), keys, scorer=fuzz.token_set_ratio)
    return SOLUTIONS[match] if score >= 60 else None

client_oa = OpenAI(api_key=data.api_key)

async def ask_gpt(text):
    def sync():
        r = client_oa.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": text}
            ],
            max_tokens=300
        )
        return r.choices[0].message.content
    return await asyncio.to_thread(sync)


class OpenTeleUser:
    def __init__(self, phone, api_id, api_hash):
        self.phone = phone
        self.session_file = Path(f"sessions/session_{phone.replace('+', '')}.session")
        self.session_file.parent.mkdir(parents=True, exist_ok=True)
        self.client = TelegramClient(str(self.session_file), api_id, api_hash)
        self.me = None

    async def init(self, password=None):
        await self.client.start(phone=self.phone, password=password)
        self.me = await self.client.get_me()


async def main():
    user = OpenTeleUser(
        phone=data.phone,
        api_id=data.api_id,
        api_hash=data.api_hash
    )
    await user.init(password=data.password)
    client = user.client
    await client.get_dialogs()
    operators_chat = await client.get_entity(data.OPERATORS_CHAT_ID)
    print("✅ Авторизованы как:", user.me.username)

    state = {}
    message_times = deque(maxlen=10)

    async def rate_limit():
        now = datetime.utcnow()
        while message_times and (now - message_times[0]).total_seconds() > 60:
            message_times.popleft()
        message_times.append(datetime.utcnow())
        await asyncio.sleep(random.uniform(3.2, 6.7))

    async def notify_operators(user_id, text):
        try:
            # Перебираем все диалоги, чтобы найти нужную группу
            async for dialog in client.iter_dialogs():
                if dialog.id == abs(data.OPERATORS_CHAT_ID) or dialog.name == "TEST AI SUPPORT":
                    chat = dialog.entity
                    await client.send_message(chat, f"⚠️ Передача диалога оператору\nUser: `{user_id}`\n{text}")
                    return
            print("Операторская группа не найдена")
        except Exception as e:
            print("Ошибка уведомления оператора:", e)

    @client.on(events.NewMessage(incoming=True))
    async def on_msg(event):
        if event.out:
            return

        user_id = event.sender_id
        text = event.raw_text.strip()
        now = datetime.utcnow()

        u = state.get(user_id, {"last": None, "replies": 0, "handover": False, "greeted": False})

        # reset session after 24h
        if u["last"] is None or (now - u["last"]) > timedelta(hours=24):
            u = {"last": None, "replies": 0, "handover": False, "greeted": False}

        # если передано оператору — ИИ молчит
        if u["handover"]:
            return

        # приветствие (не считаем в лимит)
        if not u["greeted"]:
            await rate_limit()
            await event.reply("Здравствуйте! Я — ИИ-помощник AtlantaVPN. Напишите ваш вопрос одним сообщением 😊")
            u["greeted"] = True
            u["last"] = now
            state[user_id] = u
            return

        # лимит ответов ИИ (справочник + GPT)
        if u["replies"] >= 3:
            # 👇 Пробуем найти решение в базе (например, "спасибо")
            solution = find_solution(text)

            # 👇 Если найдено благодарственное сообщение — не вызываем оператора
            if solution and "рад" in solution.lower():
                await rate_limit()
                await event.reply(solution)
                # 👇 Сбрасываем счётчик, чтобы можно было начать новый диалог
                u = {"last": now, "replies": 0, "handover": False, "greeted": True}
                state[user_id] = u
                return

            # 👇 Иначе вызываем оператора как раньше
            await notify_operators(user_id, text)
            await rate_limit()
            await event.reply("Передаю диалог оператору 👨‍💻 Пожалуйста, подождите...")
            u["handover"] = True
            state[user_id] = u
            return

        # ответ из базы
        solution = find_solution(text)
        if solution:
            await rate_limit()
            await event.reply(solution)
            u["replies"] += 1
            u["last"] = now
            state[user_id] = u
            return

        # GPT ответ
        await rate_limit()
        reply = await ask_gpt(text)
        await event.reply(reply)
        u["replies"] += 1
        u["last"] = now
        state[user_id] = u

    print("🤖 AtlantaVPN AI запущен — слушаю диалоги...")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
