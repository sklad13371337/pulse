#!/usr/bin/env python3
"""Пульс базы: забирает успешные сделки из AmoCRM и отдаёт страницу с данными.

Только стандартная библиотека Python 3.8+. Запуск:  python pulse.py
Настройки: config.json рядом со скриптом (образец: config.example.json).
"""
import base64
import datetime
import gzip
import hmac
import secrets
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(HERE, "amo_data.json")   # снимок из AmoCRM
SENT_FILE = os.path.join(HERE, "sent.json")       # что мы уже отправляли: {телефон: [номера дней]}
BATCH_FILE = os.path.join(HERE, "batches.json")   # журнал пачек: шаблон, получатели, контрольная группа
DNC_FILE = os.path.join(HERE, "dnc.json")         # список «не писать»: {телефон: {"d": день, "note": текст}}
LOCK = threading.Lock()
CACHE = {}


def load_cfg():
    with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
        return json.load(f)


def log(*a):
    print(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


# ---------------------------------------------------------------- AmoCRM
def amo(cfg, path, params=None):
    url = "https://%s.amocrm.ru/api/v4%s" % (cfg["subdomain"], path)
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    for attempt in range(5):
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + cfg["token"]})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                body = r.read()
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as e:
            if e.code == 429:           # слишком часто, ждём
                time.sleep(2 * (attempt + 1))
                continue
            if e.code == 204:
                return {}
            raise
        finally:
            time.sleep(0.2)             # лимит AmoCRM: 7 запросов в секунду
    raise RuntimeError("AmoCRM не отвечает: " + url)


def amo_post(cfg, path, payload):
    """Запись в AmoCRM (нужны права токена на запись). Возвращает (код, ответ)."""
    url = "https://%s.amocrm.ru/api/v4%s" % (cfg["subdomain"], path)
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Authorization": "Bearer " + cfg["token"], "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read()
            return r.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode("utf-8", "replace")[:300]}
    finally:
        time.sleep(0.3)


def write_notes(cfg, items):
    """items: [{id: 'префикс-123', lead: 456, text: '...'}] -> примечания в историю карточки сделки (или контакта, если сделки нет)."""
    ok = bad = 0
    err = ""
    leads, conts = [], []
    for it in items[:300]:
        txt = str(it.get("text", ""))[:3000]
        lid = str(it.get("lead") or "")
        cid = str(it.get("id", "")).rsplit("-", 1)[-1]
        if txt and lid.isdigit():
            leads.append({"entity_id": int(lid), "note_type": "common", "params": {"text": txt}})
        elif txt and cid.isdigit():
            conts.append({"entity_id": int(cid), "note_type": "common", "params": {"text": txt}})
        else:
            bad += 1
    for path, notes in (("/leads/notes", leads), ("/contacts/notes", conts)):
        for i in range(0, len(notes), 50):
            code, resp = amo_post(cfg, path, notes[i:i + 50])
            if code in (200, 201):
                ok += len(notes[i:i + 50])
            else:
                bad += len(notes[i:i + 50])
                err = "AmoCRM %s: %s" % (code, resp.get("error", ""))
                return ok, bad, err
    return ok, bad, err


def pages(cfg, path, params, key):
    page = 1
    while True:
        p = dict(params)
        p["page"] = page
        p["limit"] = 250
        data = amo(cfg, path, p)
        items = (data.get("_embedded") or {}).get(key) or []
        for it in items:
            yield it
        if not (data.get("_links") or {}).get("next"):
            break
        page += 1


def field_values(entity, field_id=None, code=None):
    for f in entity.get("custom_fields_values") or []:
        if (field_id and f.get("field_id") == field_id) or (code and f.get("field_code") == code):
            return [v.get("value") for v in f.get("values") or []]
    return []


def truthy(vals):
    """Поле отписки может быть флажком, датой или текстом: любое непустое значение считаем отпиской."""
    return any(v not in (None, False, "", 0, "0") for v in vals)


