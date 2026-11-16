#!/usr/bin/env python3
"""Sinh phần dịch vụ theo danh mục của telegram_call từ ``catalog.SERVICES`` vào services.yaml, strings.json và translations/{vi,en}.json.

Chạy lại bất cứ lúc nào (idempotent): phần sinh nằm sau dòng đánh dấu cuối services.yaml và dưới các khoá trùng tên catalog trong JSON; phần viết tay không bị đụng.
    python3 tools/gen_catalog.py
"""
import importlib.util
import json
import os
import sys

import yaml

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
INT = os.path.join(ROOT, "custom_components", "telegram_call")
MARK = "# --- Sinh bởi tools/gen_catalog.py từ catalog.py: ĐỪNG sửa tay phần dưới ---\n"


def _catalog():
    spec = importlib.util.spec_from_file_location("telegram_call_catalog", os.path.join(INT, "catalog.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _selector(f):
    if f.kind == "int":
        sel = {"number": {"mode": "box"}}
        if f.lo is not None:
            sel["number"]["min"] = f.lo
        if f.hi is not None:
            sel["number"]["max"] = f.hi
        return sel
    if f.kind == "bool":
        return {"boolean": None}
    if f.kind == "select":
        return {"select": {"options": list(f.options), "mode": "dropdown"}}
    if f.kind == "list_text":
        return {"text": {"multiline": True}}
    return {"text": None}


def _field_order(spec):
    keys = []
    if spec.target == "chat":
        keys += (["recipient"] if spec.fill is None else []) + ["chat_id", "peer"]
    elif spec.target == "user":
        keys += ["recipient"] + ([] if any(f.key == "user_id" for f in spec.fields) else ["user_id"]) + ["peer"]
    return keys


def main() -> None:
    cat = _catalog()
    path = os.path.join(INT, "services.yaml")
    text = open(path, encoding="utf-8").read().split(MARK)[0]
    block = {}
    for name, spec in cat.SERVICES.items():
        fields = {}
        for k in _field_order(spec):
            if k == "recipient":
                fields[k] = {"selector": {"entity": {"domain": "notify", "integration": "telegram_call"}}}
            elif k in ("chat_id", "user_id"):
                fields[k] = {"selector": {"number": {"mode": "box"}}}
            else:
                fields[k] = {"selector": {"text": None}}
        for f in spec.fields:
            d = {"selector": _selector(f)}
            if f.required:
                d["required"] = True
            if f.default is not None:
                d["default"] = f.default
            fields[f.key] = d
        fields["config_entry_id"] = {"selector": {"text": None}}
        block[name] = {"fields": fields}

    class D(yaml.SafeDumper):
        def ignore_aliases(self, data):
            return True
    out = yaml.dump(block, Dumper=D, allow_unicode=True, sort_keys=False, width=10000)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text if text.endswith("\n") else text + "\n")
        fh.write(MARK + out)

    for rel, lang in (("strings.json", "vi"), (os.path.join("translations", "vi.json"), "vi"), (os.path.join("translations", "en.json"), "en")):
        p = os.path.join(INT, rel)
        data = json.load(open(p, encoding="utf-8"))
        svc = data["services"]
        base = svc["mark_read"]["fields"]
        common = {"recipient": base["recipient"], "chat_id": base["chat_id"], "peer": base["peer"], "config_entry_id": base["config_entry_id"]}
        common["user_id"] = ({"name": "ID người dùng", "description": "Số ID Telegram của người. Có thể thay bằng ô «Chọn người từ danh bạ» hoặc nhập ở ô «Người nhận»."} if lang == "vi" else
                             {"name": "User ID", "description": "Numeric Telegram user ID. May be replaced by the contact picker or the recipient field."})
        for name, spec in cat.SERVICES.items():
            fl = {k: common[k] for k in _field_order(spec)}
            for f in spec.fields:
                fl[f.key] = {"name": f.vi_name if lang == "vi" else f.en_name, "description": f.vi_desc if lang == "vi" else f.en_desc}
            fl["config_entry_id"] = common["config_entry_id"]
            svc[name] = {"name": spec.vi_name if lang == "vi" else spec.en_name, "description": spec.vi_desc if lang == "vi" else spec.en_desc, "fields": fl}
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")


if __name__ == "__main__":
    main()
