"""Kiểm yêu cầu gửi cho TDLib có ĐÚNG lược đồ không (tên lớp, tên trường, kiểu trường lồng nhau).

Lý do: gateway từng sai ba lần cùng một kiểu — viết theo API TDLib cũ (setTdlibParameters lồng "parameters",
bước QR tên cũ, tệp không bọc inputDocument/inputPhoto...) mà test dùng client/transport giả nên đều xanh.
Các fake trong test gọi ``validate`` cho MỌI yêu cầu nên đường code nào test chạm tới đều được đối chiếu.

``td_api_subset.tl`` trích từ td_api.tl (github.com/tdlib/td). Thêm lớp mới vào mã thì trích lại: chạy
``python3 tests/schema.py <td_api.tl đầy đủ>`` để sinh lại tệp này từ các "@type" trong gateway/*.py.
"""
import glob
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_PRIM = {"int32", "int53", "int64", "string", "Bool", "bytes", "double"}
_DEF = re.compile(r"^([a-z][A-Za-z0-9]*)\s+(.*?)\s*=\s*([A-Za-z0-9]+);")
_SCHEMA = None


def _load():
    global _SCHEMA
    if _SCHEMA is None:
        fields, result = {}, {}
        with open(os.path.join(_HERE, "td_api_subset.tl"), encoding="utf-8") as fh:
            for ln in fh:
                m = _DEF.match(ln.strip())
                if m:
                    fields[m.group(1)] = {f.split(":")[0]: f.split(":", 1)[1] for f in m.group(2).split() if ":" in f}
                    result[m.group(1)] = m.group(3)
        _SCHEMA = (fields, result)
    return _SCHEMA


def _lc(s):
    return s[:1].lower() + s[1:]


def errors(obj, expect=None, path="$"):
    """Danh sách lỗi (rỗng = đúng) của một yêu cầu/đối tượng TDLib dạng dict."""
    fields, result = _load()
    out = []
    if isinstance(obj, list):
        inner = re.sub(r"^vector<(.*)>$", r"\1", expect) if expect else None
        for i, v in enumerate(obj):
            out += errors(v, inner, f"{path}[{i}]")
        return out
    if not isinstance(obj, dict):
        return out
    name = obj.get("@type")
    if name is None:
        return [f"{path}: thiếu @type"] if expect and expect not in _PRIM else out
    if name not in fields:
        return [f"{path}: lớp '{name}' không có trong lược đồ (hoặc chưa trích vào td_api_subset.tl)"]
    if expect and expect not in (name, result[name]) and _lc(expect) != name:
        out.append(f"{path}: {name} đặt ở chỗ cần {expect}")
    for k, v in obj.items():
        if k in ("@type", "@extra"):
            continue
        if k not in fields[name]:
            out.append(f"{path}: {name} không có trường '{k}' (có: {' '.join(fields[name])})")
            continue
        out += errors(v, fields[name][k], f"{path}.{k}")
    return out


def validate(req):
    errs = errors(req)
    assert not errs, "Yêu cầu TDLib sai lược đồ:\n  " + "\n  ".join(errs)


def _regenerate(full_tl):
    defs, by_result = {}, {}
    for ln in open(full_tl, encoding="utf-8").read().split("\n"):
        m = _DEF.match(ln)
        if m:
            defs[m.group(1)] = ln
            by_result.setdefault(m.group(3), []).append(m.group(1))
    used = {"inputFileLocal", "inputDocument", "inputPhoto", "inputVideo", "inputAudio", "inputVoiceNote", "inputThumbnail"}
    for f in glob.glob(os.path.join(_HERE, "..", "gateway", "*.py")):
        for m in re.finditer(r'"@type":\s*"([A-Za-z0-9]+)"', open(f, encoding="utf-8").read()):
            if m.group(1) in defs:
                used.add(m.group(1))
    need, stack = set(), list(used)
    while stack:
        n = stack.pop()
        if n in need or n not in defs:
            continue
        need.add(n)
        for f in _DEF.match(defs[n]).group(2).split():
            if ":" not in f:
                continue
            t = re.sub(r"^vector<(.*)>$", r"\1", f.split(":", 1)[1])
            if t and t not in _PRIM and t != "Ok":
                stack.extend([t] if t[0].islower() else by_result.get(t, []))
    with open(os.path.join(_HERE, "td_api_subset.tl"), "w", encoding="utf-8") as fh:
        fh.write("// Trích từ td_api.tl của TDLib (github.com/tdlib/td, master) — chỉ các lớp gateway dùng và các lớp tham chiếu.\n")
        fh.write("// Dùng cho tests/schema.py. Sinh lại: python3 tests/schema.py <td_api.tl đầy đủ>\n")
        fh.write("\n".join(defs[n] for n in sorted(need)) + "\n")


if __name__ == "__main__":
    _regenerate(sys.argv[1])
