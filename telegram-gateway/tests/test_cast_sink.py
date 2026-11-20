"""Loa Google Cast cho cuộc gọi: chỉ nhận cast://<IPv4 nội bộ> | cast://id/<mã>, khung CastMessage mã hoá/giải mã đúng, luồng MP3 nhận
từ ffmpeg (TCP 127.0.0.1) và phát cho người nghe kèm ~1 s đầu, token riêng từng cuộc (sai/đã đóng → không cho). Không mạng ngoài."""
import socket
import time

from gateway import cast_sink, live_call


def test_chi_nhan_hai_dang_dia_chi_loa_cast():
    ok = ["cast://192.168.1.50", "cast://10.0.0.7", "cast://172.16.10.249", "cast://id/" + "a" * 32]
    for s in ok:
        assert cast_sink.is_cast_spec(s) and live_call.valid_speaker_url(s), s
    bad = ["cast://8.8.8.8", "cast://127.0.0.1", "cast://169.254.1.1", "cast://0.0.0.0", "cast://loa.local", "cast://192.168.1.50:8009",
           "cast://id/" + "A" * 32, "cast://id/abc", "http://192.168.1.50/x", "cast://192.168.1.500", "", "cast://192.168.1.50/../x"]
    for s in bad:
        assert not cast_sink.is_cast_spec(s), s
    assert cast_sink.parse_spec("cast://id/" + "0f" * 16) == {"id": "0f" * 16}
    assert cast_sink.parse_spec("cast://192.168.001.050") is None or cast_sink.parse_spec("cast://192.168.001.050") == {"ip": "192.168.1.50"}


def test_khung_cast_message_ma_hoa_giai_ma_ca_noi_dung_dai():
    payload = {"type": "LOAD", "media": {"contentId": "http://x/" + "y" * 400}, "requestId": 7}
    raw = cast_sink.encode_message("sender-0", "web-5", cast_sink.NS_MEDIA, payload)
    n = int.from_bytes(raw[:4], "big")
    assert n == len(raw) - 4
    m = cast_sink.decode_message(raw[4:])
    assert m == {"source": "sender-0", "dest": "web-5", "namespace": cast_sink.NS_MEDIA, "payload": payload}


def test_luong_mp3_nhan_tu_ffmpeg_va_phat_cho_nguoi_nghe_kem_doan_dau():
    st = cast_sink.CastStream()
    try:
        cmd = st.encoder_command()
        assert cmd.endswith("tcp://127.0.0.1:%d" % st.port) and "-i pipe:0" in cmd and "libmp3lame" in cmd
        s = socket.create_connection(("127.0.0.1", st.port))
        s.sendall(b"A" * 1000)
        for _ in range(50):
            if st.bytes_in >= 1000:
                break
            time.sleep(0.02)
        q, prime = st.subscribe()
        assert prime == b"A" * 1000                     # người nghe vào sau vẫn có đoạn đầu gần nhất
        s.sendall(b"B" * 10)
        assert q.get(timeout=2) == b"B" * 10
        s.close()
        assert st.check(st.token) and not st.check("sai") and not st.check("")
        st.unsubscribe(q)
    finally:
        st.close()
    assert not st.check(st.token)                      # đóng (cuộc gọi kết thúc) → token hết hiệu lực


def test_doan_dau_toi_da_khoang_mot_giay():
    st = cast_sink.CastStream()
    try:
        s = socket.create_connection(("127.0.0.1", st.port))
        s.sendall(b"x" * (cast_sink.PRIME_BYTES * 3))
        for _ in range(100):
            if st.bytes_in >= cast_sink.PRIME_BYTES * 3:
                break
            time.sleep(0.02)
        _, prime = st.subscribe()
        assert len(prime) == cast_sink.PRIME_BYTES      # không dồn quá ~1 s (đỡ cộng trễ)
        s.close()
    finally:
        st.close()


def test_cast_sink_tu_choi_dia_chi_la():
    import pytest
    with pytest.raises(ValueError):
        cast_sink.CastSink("http://evil/x", "c1", 3333)
