"""The Telegram bot of a lostOS server: how people come in, what they can ask from the pocket, and who tells them when something happens.

It talks to Telegram by long polling (the server opens the connection, nothing from Telegram reaches the server, so no webhook and no open port)
The token of the bot is in the environment of the service (LOSTOS_TG_TOKEN); the file is readable by root and the service only.

Who may talk to it: the people in accounts.py (the owner and the members he invited). Everybody else gets one polite refusal.
What it does:
  login        the login page of the panel shows a link to the bot and a two digit number; the bot shows three numbers and the person taps
               the one from the screen ("number matching"): a link that somebody else sent cannot log the person's Telegram into a stranger's browser
  bind         the owner ties his Telegram account to the server (a button in the panel)
  invite       the owner makes a link; whoever opens it and agrees becomes a member
  stations     /connect <identifier> ties the speaker whose identifier the person typed (the speaker then asks for its Alice button); /stations lists his own
  control      /status /play /pause /next /prev /volume N  (owner and members)
  sessions     /sessions shows where the person is signed in and ends sessions; /logout ends them all
  tell         a new login, a station that joined or went away
The buttons carry short ids of one-time tokens (16 characters of a hash), never the token itself."""
import asyncio
import html
import os
import random
import time

import aiohttp

API = "https://api.telegram.org"
PHRASE_CLOSED = "Регистрация на этом сервере lostOS только по приглашению. Попросите владельца прислать ссылку"
PHRASE_REFUSE = "Это закрытый сервер lostOS. Чтобы получить доступ, попросите владельца прислать вам приглашение"


def esc(s) -> str:
    return html.escape(str(s or ""), quote=False)


