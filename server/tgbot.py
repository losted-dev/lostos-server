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
  remote       /start: one message with what plays and the buttons ⏮ ⏯ ⏭, the volume and the choice of the station, edited in place;
               /play /pause /next /prev /volume N as commands too (owner and members, their own household only)
  plugins      /plugins: the plugins' own pages (manifest "ui", command panel_ui), drawn as a message with buttons; a search asks for text
  people       /people: the owner of a household invites and removes its members
  sessions     /sessions shows where the person is signed in and ends sessions; /logout ends them all
  operator     /requests (who asks to come in), /server (households, free places, stations on line)
Roles: the operator of the server (admin), the owner of a household (everybody who comes in owns one), a member he invited. Everything a
person sees or changes is of his own household (accounts.tenant_of); the menu of commands of each chat follows the role.
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
        self.sel: dict = {}                           # Telegram id -> the station he picked in the remote
        self.menus: dict = {}                         # Telegram id -> (role, operator) the menu of his chat was set for
        self.plug_lists: dict = {}                    # chat -> ids of the plugins of the last /plugins list
        self.pages: dict = {}                         # chat -> {pid, acts}: what the buttons of the page on the screen do
        self.wait_search: dict = {}                   # Telegram id -> (chat, pid, search id, message): the next text is a search

    # ---- Telegram
    async def call(self, method: str, **params):
        try:
            async with self.http.post(f"{self.api}/bot{self.token}/{method}", json=params, timeout=aiohttp.ClientTimeout(total=40)) as r:
                d = await r.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            self.log(f"telegram: {method}: {e!r}")
            return None
        if not d.get("ok"):
            if "not modified" not in str(d.get("description")):          # an edit that changes nothing is fine; every other refusal is told
                self.log(f"telegram: {method}: {d.get('error_code')} {str(d.get('description'))[:120]}")
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
            await self.call("setMyCommands", commands=[{"command": "start", "description": "начать"}, {"command": "help", "description": "что я умею"}])   # strangers;
            # the people of the server get the menu of their role in their own chat (set_commands)
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
        kind = "message" if "message" in u else "callback_query" if "callback_query" in u else "other"
        src = (u.get(kind) or {}).get("from", {}) if kind != "other" else {}
        what = ((u.get("message") or {}).get("text") or "").split(" ")[0][:20] if kind == "message" else ((u.get("callback_query") or {}).get("data") or "")[:1]
        self.log(f"telegram: {kind} {what!r} from {src.get('id', '?')}")       # what came in (the command only, never the text of a message)
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
        tg = f["id"]
        text = (m.get("text") or "").strip()
        if not text.startswith("/"):
            if not self.acc.role_of(tg):
                return await self.refuse(chat, f)
            w = self.wait_search.pop(tg, None)
            if w and text:                                                   # the answer to «🔎 Поиск» of a plugin's page
                await self.page_event(chat, tg, w[1], "input", w[2], text[:200])
                return
            return await self.remote(chat, tg)
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        arg = arg.strip()
        if cmd == "/start" and arg:
            return await self.on_start(chat, f, arg)
        role, admin = self.roles(tg)
        if not role:
            return await self.refuse(chat, f)
        self.wait_search.pop(tg, None)
        await self.set_commands(tg)
        if cmd in ("/start", "/status", "/remote"):
            return await self.remote(chat, tg)
        if cmd == "/help":
            return await self.send(chat, self.help_text(tg))
        if cmd == "/plugins":
            return await self.plugins_menu(chat, tg)
        if cmd in ("/play", "/pause", "/next", "/prev"):
            try:
                await self.ctx.media_command(tg, {"/prev": "previous"}.get(cmd, cmd[1:]), {}, sid=self.sel.get(tg, ""))
                return await self.send(chat, "Готово")
            except Exception as e:
                return await self.send(chat, f"Не получилось: {esc(e)}")
        if cmd == "/volume":
            if not arg.isdigit() or not 0 <= int(arg) <= 100:
                return await self.send(chat, "Укажите число от 0 до 100, например /volume 30")
            try:
                await self.ctx.media_command(tg, "volume", {"level": int(arg)}, sid=self.sel.get(tg, ""))
                return await self.send(chat, f"Громкость {int(arg)}.")
            except Exception as e:
                return await self.send(chat, f"Не получилось: {esc(e)}")
        if cmd in ("/requests", "/server"):
            if not admin:
                return await self.send(chat, "Это команда оператора сервера")
            if cmd == "/server":
                return await self.send_server(chat)
            rows = self.acc.pending_requests()
            if not rows:
                return await self.send(chat, "Заявок нет")
            for r in rows:
                await self.send(chat, f"<b>{esc(r['name'])}</b>" + (f" (@{esc(r['username'])})" if r["username"] else ""), [[("Одобрить", f"A+{r['tg_id']}"), ("Отклонить", f"A-{r['tg_id']}")]])
            return
        if cmd == "/people":
            if role != "owner":
                return await self.send(chat, "Людей дома приглашает и убирает его владелец")
            return await self.send_people(chat, tg)
        if cmd == "/stations":
            return await self.send_stations(chat, tg, role)
        if cmd == "/connect":
            if not arg:
                return await self.send(chat, "Напишите идентификатор колонки: /connect и 20 знаков, которые показывает установщик")
            if not self.ctx.link:
                return await self.send(chat, "Подключение колонок здесь не включено")
            res = await self.ctx.link.claim(arg, tg)
            return await self.send(chat, {"press": "Нажмите кнопку Алисы на колонке в течение минуты", "ok": "Колонка подключена ✅", "busy": "Эту колонку уже подключает другой человек",
                                          "offline": "Колонка сейчас не в сети"}.get(res, "Такой колонки нет в ожидании: проверьте идентификатор и что она включена"))
        if cmd == "/sessions":
            return await self.send_sessions(chat, tg, role)
        if cmd == "/logout":
            n = self.acc.revoke_all(tg)
            self.acc.log(tg, "logout", f"вышел везде ({n})")
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
            await self.set_commands(f["id"])
            return await self.remote(chat, f["id"])
        await self.refuse(chat, f)

    # ---- buttons
    async def on_callback(self, q: dict):
        chat, msg, f, data = q["message"]["chat"]["id"], q["message"]["message_id"], q["from"], q.get("data", "")
        name, username = self.who(f)

        async def done(text, toast=""):
            await self.call("answerCallbackQuery", callback_query_id=q["id"], text=toast)
            await self.edit(chat, msg, text)

        k, rest = data[:1], data[1:]
        tg = f["id"]
        if k in "PCUVM" and data:                                            # the remote, the stations, the plugins' pages, the people: people of the server only
            if not self.acc.role_of(tg):
                return await self.call("answerCallbackQuery", callback_query_id=q["id"], text="Нет доступа", show_alert=True)
            if k == "P":
                return await self.on_remote(q, chat, msg, tg, rest)
            if k == "C":
                return await self.on_station_pick(q, chat, msg, tg, rest)
            if k == "U":
                return await self.on_page(q, chat, msg, tg, rest)
            if k == "V":
                return await self.on_page_action(q, chat, msg, tg, rest)
            return await self.on_people(q, chat, msg, tg, rest)
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
            await self.send(t["by_tg"], f"<b>{esc(name)}</b> принял(а) ваше приглашение в lostOS.")   # the one who invited, not the operator of the server
            return
        if k == "S":                                                         # S-<id>: remove a station (only the one it belongs to, or the owner of the server)
            link = self.ctx.link
            role = self.acc.role_of(f["id"])
            r = link.stations.get(rest[1:]) if link else None
            if not r or not role or not link.visible_to(r, f["id"], role, self.acc.tenant_of(f["id"])):   # only a station of his own household
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
            if not role:
                return await self.call("answerCallbackQuery", callback_query_id=q["id"])
            # the owner of a household ends the sessions of his household, a member only his own; nobody those of another household
            ok = self.acc.revoke(rest, tenant=self.acc.tenant_of(f["id"])) if role == "owner" else self.acc.revoke(rest, f["id"])
            await self.call("answerCallbackQuery", callback_query_id=q["id"], text="Сессия завершена" if ok else "Не найдено")
            return await self.send_sessions(chat, f["id"], role)
        if k == "X":                                                         # "this was not me" under a login notice
            self.acc.revoke_all(f["id"])
            return await done("Все сессии завершены. Если вы не входили, смените доступ: владелец может выпустить новую одноразовую ссылку командой lostos-link.")
        await self.call("answerCallbackQuery", callback_query_id=q["id"])

    async def send_stations(self, chat: int, tg_id: int, role: str):
        rows = self.ctx.link.listing(tg_id, role, tenant=self.acc.tenant_of(tg_id)) if self.ctx.link else []   # his household only
        if not rows:
            return await self.send(chat, "Колонок пока нет. Подключить: /connect и идентификатор, который показывает установщик")
        for s in rows:
            state = ("В сети" if s["online"] else "Не в сети") if s["confirmed"] else "ждёт нажатия кнопки Алисы"
            await self.send(chat, f"<b>{esc(s['name'] or 'Колонка')}</b>\n<code>{esc(s['id'])}</code>\n{state}", [[("Убрать", f"S-{s['id']}")]])

    async def send_sessions(self, chat: int, tg_id: int, role: str):
        rows = self.acc.sessions_of(tenant=self.acc.tenant_of(tg_id)) if role == "owner" else self.acc.sessions_of(tg_id)   # his household / himself
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

    # ---- roles and their menus
    def roles(self, tg: int):
        """-> (role in the household: "owner" | "member" | "", operator of the server)"""
        u = self.acc.user(tg)
        return self.acc.role_of(tg), bool(u and u.get("admin"))

    COMMANDS = {
        "member": (("start", "пульт колонки"), ("plugins", "страницы плагинов"), ("next", "следующий трек"), ("prev", "предыдущий трек"), ("pause", "пауза"),
                   ("play", "продолжить"), ("volume", "громкость, например /volume 30"), ("stations", "мои колонки"), ("sessions", "где вы вошли"), ("logout", "выйти везде"),
                   ("help", "что я умею")),
        "owner": (("people", "люди дома и приглашения"), ("connect", "подключить колонку по идентификатору")),
        "admin": (("requests", "заявки на доступ"), ("server", "состояние сервера")),
    }

    async def set_commands(self, tg: int, force: bool = False):
        """The menu of commands of this chat follows the person's role: a member does not see the owner's commands, nobody the operator's."""
        role, admin = self.roles(tg)
        key = (role, admin)
        if not force and self.menus.get(tg) == key:
            return
        self.menus[tg] = key
        cmds = list(self.COMMANDS["member"]) if role else [("start", "начать")]
        if role == "owner":
            cmds += self.COMMANDS["owner"]
        if admin:
            cmds += self.COMMANDS["admin"]
        await self.call("setMyCommands", commands=[{"command": c, "description": d} for c, d in cmds], scope={"type": "chat", "chat_id": tg})

    def help_text(self, tg: int) -> str:
        role, admin = self.roles(tg)
        lines = ["<b>lostOS</b>: колонка из кармана.", "", "/start: пульт (что играет, кнопки ⏮ ⏯ ⏭ и громкость)", "/plugins: страницы плагинов (радио и другие)",
                 "/next /prev /pause /play /volume 30", "/stations: колонки дома", "/sessions: где вы вошли, /logout: выйти везде"]
        if role == "owner":
            lines += ["", "Владелец дома:", "/people: кто в доме, пригласить и убрать", "/connect: подключить колонку по идентификатору"]
        if admin:
            lines += ["", "Оператор сервера:", "/requests: заявки на доступ", "/server: дома, места, колонки на связи"]
        return "\n".join(lines)

    # ---- the remote: one message that is edited in place
    async def remote(self, chat: int, tg: int, msg: int = None):
        stations = self.ctx.stations(tg) if hasattr(self.ctx, "stations") else []
        if hasattr(self.ctx, "stations") and not stations:
            text, rows = "Колонок пока нет. Подключить: /connect и идентификатор, который показывает установщик", []
        else:
            try:
                st = await self.ctx.status(tg, self.sel.get(tg, ""))
            except Exception as e:
                st = {"online": False, "now": "", "error": str(e)}
            vol = st.get("volume")
            text = (f"<b>{esc(st.get('name') or 'Колонка')}</b> · {'в сети' if st.get('online') else 'не в сети'}\n"
                    f"🎵 {esc(st.get('now') or 'ничего не играет')}" + (f"\n🔊 {vol}%" if vol is not None else ""))
            rows = [[("⏮", "Pprevious"), ("⏯", "Pplaypause"), ("⏭", "Pnext")], [("🔉 −10", "Pdown"), ("🔊 +10", "Pup"), ("🔄", "Pref")]]
            extra = [("🧩 Плагины", "Ul")]
            if len(stations) > 1:
                extra.insert(0, ("Колонка ▾", "Cl"))
            rows.append(extra)
        if msg:
            return await self.edit(chat, msg, text, rows)
        return await self.send(chat, text, rows)

    async def on_remote(self, q: dict, chat: int, msg: int, tg: int, act: str):
        """P<action> under the remote: the player of the chosen station, then the remote is drawn again."""
        if act in ("previous", "next", "playpause"):
            try:
                await self.ctx.media_command(tg, act, {}, sid=self.sel.get(tg, ""))
                note = {"previous": "Назад", "next": "Дальше", "playpause": "Пауза / продолжить"}[act]
            except Exception as e:
                note = f"Не получилось: {e}"[:180]
            await self.call("answerCallbackQuery", callback_query_id=q["id"], text=note)
            await asyncio.sleep(0.8)                                       # the speaker reports the new track a moment later
        elif act in ("up", "down"):
            st = await self.ctx.status(tg, self.sel.get(tg, ""))
            level = max(0, min(100, int(st.get("volume") or 30) + (10 if act == "up" else -10)))
            try:
                await self.ctx.media_command(tg, "volume", {"level": level}, sid=self.sel.get(tg, ""))
                note = f"Громкость {level}"
            except Exception as e:
                note = f"Не получилось: {e}"[:180]
            await self.call("answerCallbackQuery", callback_query_id=q["id"], text=note)
            await asyncio.sleep(0.4)
        else:
            await self.call("answerCallbackQuery", callback_query_id=q["id"])
        await self.remote(chat, tg, msg)

    async def on_station_pick(self, q: dict, chat: int, msg: int, tg: int, rest: str):
        """Cl: the list of the household's stations; C=<id>: that one from now on (in this bot)."""
        stations = self.ctx.stations(tg)
        if rest.startswith("="):
            if any(s["id"] == rest[1:] for s in stations):
                self.sel[tg] = rest[1:]
            await self.call("answerCallbackQuery", callback_query_id=q["id"])
            return await self.remote(chat, tg, msg)
        await self.call("answerCallbackQuery", callback_query_id=q["id"])
        cur = self.ctx.station_of(tg, self.sel.get(tg, ""))
        rows = [[(("✓ " if s["id"] == cur else "") + s["name"] + ("" if s["online"] else " (не в сети)"), f"C={s['id']}")] for s in stations[:20]]
        await self.edit(chat, msg, "Какой колонкой управлять?", rows + [[("← Пульт", "Pref")]])

    # ---- the plugins' own pages (the same panel_ui the panel draws, see docs/plugins.md)
    async def plugins_menu(self, chat: int, tg: int, msg: int = None):
        try:
            pages = [p for p in await self.ctx.plugin_ui(tg, self.sel.get(tg, "")) if p.get("page")]
        except Exception as e:
            pages, err = [], str(e)
        else:
            err = ""
        self.plug_lists[chat] = [p["id"] for p in pages]
        text = "Страницы плагинов:" if pages else (f"Не получилось: {esc(err)}" if err else "У плагинов этой колонки нет своих страниц")
        rows = [[(p["page"]["title"], f"Uo{i}")] for i, p in enumerate(pages[:20])] + [[("← Пульт", "Pref")]]
        if msg:
            return await self.edit(chat, msg, text, rows)
        return await self.send(chat, text, rows)

    def page_view(self, chat: int, pid: str, d: dict):
        """A plugin's answer as one message: the texts and the titles of the lists as text, every row, button and switch as a button."""
        acts, lines, rows = [], [f"<b>{esc(d.get('title') or 'Плагин')}</b>"], []
        for b in d.get("blocks") or []:
            t = b.get("type")
            if t == "text":
                lines.append(esc(b.get("text")))
            elif t == "search":
                acts.append(("search", b.get("id", ""), ""))
                rows.append([("🔎 " + (b.get("placeholder") or "Поиск")[:40], f"V{len(acts) - 1}")])
            elif t == "list":
                if b.get("title"):
                    lines.append(f"\n<b>{esc(b['title'])}</b>: кнопки ниже")
                if not b.get("items"):
                    lines.append(esc(b.get("empty") or "Пусто"))
                for it in (b.get("items") or [])[:12]:
                    acts.append(("select", b.get("id", ""), it.get("id", "")))
                    rows.append([(("✓ " if it.get("on") else "") + (it.get("title") or "—")[:48], f"V{len(acts) - 1}")])
            elif t == "buttons":
                row = []
                for x in b.get("items") or []:
                    acts.append(("press", x.get("id", ""), ""))
                    row.append(((x.get("title") or "—")[:30], f"V{len(acts) - 1}"))
                if row:
                    rows.append(row)
            elif t == "toggle":
                acts.append(("toggle", b.get("id", ""), "0" if b.get("on") else "1"))
                rows.append([(f"{(b.get('title') or '—')[:40]}: {'вкл' if b.get('on') else 'выкл'}", f"V{len(acts) - 1}")])
        rows.append([("← Плагины", "Ul"), ("Пульт", "Pref")])
        self.pages[chat] = {"pid": pid, "acts": acts}
        return "\n".join(lines)[:3900], rows[:60]

    async def page_event(self, chat: int, tg: int, pid: str, event: str, bid: str = "", value: str = "", msg: int = None) -> str:
        d = await self.ctx.plugin_ui(tg, self.sel.get(tg, ""), pid, {"view": "page", "event": event, "id": bid, "value": value})
        if not d.get("ok"):
            if msg is None:
                await self.send(chat, f"Не получилось: {esc(d.get('error') or 'плагин не ответил')}")
            return d.get("error") or "Не получилось"
        if d.get("blocks"):
            text, rows = self.page_view(chat, pid, d)
            if msg:
                await self.edit(chat, msg, text, rows)
            else:
                await self.send(chat, text, rows)
        return d.get("toast") or ""

    async def on_page(self, q: dict, chat: int, msg: int, tg: int, rest: str):
        """Ul: the list of pages, Uo<n>: open one, V<n>: a row, a button or a switch of the page on the screen."""
        if rest == "l":
            await self.call("answerCallbackQuery", callback_query_id=q["id"])
            return await self.plugins_menu(chat, tg, msg)
        pl = self.plug_lists.get(chat) or []
        if rest.startswith("o"):
            try:
                pid = pl[int(rest[1:])]
            except (ValueError, IndexError):
                return await self.call("answerCallbackQuery", callback_query_id=q["id"], text="Список устарел, откройте /plugins снова")
            await self.call("answerCallbackQuery", callback_query_id=q["id"])
            await self.page_event(chat, tg, pid, "open", msg=msg)
            return
        await self.call("answerCallbackQuery", callback_query_id=q["id"])

    async def on_page_action(self, q: dict, chat: int, msg: int, tg: int, n: str):
        page = self.pages.get(chat)
        try:
            event, bid, value = page["acts"][int(n)]
        except (TypeError, ValueError, IndexError, KeyError):
            return await self.call("answerCallbackQuery", callback_query_id=q["id"], text="Страница устарела, откройте её снова", show_alert=True)
        if event == "search":
            self.wait_search[tg] = (chat, page["pid"], bid, msg)
            await self.call("answerCallbackQuery", callback_query_id=q["id"])
            return await self.send(chat, "Напишите, что найти")
        toast = await self.page_event(chat, tg, page["pid"], event, bid, value, msg)
        await self.call("answerCallbackQuery", callback_query_id=q["id"], text=toast[:190])

    # ---- the people of a household
    async def send_people(self, chat: int, tg: int, msg: int = None):
        tid = self.acc.tenant_of(tg)
        rows, lines = [], []
        for u in self.acc.users(tid):
            mark = "владелец" if u["role"] == "owner" else "участник"
            lines.append(f"• <b>{esc(u['name'])}</b>" + (f" (@{esc(u['username'])})" if u.get("username") else "") + f": {mark}")
            if u["role"] != "owner":
                rows.append([(f"Убрать {u['name'][:24]}", f"M-{u['tg_id']}")])
        rows.append([("Пригласить человека", "Mi")])
        text = "Люди дома:\n" + ("\n".join(lines) or "пока только вы")
        if msg:
            return await self.edit(chat, msg, text, rows)
        return await self.send(chat, text, rows)

    async def on_people(self, q: dict, chat: int, msg: int, tg: int, rest: str):
        if self.acc.role_of(tg) != "owner":
            return await self.call("answerCallbackQuery", callback_query_id=q["id"], text="Это может только владелец дома", show_alert=True)
        if rest == "i":
            raw = self.acc.new_token("invite", by_tg=tg)
            await self.call("answerCallbackQuery", callback_query_id=q["id"])
            if not raw:
                return await self.send(chat, "Слишком много открытых приглашений, попробуйте позже")
            return await self.send(chat, f"Ссылка-приглашение в ваш дом (действует сутки, один раз):\nhttps://t.me/{self.username}?start=i{raw}\n\n"
                                         "Перешлите её человеку. Кто откроет и согласится, станет участником: музыка и управление колонкой, без ключей и настроек")
        if rest.startswith("-"):
            try:
                who = int(rest[1:])
            except ValueError:
                who = 0
            ok = self.acc.remove_user(who, tenant=self.acc.tenant_of(tg))       # only a member of HIS household
            await self.call("answerCallbackQuery", callback_query_id=q["id"], text="Убран" if ok else "Не найден")
            return await self.send_people(chat, tg, msg)
        await self.call("answerCallbackQuery", callback_query_id=q["id"])

    async def send_server(self, chat: int):
        link = self.ctx.link
        online = len(link.online) if link else 0
        total = len([1 for r in (link.stations.values() if link else []) if r.get("confirmed")])
        homes = len(self.acc.tenants()) if hasattr(self.acc, "tenants") else 0
        await self.send(chat, f"<b>Сервер</b>\nДомов: {homes}, свободных мест: {self.ctx.capacity_left()}\nКолонок на связи: {online} из {total}\n"
                              f"Регистрация: {esc(self.ctx.registration())}\nЗаявок ждёт: {len(self.acc.pending_requests())}")

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
