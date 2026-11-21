#!/usr/bin/env python3
"""Sinh phần dịch vụ theo danh mục của telegram_call từ ``catalog.SERVICES`` vào services.yaml, strings.json và translations/{vi,en}.json.

Chạy lại bất cứ lúc nào (idempotent): phần sinh nằm sau dòng đánh dấu cuối services.yaml và dưới các khoá trùng tên catalog trong JSON; phần viết tay không bị đụng.
    python3 tools/gen_catalog.py
"""
import importlib.util
import json
import os
import re
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
    # Mọi dịch vụ viết tay dùng cùng bộ chọn tài khoản; giữ nguyên các anchor YAML.
    text = re.sub(r"    account:\n      selector:\n        text: null\n", "", text)
    text = text.replace("    config_entry_id:", "    account:\n      selector:\n        text: null\n    config_entry_id:")
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
        fields["account"] = {"selector": {"text": None}}
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
        svc["send_media_group"] = {
            "name": "Gửi album" if lang == "vi" else "Send album",
            "description": "Gửi album ảnh/video, hoặc gửi lần lượt khi các tệp không ghép được." if lang == "vi" else "Send a photo/video album, or send files individually when they cannot form an album.",
            "fields": {k: v for k, v in svc["send_file"]["fields"].items() if k in ("recipient", "chat_id", "peer", "reply_to", "format", "config_entry_id")},
        }
        svc["send_media_group"]["fields"]["media"] = {
            "name": "Tệp trong album" if lang == "vi" else "Album files",
            "description": "Danh sách media_type, file và caption của từng tệp." if lang == "vi" else "List of media_type, file and caption for each file.",
        }
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
        for service in svc.values():
            fields = service.get("fields", {})
            if "config_entry_id" in fields:
                fields["config_entry_id"] = ({"name": "Mục cấu hình", "description": "Ưu tiên cao nhất. Bỏ trống để chọn bằng account hoặc dùng tài khoản mặc định."} if lang == "vi" else
                                            {"name": "Config entry", "description": "Highest priority. Leave empty to select by account or use the default account."})
                fields["account"] = ({"name": "Tài khoản Telegram", "description": "Số điện thoại có mã nước hoặc tên hiển thị trên Telegram. Bỏ trống dùng mặc định; tên trùng phải dùng số hoặc config_entry_id."} if lang == "vi" else
                                     {"name": "Telegram account", "description": "Phone number with country code or Telegram display name. Empty uses the default; duplicate names require a phone or config_entry_id."})
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")


if __name__ == "__main__":
    main()
