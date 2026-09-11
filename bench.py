import time
import re
import functools
import json

def _update_toml_value_original(text: str, section: str, key: str, value) -> str:
    if isinstance(value, bool):
        literal = "true" if value else "false"
    elif isinstance(value, int):
        literal = str(value)
    else:
        literal = json.dumps(str(value), ensure_ascii=False)

    section_re = re.compile(
        rf"(?ms)^(\[{re.escape(section)}\]\s*$)(.*?)(?=^\[|\Z)"
    )
    match = section_re.search(text)
    if not match:
        suffix = "\n" if text and not text.endswith("\n") else ""
        return f"{text}{suffix}\n[{section}]\n{key} = {literal}\n"

    body = match.group(2)
    key_re = re.compile(rf"(?m)^(\s*{re.escape(key)}\s*=\s*).*$")
    key_match = key_re.search(body)
    if key_match:
        body = body[: key_match.start()] + key_match.group(1) + literal + body[key_match.end() :]
    else:
        if body and not body.endswith("\n"):
            body += "\n"
        body += f"{key} = {literal}\n"
    return text[: match.start(2)] + body + text[match.end(2) :]

@functools.lru_cache(maxsize=128)
def _get_section_re(section: str):
    return re.compile(rf"(?ms)^(\[{re.escape(section)}\]\s*$)(.*?)(?=^\[|\Z)")

@functools.lru_cache(maxsize=128)
def _get_key_re(key: str):
    return re.compile(rf"(?m)^(\s*{re.escape(key)}\s*=\s*).*$")

def _update_toml_value_optimized(text: str, section: str, key: str, value) -> str:
    if isinstance(value, bool):
        literal = "true" if value else "false"
    elif isinstance(value, int):
        literal = str(value)
    else:
        literal = json.dumps(str(value), ensure_ascii=False)

    section_re = _get_section_re(section)
    match = section_re.search(text)
    if not match:
        suffix = "\n" if text and not text.endswith("\n") else ""
        return f"{text}{suffix}\n[{section}]\n{key} = {literal}\n"

    body = match.group(2)
    key_re = _get_key_re(key)
    key_match = key_re.search(body)
    if key_match:
        body = body[: key_match.start()] + key_match.group(1) + literal + body[key_match.end() :]
    else:
        if body and not body.endswith("\n"):
            body += "\n"
        body += f"{key} = {literal}\n"
    return text[: match.start(2)] + body + text[match.end(2) :]


text = """
[Settings]
foo = 1
bar = 2

[Target]
hello = "world"
"""

start = time.time()
for i in range(100000):
    _update_toml_value_original(text, "Settings", "foo", i)
    _update_toml_value_original(text, "Target", "hello", "world")
end = time.time()
orig_time = end - start

start = time.time()
for i in range(100000):
    _update_toml_value_optimized(text, "Settings", "foo", i)
    _update_toml_value_optimized(text, "Target", "hello", "world")
end = time.time()
opt_time = end - start

print(f"Original Elapsed: {orig_time:.4f}s")
print(f"Optimized Elapsed: {opt_time:.4f}s")
print(f"Improvement: {(orig_time - opt_time) / orig_time * 100:.2f}%")
