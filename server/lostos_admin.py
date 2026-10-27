"""The administration of a lostOS server for many people. It is NOT a part of the panel: you run it on the server (ssh), as the user of the service.

  lostos-admin status                       who has a server process, how much memory it takes and what its limit is
  lostos-admin people                       everybody with an account and the household he belongs to
  lostos-admin requests                     people who asked for access
  lostos-admin approve <telegram id>        let a person in (he gets a server process of his own at the first visit)
  lostos-admin deny <telegram id>
  lostos-admin ram <telegram id> <MB>       memory limit of one person's process (e.g. 300); `ram default <MB>` for everybody who has no own value
  lostos-admin max <number>                 how many people may have a process at once
  lostos-admin idle <minutes>               a process nobody uses (no speaker connected) is stopped after so many minutes
  lostos-admin registration open|approval|invite   open = anybody who asks the bot gets in; approval = closed, by requests; invite = closed, only by a link
  lostos-admin invite                       a link that lets one new person in (valid a week; the person opens it in Telegram and accepts)
  lostos-admin restart <telegram id>        restart the process of a person (the gateway does it within 5 seconds)
  lostos-admin remove <telegram id>         remove a person and his household's people (the files of the household stay in data/tenants/<id> until you delete them)
  lostos-admin developers                   the verified developers (plugapi.losted.dev), the revoked keys and how long the list is valid
  lostos-admin developers add <id> "<name>" <public key>     verify a developer (the key is what he sent you; the list is signed and published at once, servers take it within seconds)
  lostos-admin developers remove <id>       the developer is not verified any more (his key is not revoked)
  lostos-admin developers revoke <fingerprint> [why]         revoke a key: every server stops trusting it within seconds and switches the plugins signed with it off
  lostos-admin servers                      the servers that the installer knows and does not warn about (published on plugapi as hashes of their hosts)
  lostos-admin servers add <address> "<name>"    a server joins the list (the installer shows the name when a person types this address)
  lostos-admin servers remove <name|hash>   the server leaves the list
  lostos-admin servers ban <address> "<reason>"      the installer REFUSES this server and tells the reason (a server of the list leaves it)
  lostos-admin servers unban <hash|reason>          the server is not banned any more
  lostos-admin developers init              once: the key that signs the list (it stays on this server); prints its public part for trusted_keys.json
  lostos-admin developers refresh           signs the list again when it runs low (the daily timer does it)
Settings are in data/tenants.json, which the gateway reads again every few seconds: no restart is needed.
Run:  LOSTOS_DATA=/srv/lostos/data python lostos_admin.py status"""
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
DATA = os.environ.get("LOSTOS_DATA", HERE)
CFG = os.path.join(DATA, "tenants.json")
DEFAULTS = {"default_mb": 200, "max_tenants": 10, "idle_minutes": 20, "registration": "approval", "overrides": {}}


def load_cfg() -> dict:
    try:
        return {**DEFAULTS, **json.load(open(CFG, encoding="utf-8"))}
    except (OSError, ValueError):
        return dict(DEFAULTS)


def save_cfg(c: dict):
    tmp = CFG + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(c, f, ensure_ascii=False, indent=1)
    os.replace(tmp, CFG)


def accounts():
    import accounts as a
    return a.Accounts(os.path.join(DATA, "accounts.db"))


def need_int(args, i, what):
    try:
        return int(args[i])
    except (IndexError, ValueError):
        sys.exit(f"нужно число: {what}")