FLOWERS = [("роз", "Роза"), ("тюльпан", "Тюльпан"), ("пион", "Пион"), ("хризантем", "Хризантема"),
           ("гвоздик", "Гвоздика"), ("лили", "Лилия"), ("гербер", "Гербера"), ("ирис", "Ирис"),
           ("орхиде", "Орхидея"), ("гортензи", "Гортензия"), ("эустом", "Эустома"), ("альстромер", "Альстромерия"),
           ("подсолнух", "Подсолнух"), ("ранункул", "Ранункулюс"), ("гипсофил", "Гипсофила"), ("мимоз", "Мимоза"),
           ("нарцисс", "Нарцисс"), ("гиацинт", "Гиацинт"), ("фрези", "Фрезия"), ("кустов", "Кустовая роза"),
           ("пионовидн", "Пионовидная роза"), ("букет", "Букет (без состава)"), ("корзин", "Корзина"),
           ("композиц", "Композиция")]


def parse_items(text):
    """Названия цветов из свободного текста: «большой букет роз» -> [Роза, Букет (без состава)]."""
    t = (text or "").lower()
    found = []
    for stem, name in FLOWERS:
        if re.search(r"(?<![а-яё])" + stem, t) and name not in found:
            found.append(name)
    if len(found) > 1 and "Букет (без состава)" in found:
        found.remove("Букет (без состава)")
    return found


def load_notes(cfg, lead_ids):
    """Тексты примечаний к сделкам одним потоком запросов: {id сделки: текст}. Ошибка не ломает синхронизацию."""
    notes = {}
    try:
        for i in range(0, len(lead_ids), 50):
            for n in pages(cfg, "/leads/notes", {"filter[entity_id][]": lead_ids[i:i + 50], "filter[note_type]": "common"}, "notes"):
                t = (n.get("params") or {}).get("text") or ""
                if "Сообщение пришло из источника" in t:
                    continue
                notes[n["entity_id"]] = (notes.get(n["entity_id"], "") + " " + t).strip()
    except Exception as e:
        log("  примечания не загрузились:", e)
    return notes


def norm_phone(s):
    d = re.sub(r"\D", "", str(s or ""))
    if len(d) == 11 and d[0] == "8":
        d = "7" + d[1:]
    if len(d) == 10:
        d = "7" + d
    return d


