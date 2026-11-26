import json
c = json.load(open("config.json", encoding="utf-8"))
c["items_field_ids"] = [741593, 743261]
c["parse_notes"] = False
c["sync_hours"] = 24
json.dump(c, open("config.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("config ok", c["items_field_ids"], c["sync_hours"])
