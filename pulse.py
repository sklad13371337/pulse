#!/usr/bin/env python3
"""Пульс базы: забирает успешные сделки из AmoCRM и отдаёт страницу с данными.

Только стандартная библиотека Python 3.8+. Запуск:  python pulse.py
Настройки: config.json рядом со скриптом (образец: config.example.json).
"""
import base64
import datetime
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
LOCK = threading.Lock()


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
    since = int(time.time()) - int(cfg.get("months_back", 24)) * 30 * 86400
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
            if cfg.get("items_field_id"):
                items = [str(x) for x in field_values(l, cfg["items_field_id"]) if x]
            else:
                items = [l.get("name") or ""]
            cl = clients.setdefault(key, {
                "id": "%s-%s" % (prefix, c["id"]), "name": c.get("name") or "Без имени", "phone": ph,
                "consent": bool(cfg.get("assume_consent", False)), "optout": False, "complaint": False,
                "noWa": False, "lastMsg": None, "msgs30": 0, "orders": []})
            if cfg.get("consent_field_id"):
                cl["consent"] = any(v is True for v in field_values(c, cfg["consent_field_id"]))
            if cfg.get("optout_field_id"):
                cl["optout"] = truthy(field_values(c, cfg["optout_field_id"]))
            cl["orders"].append({"d": l["closed_at"] // 86400, "sum": l["price"], "items": [i for i in items if i]})
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
    return snap


def add_batch(funnel, tpl, phones, ctrl):
    """Журнал пачек: по нему раздел «Результаты» сравнивает купивших и контрольную группу."""
    with LOCK:
        b = read_json(BATCH_FILE, [])
        b.append({"id": len(b) + 1, "day": int(time.time() // 86400), "funnel": str(funnel), "tpl": str(tpl)[:2000],
                  "phones": phones, "ctrl": ctrl})
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
def make_handler(cfg):
    user, pwd = cfg.get("user"), cfg.get("password")
    token = "Basic " + base64.b64encode(("%s:%s" % (user, pwd)).encode()).decode() if pwd else None

    class H(BaseHTTPRequestHandler):
        def _auth(self):
            if token and self.headers.get("Authorization") != token:
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="Pulse"')
                self.end_headers()
                return False
            return True

        def _send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self._auth():
                return
            path = self.path.split("?")[0]
            if path == "/data.json":
                self._send(200, json.dumps(merged_data(), ensure_ascii=False).encode(), "application/json; charset=utf-8")
            elif path in ("/", "/index.html"):
                with open(os.path.join(HERE, "index.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self):
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
                    add_batch(body.get("funnel", ""), body.get("tpl", ""), phones, ctrl)
                log("Записано отправок:", len(phones))
                self._send(200, b"{}", "application/json")
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