def sync(cfg):
    log("Синхронизация с AmoCRM: старт")
    pls = (amo(cfg, "/leads/pipelines").get("_embedded") or {}).get("pipelines") or []
    wanted = {}
    for name in cfg["funnels"]:
        hit = [p for p in pls if name.lower() in p["name"].lower()]
        if not hit:
            log("  воронка не найдена:", name, "| есть:", [p["name"] for p in pls])
            continue
        wanted[name] = hit[0]["id"]
    since = int(datetime.datetime.strptime(cfg.get("since_date", "2022-01-01"), "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc).timestamp())
    out = {}
    leads_out = {}
    for fname, pid in wanted.items():
        leads = list(pages(cfg, "/leads", {
            "filter[statuses][0][pipeline_id]": pid,
            "filter[statuses][0][status_id]": 142,           # «Успешно реализовано»
            "filter[closed_at][from]": since,
            "with": "contacts"}, "leads"))
        log("  %s: сделок %d" % (fname, len(leads)))
        cids = sorted({c["id"] for l in leads for c in (l.get("_embedded") or {}).get("contacts") or [] if c.get("is_main")})
        contacts = {}
        for i in range(0, len(cids), 50):
            for c in pages(cfg, "/contacts", {"filter[id][]": cids[i:i + 50]}, "contacts"):
                contacts[c["id"]] = c
        notes = load_notes(cfg, [l["id"] for l in leads]) if cfg.get("parse_notes", True) else {}
        log("  %s: примечаний с текстом %d" % (fname, len(notes)))
        clients = {}
        prefix = fname[:2].upper()
        for l in leads:
            main = [c for c in (l.get("_embedded") or {}).get("contacts") or [] if c.get("is_main")]
            if not main or not l.get("closed_at") or not l.get("price"):
                continue
            c = contacts.get(main[0]["id"])
            if not c:
                continue
            ph = norm_phone((field_values(c, code="PHONE") or [""])[0])
            key = ph or "c%s" % c["id"]
            ids = cfg.get("items_field_ids") or ([cfg["items_field_id"]] if cfg.get("items_field_id") else [])
            if ids:
                txt = " ; ".join(str(x) for i in ids for x in field_values(l, i) if x)
                names = [m.strip() for m in re.findall(r"\d+\.\s*([^/]+?),\s*\d", txt)]
                items = parse_items(txt) or names[:3] or ([txt[:60]] if txt else ["Без состава"])
            else:
                items = parse_items((l.get("name") or "") + " " + notes.get(l["id"], "")) or ["Без состава"]
            cl = clients.setdefault(key, {
                "id": "%s-%s" % (prefix, c["id"]), "name": c.get("name") or "Без имени", "phone": ph,
                "consent": bool(cfg.get("assume_consent", False)), "optout": False, "complaint": False,
                "noWa": False, "lastMsg": None, "msgs30": 0, "orders": []})
            if cfg.get("consent_field_id"):
                cl["consent"] = any(v is True for v in field_values(c, cfg["consent_field_id"]))
            if cfg.get("optout_field_id"):
                cl["optout"] = truthy(field_values(c, cfg["optout_field_id"]))
            cl["orders"].append({"d": l["closed_at"] // 86400, "sum": l["price"], "items": [i for i in items if i], "l": l["id"]})
        out[fname] = list(clients.values())
        log("  %s: клиентов %d" % (fname, len(out[fname])))
        # обращения без покупки (для раздела «Лиды»): открытые и закрытые без реализации
        try:
            open_l = [l for l in pages(cfg, "/leads", {
                "filter[pipeline_id]": pid, "filter[created_at][from]": since, "with": "contacts"}, "leads")
                if l.get("status_id") != 142]
            need = sorted({c["id"] for l in open_l for c in (l.get("_embedded") or {}).get("contacts") or []
                           if c.get("is_main")} - set(contacts))
            for i in range(0, len(need), 50):
                for c in pages(cfg, "/contacts", {"filter[id][]": need[i:i + 50]}, "contacts"):
                    contacts[c["id"]] = c
            pros = {}
            for l in open_l:
                main = [c for c in (l.get("_embedded") or {}).get("contacts") or [] if c.get("is_main")]
                c = contacts.get(main[0]["id"]) if main else None
                if not c or not l.get("created_at"):
                    continue
                ph = norm_phone((field_values(c, code="PHONE") or [""])[0])
                key = ph or "c%s" % c["id"]
                r = pros.setdefault(key, {
                    "id": "%s-%s" % (prefix, c["id"]), "name": c.get("name") or "Без имени", "phone": ph,
                    "consent": bool(cfg.get("assume_consent", False)), "optout": False, "leads": []})
                if cfg.get("consent_field_id"):
                    r["consent"] = any(v is True for v in field_values(c, cfg["consent_field_id"]))
                if cfg.get("optout_field_id"):
                    r["optout"] = truthy(field_values(c, cfg["optout_field_id"]))
                r["leads"].append({"d": l["created_at"] // 86400, "s": "l" if l.get("status_id") == 143 else "o"})
            leads_out[fname] = list(pros.values())
            log("  %s: контактов с обращениями без покупки %d" % (fname, len(leads_out[fname])))
        except Exception as e:      # лиды не должны ломать основную синхронизацию
            log("  лиды не загружены:", repr(e))
    snap = {"generated": datetime.datetime.now().strftime("%d.%m.%Y %H:%M"), "funnels": out, "leads": leads_out}
    with LOCK:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False)
    log("Синхронизация завершена")


def sync_loop(cfg):
    while True:
        try:
            sync(cfg)
        except Exception as e:      # не роняем сервер из-за сети или токена
            log("ОШИБКА синхронизации:", repr(e))
        time.sleep(float(cfg.get("sync_hours", 6)) * 3600)


# ---------------------------------------------------------------- данные для страницы
def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def merged_data():
    snap = read_json(DATA_FILE, {"generated": "", "funnels": {}})
    sent = read_json(SENT_FILE, {})
    today = int(time.time() // 86400)
    for clients in snap["funnels"].values():
        for c in clients:
            days = sent.get(c["phone"]) or []
            if days:
                c["lastMsg"] = max(days)
                c["msgs30"] = len([d for d in days if d >= today - 30])
    snap["batches"] = read_json(BATCH_FILE, [])
    dnc = read_json(DNC_FILE, {})
    snap["dnc"] = dnc
    for clients in snap["funnels"].values():
        for c in clients:
            if c.get("phone") in dnc:
                c["optout"] = True
    for rows in (snap.get("leads") or {}).values():
        for c in rows:
            if c.get("phone") in dnc:
                c["optout"] = True
    return snap


def dnc_update(add, remove, note):
    """Список «не писать»: add/remove — любые записи номеров, приводим к виду 7XXXXXXXXXX."""
    today = int(time.time() // 86400)
    ok_add = [p for p in (norm_phone(x) for x in add) if len(p) == 11 and p[0] == "7"]
    bad = len(add) - len(ok_add)
    rm = {norm_phone(x) for x in remove}
    with LOCK:
        d = read_json(DNC_FILE, {})
        for p in ok_add:
            d.setdefault(p, {"d": today, "note": str(note)[:200]})
        for p in rm:
            d.pop(p, None)
        with open(DNC_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
    return d, len(ok_add), bad


def add_batch(funnel, tpl, phones, ctrl, cat="work"):
    """Журнал пачек: по нему раздел «Результаты» сравнивает купивших и контрольную группу."""
    with LOCK:
        b = read_json(BATCH_FILE, [])
        b.append({"id": len(b) + 1, "day": int(time.time() // 86400), "funnel": str(funnel), "tpl": str(tpl)[:2000],
                  "cat": str(cat)[:10], "phones": phones, "ctrl": ctrl})
        with open(BATCH_FILE, "w", encoding="utf-8") as f:
            json.dump(b, f, ensure_ascii=False)


def add_sent(phones):
    today = int(time.time() // 86400)
    with LOCK:
        sent = read_json(SENT_FILE, {})
        for p in phones:
            sent.setdefault(p, []).append(today)
        with open(SENT_FILE, "w", encoding="utf-8") as f:
            json.dump(sent, f)


# ---------------------------------------------------------------- веб-сервер
SESSIONS_FILE = os.path.join(HERE, "sessions.json")
SESSION_DAYS = 30
FAILS = {}   # ip -> [число неудач, время последней]

LOGIN_HTML = """<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Пульс базы — вход</title><style>
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:#f4f1f3;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:#2b2430}
.c{background:#fff;border-radius:16px;box-shadow:0 8px 30px rgba(60,30,60,.12);padding:34px 30px;width:100%;max-width:360px;margin:16px}
h1{margin:0 0 4px;font-size:22px}p{margin:0 0 22px;color:#7b6f80;font-size:14px}
label{display:block;font-size:13px;margin:14px 0 6px;color:#5b5060}
input{width:100%;padding:12px 14px;border:1px solid #d9d0dc;border-radius:10px;font-size:16px;outline:none}
input:focus{border-color:#b04a82;box-shadow:0 0 0 3px rgba(176,74,130,.15)}
button{width:100%;margin-top:22px;padding:13px;border:0;border-radius:10px;background:#b04a82;color:#fff;font-size:16px;font-weight:600;cursor:pointer}
button:hover{background:#9a3d70}.e{background:#fdecef;color:#b3243c;border-radius:8px;padding:10px 12px;font-size:14px;margin-bottom:6px}
.f{margin-top:18px;font-size:12px;color:#9a8fa0;text-align:center}</style></head><body>
<form class="c" method="post" action="/login"><h1>Пульс базы</h1><p>Розамания · повторные продажи</p>
__ERR__<label for="u">Логин</label><input id="u" name="u" autocomplete="username" autofocus required>
<label for="p">Пароль</label><input id="p" name="p" type="password" autocomplete="current-password" required>
<button type="submit">Войти</button><div class="f">Доступ только для сотрудников</div></form></body></html>"""


def make_handler(cfg):
    users = dict(cfg.get("users") or {})
    if cfg.get("password"):
        users.setdefault(cfg.get("user") or "pulse", cfg["password"])
    secure = bool(cfg.get("cookie_secure", True))

    def load_sessions():
        now = time.time()
        return {k: v for k, v in read_json(SESSIONS_FILE, {}).items() if v.get("exp", 0) > now}

    def save_sessions(d):
        tmp = SESSIONS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, SESSIONS_FILE)

    class H(BaseHTTPRequestHandler):
        def _ip(self):
            return (self.headers.get("X-Forwarded-For") or self.client_address[0]).split(",")[0].strip()

        def _cookie(self):
            for part in (self.headers.get("Cookie") or "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == "pulse_sid":
                    return v
            return ""

        def _auth(self):
            if not users:
                return True
            sid = self._cookie()
            with LOCK:
                sess = load_sessions().get(sid) if sid else None
            if sess:
                return True
            if self.command == "GET" and self.path.split("?")[0] in ("/", "/index.html"):
                self.send_response(302)
                self.send_header("Location", "/login")
                self.end_headers()
            else:
                self._send(401, b'{"error":"auth"}', "application/json")
            return False

        def _login_page(self, err=""):
            html = LOGIN_HTML.replace("__ERR__", '<div class="e">%s</div>' % err if err else "")
            self._send(200, html.encode(), "text/html; charset=utf-8")

        def _do_login(self):
            ip = self._ip()
            n = min(int(self.headers.get("Content-Length") or 0), 4096)
            form = urllib.parse.parse_qs(self.rfile.read(n).decode("utf-8", "ignore"))
            u = (form.get("u") or [""])[0].strip()
            p = (form.get("p") or [""])[0]
            f = FAILS.get(ip, [0, 0])
            if f[0] >= 5 and time.time() - f[1] < 300:
                return self._login_page("Слишком много попыток. Подождите 5 минут.")
            good = u in users and hmac.compare_digest(str(users[u]).encode(), p.encode())
            if not good:
                FAILS[ip] = [f[0] + 1 if time.time() - f[1] < 300 else 1, time.time()]
                time.sleep(1)
                log("Неудачный вход:", u[:30], ip)
                return self._login_page("Неверный логин или пароль")
            FAILS.pop(ip, None)
            sid = secrets.token_urlsafe(32)
            with LOCK:
                d = load_sessions()
                d[sid] = {"user": u, "exp": time.time() + SESSION_DAYS * 86400}
                save_sessions(d)
            log("Вход:", u, ip)
            self.send_response(303)
            self.send_header("Location", "/")
            self.send_header("Set-Cookie", "pulse_sid=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Lax%s" % (sid, SESSION_DAYS * 86400, "; Secure" if secure else ""))
            self.end_headers()

        def _do_logout(self):
            sid = self._cookie()
            with LOCK:
                d = load_sessions()
                if d.pop(sid, None) is not None:
                    save_sessions(d)
            self.send_response(303)
            self.send_header("Location", "/login")
            self.send_header("Set-Cookie", "pulse_sid=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax%s" % ("; Secure" if secure else ""))
            self.end_headers()

        def _send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_data(self):
            """data.json со сжатием gzip и кэшем: файл большой, а меняется редко."""
            key = tuple(os.path.getmtime(f) if os.path.exists(f) else 0 for f in (DATA_FILE, SENT_FILE, BATCH_FILE, DNC_FILE))
            with LOCK:
                c = CACHE.get("data")
                if not c or c[0] != key:
                    raw = json.dumps(merged_data(), ensure_ascii=False).encode()
                    c = CACHE["data"] = (key, raw, gzip.compress(raw, 6))
            _, raw, gz = c
            if "gzip" in (self.headers.get("Accept-Encoding") or ""):
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(gz)))
                self.end_headers()
                self.wfile.write(gz)
            else:
                self._send(200, raw, "application/json; charset=utf-8")

        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/login":
                return self._login_page()
            if path == "/logout":
                return self._do_logout()
            if not self._auth():
                return
            if path == "/data.json":
                self._send_data()
            elif path in ("/", "/index.html"):
                with open(os.path.join(HERE, "index.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self):
            if self.path.split("?")[0] == "/login":
                return self._do_login()
            if not self._auth():
                return
            if self.path.split("?")[0] == "/api/sent":
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(n))
                    phones = [str(p) for p in body.get("phones", []) if p]
                    ctrl = [str(p) for p in body.get("ctrl", []) if p]
                except ValueError:
                    return self._send(400, b"bad json", "text/plain")
                add_sent(phones)
                if phones:
                    add_batch(body.get("funnel", ""), body.get("tpl", ""), phones, ctrl, body.get("cat", "work"))
                log("Записано отправок:", len(phones))
                self._send(200, b"{}", "application/json")
            elif self.path.split("?")[0] == "/api/amo_notes":
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(n))
                    items = [x for x in body.get("items", []) if isinstance(x, dict)]
                except ValueError:
                    return self._send(400, b"bad json", "text/plain")
                ok, bad, err = write_notes(load_cfg(), items)
                log("Примечания в AmoCRM: записано %d, не записано %d %s" % (ok, bad, err))
                self._send(200, json.dumps({"ok": ok, "bad": bad, "err": err}).encode(), "application/json")
            elif self.path.split("?")[0] == "/api/dnc":
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(n))
                    add = [str(x) for x in body.get("add", [])][:5000]
                    rem = [str(x) for x in body.get("remove", [])][:5000]
                except ValueError:
                    return self._send(400, b"bad json", "text/plain")
                d, added, bad = dnc_update(add, rem, body.get("note", ""))
                log("Список «не писать»: добавлено %d, не распознано %d, удалено %d, всего %d" % (added, bad, len(rem), len(d)))
                self._send(200, json.dumps({"dnc": d, "added": added, "bad": bad}).encode(), "application/json")
            else:
                self._send(404, b"not found", "text/plain")

        def log_message(self, *a):
            pass

    return H



def show_fields(cfg):
    """python pulse.py --fields : показать воронки и поля с их id, чтобы заполнить config.json."""
    print("\nВОРОНКИ:")
    for p in (amo(cfg, "/leads/pipelines").get("_embedded") or {}).get("pipelines") or []:
        print("  %s (id %s)" % (p["name"], p["id"]))
    for title, path in (("СДЕЛКИ", "/leads/custom_fields"), ("КОНТАКТЫ", "/contacts/custom_fields")):
        print("\nПОЛЯ: " + title)
        for f in pages(cfg, path, {}, "custom_fields"):
            print("  id %s | %s | тип: %s" % (f["id"], f["name"], f["type"]))
    print()

def main():
    cfg = load_cfg()
    if "--fields" in sys.argv:
        show_fields(cfg)
        return
    if cfg.get("token") and cfg["token"] != "ВСТАВЬТЕ_ДОЛГОСРОЧНЫЙ_ТОКЕН":
        threading.Thread(target=sync_loop, args=(cfg,), daemon=True).start()
    else:
        log("Токен AmoCRM не задан: синхронизация выключена, отдаю последний снимок, если он есть.")
    host, port = cfg.get("host", "127.0.0.1"), int(cfg.get("port", 8080))
    if cfg.get("host", "127.0.0.1") != "127.0.0.1" and not cfg.get("password"):
        log("ВНИМАНИЕ: сервер открыт в сети без пароля, а в данных телефоны клиентов. Задайте user и password.")
    log("Открывайте http://%s:%d" % (host, port))
    ThreadingHTTPServer((host, port), make_handler(cfg)).serve_forever()


if __name__ == "__main__":
    main()
