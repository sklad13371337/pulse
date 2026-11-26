"""Только чтение: ищет поле сделки «Описание заказа» и показывает по 3 примера значений (длинные цифры скрыты)."""
import re
import pulse

c = pulse.load_cfg()
fields = list(pulse.pages(c, "/leads/custom_fields", {}, "custom_fields"))
cand = [f for f in fields if "опис" in f["name"].lower() or "состав" in f["name"].lower() or "товар" in f["name"].lower()]
print("ПОЛЯ СДЕЛКИ:", [(f["id"], f["name"]) for f in cand])
ids = {f["id"] for f in cand}
fill, shown = {}, {}
leads = pulse.amo(c, "/leads", {"limit": 250, "order[created_at]": "desc"}).get("_embedded", {}).get("leads", [])
for l in leads:
    for f in l.get("custom_fields_values") or []:
        if f["field_id"] in ids:
            fill[f["field_id"]] = fill.get(f["field_id"], 0) + 1
            if shown.get(f["field_id"], 0) < 3:
                shown[f["field_id"]] = shown.get(f["field_id"], 0) + 1
                v = str((f.get("values") or [{}])[0].get("value"))
                print(f["field_id"], "|", re.sub(r"\d{6,}", "#", v)[:160].replace("\n", " / "))
print("ЗАПОЛНЕНО в последних", len(leads), "сделок:", fill)