def main(argv) -> int:
    cmd = argv[0] if argv else ""
    c = load_cfg()
    if cmd == "status":
        try:
            st = json.load(open(os.path.join(DATA, "tenants_status.json"), encoding="utf-8"))
        except (OSError, ValueError):
            st = {"ts": 0, "tenants": []}
        age = int(time.time()) - st["ts"]
        print(f"шлюз {'работает' if st['ts'] and age < 30 else 'не отвечает (или не запущен)'}; людей с процессом: {len(st['tenants'])} из {c['max_tenants']}; память по умолчанию {c['default_mb']} МБ; регистрация: {c['registration']}")
        people = {p["tg_id"]: p for p in accounts().tenants()}
        for t in st["tenants"]:
            p = people.get(t["id"], {})
            print(f"  {t['id']:>12}  {p.get('name', '?'):<20} {(t.get('station') or 'без колонки')[:8]:<10} {'работает' if t['running'] else 'остановлен':<10} {t['rss_mb']:>4} / {t['limit_mb']} МБ, пик {t.get('peak_mb', 0)}  убито стеной {t.get('wall_kills', 0)}  перезапусков {t['restarts']}  {t['uptime'] // 60} мин")
        return 0
    if cmd == "people":
        for u in accounts().users():
            print(f"  {u['tg_id']:>12}  {u['name']:<20} {u['role']:<7} {'оператор ' if u.get('admin') else ''}дом {u.get('tenant')}")
        return 0
    if cmd == "requests":
        rows = accounts().pending_requests()
        for r in rows:
            print(f"  {r['tg_id']:>12}  {r['name']}  @{r['username']}  {time.strftime('%d.%m %H:%M', time.localtime(r['ts']))}")
        print("заявок нет" if not rows else "")
        return 0
    if cmd in ("approve", "deny"):
        who = need_int(argv, 1, "telegram id")
        if cmd == "approve" and len(accounts().tenants()) >= c["max_tenants"]:
            print(f"предупреждение: людей уже {len(accounts().tenants())} при максимуме {c['max_tenants']}; поднимите: lostos-admin max <число>")
        r = accounts().decide(who, cmd == "approve")
        print(("одобрено: " if cmd == "approve" else "отклонено: ") + r["name"] if r else "такой открытой заявки нет")
        return 0 if r else 1
    if cmd == "ram":
        if len(argv) >= 3 and argv[1] == "default":
            c["default_mb"] = need_int(argv, 2, "МБ")
        else:
            c["overrides"][str(need_int(argv, 1, "telegram id"))] = need_int(argv, 2, "МБ")
        save_cfg(c)
        print("готово, шлюз подхватит за несколько секунд")
        return 0
    if cmd in ("max", "idle"):
        c["max_tenants" if cmd == "max" else "idle_minutes"] = need_int(argv, 1, "число")
        save_cfg(c)
        print("готово")
        return 0
    if cmd == "registration":
        word = {"closed": "invite"}.get(argv[1] if len(argv) > 1 else "", argv[1] if len(argv) > 1 else "")      # the old word "closed" means "invite"
        if word not in ("open", "approval", "invite"):
            sys.exit("open (открыта всем), approval (закрыта, по заявкам) или invite (закрыта, только по ссылке)")
        c["registration"] = word
        save_cfg(c)
        print("готово")
        return 0
    if cmd == "invite":
        raw = accounts().new_token("reg", by_tg=0)
        if not raw:
            sys.exit("слишком много открытых запросов, подождите")
        try:
            bot = open(os.path.join(DATA, "bot_username.txt"), encoding="utf-8").read().strip()
        except OSError:
            bot = ""
        print(f"https://t.me/{bot}?start=r{raw}" if bot else f"бот ещё не назвал себя; код: r{raw} (ссылка: https://t.me/<бот>?start=r{raw})")
        print("действует неделю и один раз; человек открывает её в Telegram и нажимает «Принять»")
        return 0
    if cmd == "restart":
        folder = os.path.join(DATA, "tenants", str(need_int(argv, 1, "telegram id")))
        if not os.path.isdir(folder):
            sys.exit("у этого человека ещё нет папки")
        open(os.path.join(folder, "restart"), "w").close()
        print("перезапуск заказан")
        return 0
    if cmd == "remove":
        who = need_int(argv, 1, "telegram id")
        a = accounts()
        u = a.user(who)
        if not u:
            sys.exit("такого человека нет")
        if u.get("admin"):
            sys.exit("оператора удалить нельзя")
        for m in a.users(who):
            a.remove_user(m["tg_id"], who)
        a.remove_user(who)
        print("удалён; папку data/tenants/%d удалите сами, когда убедитесь, что она не нужна" % who)
        return 0
    if cmd in ("servers", "developers"):
        import edition
        if edition.edition(DATA) != "center":
            sys.exit(edition.CENTER_ONLY + " (в файле " + os.path.join(DATA, "edition") + " должно быть слово center)")
    if cmd == "servers":
        import devlist
        dl = devlist.DevList(DATA)
        sub = argv[1] if len(argv) > 1 else ""
        try:
            if sub == "add" and len(argv) == 4:
                print("added", argv[3], dl.add_server(argv[2], argv[3])[:16])
                return 0
            if sub == "remove" and len(argv) == 3:
                dl.remove_server(argv[2])
                print("removed", argv[2])
                return 0
            if sub == "ban" and len(argv) == 4:
                print("banned", dl.ban_server(argv[2], argv[3])[:16])
                return 0
            if sub == "unban" and len(argv) == 3:
                dl.unban_server(argv[2])
                print("unbanned", argv[2])
                return 0
            if sub in ("", "list") and len(argv) <= 2:
                for x in dl.state().get("servers", []):
                    print(f"  {x['name']:30} {x['h'][:16]}…")
                for x in dl.state().get("banned", []):
                    print(f"  BANNED {x['h'][:16]}… {x['why']}")
                return 0
        except (ValueError, FileNotFoundError, OSError) as e:
            sys.exit(f"{e}")
        sys.exit(__doc__)
    if cmd == "developers":
        import devlist
        import plugin_sign
        dl = devlist.DevList(DATA)
        sub = argv[1] if len(argv) > 1 else ""
        try:
            if sub == "init":
                print(dl.init())
                print("^ the public key: put it into server/trusted_keys.json as {\"publisher\": \"losted.dev\", \"key\": ..., \"scope\": \"developers\"} and deploy the server")
                return 0
            if sub == "add" and len(argv) == 5:
                print("verified", argv[2], plugin_sign.fingerprint(argv[4]) if dl.add(argv[2], argv[3], argv[4]) else "")
                return 0
            if sub == "remove" and len(argv) == 3:
                dl.remove(argv[2])
                print("removed", argv[2])
                return 0
            if sub == "revoke" and len(argv) >= 3:
                dl.revoke(argv[2], " ".join(argv[3:]))
                print("revoked", argv[2])
                return 0
            if sub == "refresh" and len(argv) == 2:
                print("signed again" if dl.refresh() else f"fine, {dl.days_left():.1f} days left")
                return 0
            if sub in ("", "list") and len(argv) <= 2:
                d = dl.state()
                print(f"serial {d.get('serial', 0)}, valid for {dl.days_left():.1f} more days")
                for x in d["developers"]:
                    print(f"  {x['id']:20} {x['name']:24} {plugin_sign.fingerprint(x['key'])}")
                for r in dl.state().get("revoked", []):
                    print("  revoked", r["fingerprint"] if isinstance(r, dict) else r, r.get("why", "") if isinstance(r, dict) else "")
                return 0
        except (ValueError, FileNotFoundError, OSError) as e:
            sys.exit(f"{e}")
        sys.exit(__doc__)
    print(__doc__)
    return 0 if cmd in ("", "help", "-h", "--help") else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
