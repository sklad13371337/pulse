#!/usr/bin/env python3
"""Только чтение: где в AmoCRM лежит состав заказа. Без имён и телефонов клиентов.
Запуск на сервере:  cd /opt/pulse && python3 leadprobe.py > /tmp/probe.txt; head -n 30 /tmp/probe.txt
"""
import collections
import re

import pulse

cfg = pulse.load_cfg()
WORDS = re.compile(r"состав|товар|букет|позици|заказ|описан|комментар|цвет|роз", re.I)
pls = (pulse.amo(cfg, "/leads/pipelines").get("_embedded") or {}).get("pipelines") or []
pid = [p["id"] for p in pls if cfg["funnels"][0].lower() in p["name"].lower()][0]
fields = {f["id"]: f for f in pulse.pages(cfg, "/leads/custom_fields", {}, "custom_fields")}
leads = (pulse.amo(cfg, "/leads", {
    "filter[statuses][0][pipeline_id]": pid, "filter[statuses][0][status_id]": 142,
    "with": "catalog_elements", "limit": 100, "order[closed_at]": "desc"}).get("_embedded") or {}).get("leads") or []
print("ВОРОНКА %s, взято сделок: %d" % (cfg["funnels"][0], len(leads)))
cnt = collections.Counter()
sample = {}
for l in leads:
    for f in l.get("custom_fields_values") or []:
        cnt[f["field_id"]] += 1
        v = str((f.get("values") or [{}])[0].get("value"))
        if WORDS.search(f.get("field_name") or ""):
            sample.setdefault(f["field_id"], v[:50])
print("\nПОЛЯ СДЕЛОК (заполнено из %d):" % len(leads))
for i, n in cnt.most_common(14):
    print("  %s | %s | %s | %d%s" % (i, (fields[i]["name"] if i in fields else "?")[:28],
                                   fields[i]["type"] if i in fields else "?", n,
                                   (" | пример: " + sample[i]) if i in sample else ""))
with_cat = [l for l in leads if (l.get("_embedded") or {}).get("catalog_elements")]
print("\nсделок с привязанными товарами (catalog_elements): %d" % len(with_cat))
for l in with_cat[:2]:
    print("  пример:", [(e.get("id"), e.get("metadata", {}).get("catalog_id"), e.get("quantity")) for e in l["_embedded"]["catalog_elements"]][:4])
try:
    cats = (pulse.amo(cfg, "/catalogs").get("_embedded") or {}).get("catalogs") or []
    print("\nСПИСКИ/КАТАЛОГИ:", [(c["id"], c["name"][:18], c.get("type")) for c in cats][:8])
except Exception as e:
    print("каталоги недоступны:", repr(e)[:60])
print("\nНАЗВАНИЯ СДЕЛОК (5 шт):", [(l.get("name") or "")[:28] for l in leads[:5]])
print("\nПРИМЕЧАНИЯ (типы, первые 3 сделки):")
for l in leads[:3]:
    try:
        ns = (pulse.amo(cfg, "/leads/%d/notes" % l["id"], {"limit": 10}).get("_embedded") or {}).get("notes") or []
        print("  сделка:", [(n.get("note_type"), len(str((n.get("params") or {}).get("text") or ""))) for n in ns][:8])
    except Exception as e:
        print("  ошибка заметок:", repr(e)[:50])
print("\nготово")
