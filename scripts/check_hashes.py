import json
import hashlib

def get_text(content):
    if isinstance(content, list):
        return " ".join(p.get("text", "") for p in content if p.get("type") == "text")
    return content or ""

def hash_msg(c):
    return hashlib.md5(("user:" + get_text(c)).encode("utf-8")).hexdigest()

d1 = json.load(open('temp/request_dump_1779860273.json', encoding='utf-8'))
d2 = json.load(open('temp/request_dump_1779860518.json', encoding='utf-8'))

hashes1 = [hash_msg(m["content"]) for m in d1["messages"] if m["role"] == "user"]
hashes2 = [hash_msg(m["content"]) for m in d2["messages"] if m["role"] == "user"]

print("Req1 hashes:", hashes1)
print("Req2 hashes:", hashes2)
