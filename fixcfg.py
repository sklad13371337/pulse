import json

def ru(codes):
    return "".join(chr(c) for c in codes)

ROZAMANIA = ru([0x0420,0x043E,0x0437,0x0430,0x043C,0x0430,0x043D,0x0438,0x044F])
AMSTERDAM = ru([0x0410,0x043C,0x0441,0x0442,0x0435,0x0440,0x0434,0x0430,0x043C])
SKLAD = ru([0x0421,0x043A,0x043B,0x0430,0x0434])

with open("config.json", "r", encoding="utf-8") as f:
    c = json.load(f)

c["funnels"] = [ROZAMANIA, AMSTERDAM, SKLAD]

with open("config.json", "w", encoding="utf-8") as f:
    json.dump(c, f, ensure_ascii=False, indent=2)

print("funnels set to:", c["funnels"])