def short_agent(agent: str) -> str:
    """The user agent of a browser in a few words: "Safari · iPhone"."""
    a = agent or ""
    os_ = next((n for k, n in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"), ("Windows", "Windows"), ("Mac OS", "Mac"), ("Linux", "Linux")) if k in a), "")
    br = next((n for k, n in (("Edg/", "Edge"), ("OPR/", "Opera"), ("Firefox", "Firefox"), ("Chrome", "Chrome"), ("CriOS", "Chrome"), ("Safari", "Safari")) if k in a), "")
    return " · ".join(x for x in (br, os_) if x) or "неизвестное устройство"


class Bot:
    def __init__(self, token: str, accounts, log=print, ctx=None, api: str = API, public_url: str = ""):
        self.token, self.acc, self.log, self.ctx, self.api, self.public_url = token, accounts, log, ctx, api.rstrip("/"), public_url
        self.username = ""
        self.offset = 0
        self.http: aiohttp.ClientSession = None
        self.press_msgs: dict = {}                    # station id -> (chat id, message id) of the question to the owner
        self.offline_timers: dict = {}
        self.stopped = False

    # ---- Telegram
    async def call(self, method: str, **params):
        try:
            async with self.http.post(f"{self.api}/bot{self.token}/{method}", json=params, timeout=aiohttp.ClientTimeout(total=40)) as r:
                d = await r.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            self.log(f"telegram: {method}: {e!r}")
            return None
        if not d.get("ok"):
            if d.get("error_code") != 400:                               # 400 = "message is not modified" and the like
                self.log(f"telegram: {method}: {d.get('error_code')} {str(d.get('description'))[:80]}")
            return None
        return d["result"]

    @staticmethod
    def markup(buttons):
        return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in buttons]} if buttons else None

    async def send(self, chat, text: str, buttons=None):
        return await self.call("sendMessage", chat_id=chat, text=text, parse_mode="HTML", disable_web_page_preview=True, reply_markup=self.markup(buttons))

    async def edit(self, chat, msg, text: str, buttons=None):
        return await self.call("editMessageText", chat_id=chat, message_id=msg, text=text, parse_mode="HTML", reply_markup=self.markup(buttons) or {"inline_keyboard": []})

    # ---- the loop
    async def run(self):
        self.http = aiohttp.ClientSession()
        try:
            me = None
            while not me and not self.stopped:
                me = await self.call("getMe")
                if not me:
                    await asyncio.sleep(15)
            if self.stopped:
                return
            self.username = me["username"]
            self.log(f"telegram: bot @{self.username} is on")
            try:                                                           # the command line (lostos-admin invite) builds the links of the operator from it
                with open(os.path.join(os.environ.get("LOSTOS_DATA") or ".", "bot_username.txt"), "w", encoding="utf-8") as f:
                    f.write(self.username)
            except OSError:
                pass
            await self.call("setMyCommands", commands=[{"command": c, "description": d} for c, d in (
                ("status", "что играет и как колонка"), ("play", "продолжить"), ("pause", "пауза"), ("next", "следующий трек"), ("prev", "предыдущий трек"),
                ("volume", "громкость, например /volume 30"), ("stations", "мои колонки"), ("connect", "подключить колонку по идентификатору"), ("sessions", "где вы вошли"), ("logout", "выйти везде"))])
            wait = 2
            while not self.stopped:
                ups = await self.call("getUpdates", offset=self.offset, timeout=25, allowed_updates=["message", "callback_query"])
                if ups is None:
                    await asyncio.sleep(wait)
                    wait = min(wait * 2, 60)
                    continue
                wait = 2
                for u in ups:
                    self.offset = max(self.offset, u["update_id"] + 1)
                    try:
                        await self.handle(u)
                    except Exception as e:                                # one broken update must not stop the bot
                        self.log(f"telegram: update failed: {e!r}")
        finally:
            await self.http.close()

    def stop(self):
        self.stopped = True

    async def handle(self, u: dict):
        if "message" in u:
            m = u["message"]
            if m.get("chat", {}).get("type") != "private" or "from" not in m:
                return
            await self.on_message(m)
        elif "callback_query" in u:
            q = u["callback_query"]
            if q.get("message", {}).get("chat", {}).get("type") != "private":
                return
            await self.on_callback(q)

    # ---- people who ask for access
    def register_request(self, tg: int, name: str, username: str) -> str:
        """A stranger asked to come in: -> "closed" (only by a link) | "full" | "joined" (the server is open to everybody) | "new" | "pending" | "denied".
        A new request is told to the operator with the two buttons."""
        mode = self.ctx.registration()
        if mode == "invite":
            return "closed"
        if mode == "open":
            if self.ctx.capacity_left() <= 0:
                return "full"
            self.acc.admit(tg, name, username, "вошёл сам: регистрация открыта")
            self.tell_operators(f"Зарегистрировался: <b>{esc(name)}</b>" + (f" (@{esc(username)})" if username else "") + f"\nСвободных мест: {self.ctx.capacity_left()}")
            return "joined"
        st = self.acc.request_access(tg, name, username)
        if st == "new":
            for a in self.acc.admins():
                asyncio.get_event_loop().create_task(self.send(a["tg_id"], f"Просит доступ к lostOS: <b>{esc(name)}</b>" + (f" (@{esc(username)})" if username else "") + f"\nСвободных мест: {self.ctx.capacity_left()}",
                                                               [[("Одобрить", f"A+{tg}"), ("Отклонить", f"A-{tg}")]]))
        return st

    def tell_operators(self, text: str):
        for a in self.acc.admins():
            asyncio.get_event_loop().create_task(self.send(a["tg_id"], text))

    async def refuse(self, chat: int, f: dict):
        name, username = self.who(f)
        st = self.register_request(f["id"], name, username)
        await self.send(chat, {"closed": PHRASE_CLOSED, "full": "На сервере сейчас нет свободных мест, попробуйте позже", "joined": "Добро пожаловать в lostOS ✅ Теперь можно войти на странице сервера через Telegram. Команды: /help", "new": "Заявка отправлена владельцу сервера. Когда её одобрят, я напишу сюда", "pending": "Заявка уже отправлена, ждём решения владельца",
                               "denied": "Доступ не одобрен"}.get(st, PHRASE_REFUSE))

    # ---- messages
    def who(self, f: dict):
        name = " ".join(x for x in (f.get("first_name"), f.get("last_name")) if x) or f.get("username") or str(f["id"])
        return name, f.get("username", "")

    async def on_message(self, m: dict):
        chat, f = m["chat"]["id"], m["from"]
        text = (m.get("text") or "").strip()
        if not text.startswith("/"):
            return await self.send(chat, "Команды: /status /play /pause /next /prev /volume /sessions /logout") if self.acc.role_of(f["id"]) else await self.refuse(chat, f)
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        arg = arg.strip()
        if cmd == "/start":
            return await self.on_start(chat, f, arg)
        role = self.acc.role_of(f["id"])
        if not role:
            return await self.refuse(chat, f)
        if cmd in ("/help", "/start"):
            return await self.send(chat, "Команды: /status /play /pause /next /prev /volume 30 /sessions /logout" + "\n/stations, /connect: колонки")
        if cmd == "/status":
            return await self.send(chat, await self.status_text(f["id"]))
        if cmd in ("/play", "/pause", "/next", "/prev"):
            try:
                await self.ctx.media_command(f["id"], {"/prev": "previous"}.get(cmd, cmd[1:]), {})
                return await self.send(chat, "Готово")
            except Exception as e:
                return await self.send(chat, f"Не получилось: {esc(e)}")
        if cmd == "/volume":
            if not arg.isdigit() or not 0 <= int(arg) <= 100:
                return await self.send(chat, "Укажите число от 0 до 100, например /volume 30")
            try:
                await self.ctx.media_command(f["id"], "volume", {"level": int(arg)})
                return await self.send(chat, f"Громкость {int(arg)}.")
            except Exception as e:
                return await self.send(chat, f"Не получилось: {esc(e)}")
        if cmd == "/requests":
            u = self.acc.user(f["id"])
            if not (u and u.get("admin")):
                return await self.send(chat, "Это команда оператора сервера")
            rows = self.acc.pending_requests()
            if not rows:
                return await self.send(chat, "Заявок нет")
            for r in rows:
                await self.send(chat, f"<b>{esc(r['name'])}</b>" + (f" (@{esc(r['username'])})" if r["username"] else ""), [[("Одобрить", f"A+{r['tg_id']}"), ("Отклонить", f"A-{r['tg_id']}")]])
            return
        if cmd == "/stations":
            return await self.send_stations(chat, f["id"], role)
        if cmd == "/connect":
            if not arg:
                return await self.send(chat, "Напишите идентификатор колонки: /connect и 20 знаков, которые показывает установщик")
            if not self.ctx.link:
                return await self.send(chat, "Подключение колонок здесь не включено")
            res = await self.ctx.link.claim(arg, f["id"])
            return await self.send(chat, {"press": "Нажмите кнопку Алисы на колонке в течение минуты", "ok": "Колонка подключена ✅", "busy": "Эту колонку уже подключает другой человек",
                                          "offline": "Колонка сейчас не в сети"}.get(res, "Такой колонки нет в ожидании: проверьте идентификатор и что она включена"))
        if cmd == "/sessions":
            return await self.send_sessions(chat, f["id"], role)
        if cmd == "/logout":
            n = self.acc.revoke_all(f["id"])
            self.acc.log(f["id"], "logout", f"вышел везде ({n})")
            return await self.send(chat, f"Вы вышли на всех устройствах ({n}). Войти снова можно на странице входа")
        await self.send(chat, "Не знаю такой команды. /help")

    async def status_text(self, tg: int) -> str:
        c = self.ctx
        lines = []
        try:
            st = await c.status(tg)
            lines.append("Колонка: <b>" + ("В сети" if st["online"] else "Не в сети") + "</b>")
            lines.append("Играет: " + esc(st["now"] or "ничего"))
        except Exception as e:
            lines.append(f"Не получилось узнать: {esc(e)}")
        return "\n".join(lines)

    async def on_start(self, chat: int, f: dict, payload: str):
        name, username = self.who(f)
        kind, raw = payload[:1], payload[1:]
        if kind == "l":                                                      # login from the browser
            t = self.acc.token(raw, "login")
            if not t or t["state"] != "wait":
                return await self.send(chat, "Эта ссылка устарела. Обновите страницу входа и попробуйте снова")
            if not self.acc.role_of(f["id"]):
                return await self.refuse(chat, f)
            h = t["hash"][:16]
            nums = {t["code"]}
            while len(nums) < 3:
                nums.add(f"{random.randrange(10, 100)}")
            row = [(n, f"L{h}{n}") for n in random.sample(sorted(nums), 3)]
            return await self.send(chat, f"Кто-то входит в lostOS.\nУстройство: <b>{esc(short_agent(t['agent']))}</b>\nАдрес: {esc(t['ip'] or '—')}\n\n"
                                         "Нажмите число, которое сейчас показано на странице входа. Если вы не входите, нажмите «Это не я»", [row, [("Это не я", f"L{h}x")]])
        if kind == "b":                                                      # the owner ties this Telegram account
            t = self.acc.token(raw, "bind")
            if not t or t["state"] != "wait":
                return await self.send(chat, "Эта ссылка устарела. Откройте её в панели ещё раз")
            owner = self.acc.owner()
            note = f"\n\nСейчас владелец: <b>{esc(owner['name'])}</b>, он будет заменён" if owner and owner["tg_id"] != f["id"] else ""
            return await self.send(chat, f"Сделать аккаунт <b>{esc(name)}</b> владельцем сервера lostOS?{note}", [[("Да, это я", f"B{t['hash'][:16]}"), ("Нет", "Bx")]])
        if kind == "r":                                                      # the link of the operator: a new person
            t = self.acc.token(raw, "reg")
            if not t or t["state"] != "wait":
                return await self.send(chat, "Ссылка устарела или уже использована. Попросите владельца сервера прислать новую")
            if self.acc.user(f["id"]):
                return await self.send(chat, "Вы уже в lostOS. Вход: страница сервера, кнопка «Войти через Telegram»")
            return await self.send(chat, "Вас приглашают на сервер lostOS: свой голосовой ассистент на колонке. Принять приглашение?", [[("Принять", f"R{t['hash'][:16]}"), ("Нет", "Rx")]])
        if kind == "i":                                                      # an invitation
            t = self.acc.token(raw, "invite")
            if not t or t["state"] != "wait":
                return await self.send(chat, "Приглашение устарело или уже использовано. Попросите владельца прислать новое")
            if self.acc.user(f["id"]):
                return await self.send(chat, "Вы уже в lostOS. Вход: страница сервера, кнопка «Войти через Telegram»")
            return await self.send(chat, "Вас приглашают в lostOS: музыка и управление колонкой. Принять приглашение?", [[("Принять", f"I{t['hash'][:16]}"), ("Нет", "Ix")]])
        if self.acc.role_of(f["id"]):
            return await self.send(chat, f"Здравствуйте, {esc(name)}. Вы в lostOS.\n\n" + await self.status_text(f["id"]) + "\n\n/help: что я умею")
        await self.refuse(chat, f)

    # ---- buttons
    async def on_callback(self, q: dict):
        chat, msg, f, data = q["message"]["chat"]["id"], q["message"]["message_id"], q["from"], q.get("data", "")
        name, username = self.who(f)

        async def done(text, toast=""):
            await self.call("answerCallbackQuery", callback_query_id=q["id"], text=toast)
            await self.edit(chat, msg, text)

        k, rest = data[:1], data[1:]
        if k == "L":
            t = self.acc.token_by_id(rest[:16], "login")
            if not t or t["state"] != "wait" or not self.acc.role_of(f["id"]):
                return await done("Эта ссылка устарела")
            if rest[16:] == t["code"]:
                self.acc.set_token(t["hash"], "ok", f["id"])
                return await done("Готово: вход подтверждён. Вернитесь в браузер ✅")
            self.acc.set_token(t["hash"], "denied", f["id"])
            self.acc.log(f["id"], "login", "отклонён" if rest[16:] == "x" else "неверное число")
            return await done("Вход отклонён" if rest[16:] == "x" else "Число не то. Вход отклонён, это защита от чужих ссылок")
        if k == "B":
            t = self.acc.token_by_id(rest[:16], "bind") if rest != "x" else None
            if not t or t["state"] != "wait":
                return await done("Отменено" if rest == "x" else "Эта ссылка устарела")
            self.acc.bind_owner(f["id"], name, username)
            self.acc.set_token(t["hash"], "ok", f["id"])
            return await done(f"Готово: <b>{esc(name)}</b> теперь владелец сервера. Вернитесь в панель ✅")
        if k == "R":
            t = self.acc.token_by_id(rest[:16], "reg") if rest != "x" else None
            if not t or t["state"] != "wait":
                return await done("Отказано" if rest == "x" else "Ссылка устарела")
            if self.ctx.capacity_left() <= 0:
                return await done("На сервере сейчас нет свободных мест. Ссылка осталась действующей: попробуйте позже")
            self.acc.admit(f["id"], name, username, "вошёл по ссылке оператора")
            self.acc.use_token_by_hash(t["hash"])
            await done("Добро пожаловать в lostOS ✅ Теперь можно войти на странице сервера через Telegram. Команды: /help")
            self.tell_operators(f"По ссылке зарегистрировался: <b>{esc(name)}</b>" + (f" (@{esc(username)})" if username else "") + f"\nСвободных мест: {self.ctx.capacity_left()}")
            return
        if k == "I":
            t = self.acc.token_by_id(rest[:16], "invite") if rest != "x" else None
            if not t or t["state"] != "wait":
                return await done("Отказано" if rest == "x" else "Приглашение устарело")
            self.acc.add_member(f["id"], name, username, self.acc.tenant_of(t["by_tg"]))
            self.acc.use_token_by_hash(t["hash"])
            await done("Добро пожаловать в lostOS ✅ Теперь можно войти на странице сервера через Telegram. Команды: /help")
            await self.tell_owner(f"<b>{esc(name)}</b> принял(а) приглашение в lostOS.")
            return
        if k == "S":                                                         # S-<id>: remove a station (only the one it belongs to, or the owner of the server)
            link = self.ctx.link
            role = self.acc.role_of(f["id"])
            r = link.stations.get(rest[1:]) if link else None
            if not r or not link.visible_to(r, f["id"], role):
                return await done("Такой колонки нет")
            ok = await link.remove(rest[1:])
            return await done("Колонка отключена" if ok else "Такой колонки нет")
        if k == "A":                                                         # A+<id> approve, A-<id> turn down: only the operator
            u = self.acc.user(f["id"])
            if not (u and u.get("admin")):
                return await self.call("answerCallbackQuery", callback_query_id=q["id"], text="Только оператор сервера", show_alert=True)
            try:
                who = int(rest[1:])
            except ValueError:
                return await done("Не понял")
            if rest[:1] == "+" and self.ctx.capacity_left() <= 0:
                return await done("Мест нет: освободите место или увеличьте max_tenants")
            r = self.acc.decide(who, rest[:1] == "+")
            if not r:
                return await done("Заявка уже решена")
            await self.send(who, "Доступ открыт ✅ Откройте страницу сервера и войдите через Telegram" if rest[:1] == "+" else "Доступ не одобрен")
            return await done(f"{'Одобрено' if rest[:1] == '+' else 'Отклонено'}: {esc(r['name'])}")
        if k == "O":
            role = self.acc.role_of(f["id"])
            ok = self.acc.revoke(rest, None if role == "owner" else f["id"])
            await self.call("answerCallbackQuery", callback_query_id=q["id"], text="Сессия завершена" if ok else "Не найдено")
            return await self.send_sessions(chat, f["id"], role)
        if k == "X":                                                         # "this was not me" under a login notice
            self.acc.revoke_all(f["id"])
            return await done("Все сессии завершены. Если вы не входили, смените доступ: владелец может выпустить новую одноразовую ссылку командой lostos-link.")
        await self.call("answerCallbackQuery", callback_query_id=q["id"])

    async def send_stations(self, chat: int, tg_id: int, role: str):
        rows = self.ctx.link.listing(tg_id, role) if self.ctx.link else []
        if not rows:
            return await self.send(chat, "Колонок пока нет. Подключить: /connect и идентификатор, который показывает установщик")
        for s in rows:
            state = ("В сети" if s["online"] else "Не в сети") if s["confirmed"] else "ждёт нажатия кнопки Алисы"
            await self.send(chat, f"<b>{esc(s['name'] or 'Колонка')}</b>\n<code>{esc(s['id'])}</code>\n{state}", [[("Убрать", f"S-{s['id']}")]])

    async def send_sessions(self, chat: int, tg_id: int, role: str):
        rows = self.acc.sessions_of(None if role == "owner" else tg_id)
        if not rows:
            return await self.send(chat, "Сессий нет")
        lines, buttons = [], []
        for i, r in enumerate(rows[:12], 1):
            who = ""
            if role == "owner":
                u = self.acc.user(r["tg_id"])
                who = f"{esc(u['name'])}, " if u else ""
            lines.append(f"{i}. {who}{esc(short_agent(r['agent']))}, {time.strftime('%d.%m %H:%M', time.localtime(r['last'] or r['created']))}")
            buttons.append([(f"Завершить {i}", f"O{r['id']}")])
        await self.send(chat, "Где выполнен вход:\n" + "\n".join(lines), buttons)

    # ---- what the bot tells by itself
    async def tell_owner(self, text: str, buttons=None):
        o = self.acc.owner()
        if o:
            return await self.send(o["tg_id"], text, buttons)

    def after_login(self, tg_id: int, agent: str, ip: str):
        asyncio.get_event_loop().create_task(self.send(tg_id, f"Вход в lostOS: <b>{esc(short_agent(agent))}</b>, адрес {esc(ip or '—')}.", [[("Это не я: выйти везде", "X")]]))

    def on_link_event(self, kind: str, sid: str, name: str = "", extra: str = ""):
        """Called by link.py (the loop thread). Tells the person the station belongs to (extra = his Telegram id; 0 = the owner of the server)."""
        loop = asyncio.get_event_loop()
        try:
            tg = int(extra or 0)
        except ValueError:
            tg = 0
        to = lambda text: loop.create_task(self.send(tg, text) if tg else self.tell_owner(text))
        if kind == "confirmed":
            to("Колонка подключена ✅")
        elif kind == "press_failed":
            to("Кнопку не нажали вовремя, колонка не подключена. Введите её идентификатор снова, когда будете рядом")
        elif kind == "offline":
            t = self.offline_timers.pop(sid, None)
            if t:
                t.cancel()
            self.offline_timers[sid] = loop.call_later(180, lambda: to(f"Колонка <b>{esc(name or sid)}</b> не в сети уже три минуты"))
        elif kind == "online":
            t = self.offline_timers.pop(sid, None)
            if t:
                t.cancel()
