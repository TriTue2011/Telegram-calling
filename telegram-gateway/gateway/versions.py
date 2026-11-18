"""Thương lượng phiên bản tgcalls giữa gateway và bên kia (điện thoại / Telegram Desktop), khớp cách NTgCalls chọn (signaling.cpp ``best_match``):
sắp danh sách phiên bản của bên kia từ cao xuống thấp rồi lấy phiên bản ĐẦU TIÊN mà ta hỗ trợ.

Hai họ báo hiệu (đọc từ mã NTgCalls, 18/11/2026): 8.0.0 và 9.0.0 = V2 «Legacy» (thông điệp báo hiệu đi qua máy chủ Telegram, kết nối ngoài);
12.0.0 và 13.0.0 = V3 (báo hiệu đi trong kênh dữ liệu SCTP chung đường media). Mã hoá báo hiệu dùng chung khoá trao đổi; bitrate do bộ ước lượng WebRTC quyết định ở cả hai.
Bản cũ hơn (2.4.4, 5.0.0…) NTgCalls KHÔNG hỗ trợ: phải báo rõ và ngắt, không để cuộc gọi treo không tiếng."""

FAMILY = {"8.0.0": "V2 Legacy", "9.0.0": "V2 Legacy", "12.0.0": "V3 SCTP", "13.0.0": "V3 SCTP"}
KNOWN_OLD = ("2.4.4", "2.7.7", "4.0.0", "5.0.0", "6.0.0", "7.0.0")      # phiên bản tgcalls cũ còn gặp ở bản Telegram rất cũ


def _key(version):
    try:
        return tuple(int(p) for p in str(version).split("."))
    except ValueError:
        return ()


def best_match(peer_versions, ours):
    """Phiên bản chung cao nhất, hoặc None. Danh sách rỗng/None = không biết (trả None, người gọi quyết định)."""
    mine = set(str(v) for v in (ours or []))
    for v in sorted((str(x) for x in (peer_versions or [])), key=_key, reverse=True):
        if v in mine:
            return v
    return None


def describe(peer_versions, ours):
    """(phiên bản chung|None, câu mô tả tiếng Việt cho nhật ký). peer rỗng → (None, 'bên kia không khai phiên bản')."""
    peer = [str(v) for v in (peer_versions or [])]
    if not peer:
        return None, "bên kia không khai phiên bản"
    v = best_match(peer, ours)
    if v is None:
        old = [x for x in peer if x in KNOWN_OLD]
        return None, "KHÔNG có phiên bản chung (bên kia %s, ta %s)%s" % (peer, list(ours or []), " — bản tgcalls cũ, cần cập nhật Telegram" if old and len(old) == len(peer) else "")
    return v, "%s (báo hiệu %s)" % (v, FAMILY.get(v, "?"))
