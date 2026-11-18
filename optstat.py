#!/usr/bin/env python3
"""Только чтение: где в AmoCRM отмечена отписка и у скольких записей поле заполнено.
Запуск на сервере:  cd /opt/pulse && python3 optstat.py
"""
import re
import sys

import pulse

cfg = pulse.load_cfg()
WORDS = re.compile(r"отпис|рассыл|не беспок|не писать|спам|согласи", re.I)
IDS = {743207, 743391}

for ent, path in (("контакты", "/contacts"), ("сделки", "/leads")):
    fields = {f["id"]: f for f in pulse.pages(cfg, path + "/custom_fields", {}, "custom_fields")}
    pick = {i: f for i, f in fields.items() if i in IDS or WORDS.search(f["name"] or "")}
    print("\n=== %s: подходящих полей %d" % (ent.upper(), len(pick)))
    for i, f in pick.items():
        print("  поле %s | %s | тип %s" % (i, f["name"], f["type"]))
    if not pick:
        continue
    total = 0
    filled = {i: 0 for i in pick}
    samples = {i: [] for i in pick}
    for rec in pulse.pages(cfg, path, {}, "contacts" if ent == "контакты" else "leads"):
        total += 1
        for f in rec.get("custom_fields_values") or []:
            i = f.get("field_id")
            if i in pick:
                vals = [v.get("value") for v in f.get("values") or []]
                if pulse.truthy(vals):
                    filled[i] += 1
                    if len(samples[i]) < 2:
                        samples[i].append(str(vals[0])[:20])
    print("  всего записей: %d" % total)
    for i, n in filled.items():
        print("  заполнено %-6d поле %s %s примеры: %s" % (n, i, pick[i]["name"], samples[i]))
print("\nготово")
