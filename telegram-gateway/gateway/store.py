"""Quản lý tài khoản + session TDLib trên đĩa (phương án B).

Bố cục /data (volume bền):
    /data/
      accounts.json            (0600) [{phone, display_name, status,
                                  added_at, last_seen}] — KHÔNG bí mật
      tdlib/<account_id>/
        khoa                   (0600) khóa mã hóa local TDLib, 32 byte hex
        files/                 (0700) thư mục database TDLib (P0 để trống)
        tai_khoan.json         (0600) sổ mock của tài khoản
      events/                  spool cho poll fallback

Khác session Pyrogram của thiết kế container cũ (file SQLite một tài
khoản): TDLib giữ state riêng trong thư mục files + khóa mã hóa local.
Mất khóa = mất khả năng đọc DB local = phải đăng nhập lại, nên khóa và
thư mục files phải nằm trong cùng một đơn vị sao lưu.

account_id là 16 ký tự hex đầu của SHA-256(phone): ổn định qua restart,
không lộ trực tiếp số điện thoại trong tên thư mục.
"""

import hashlib
import json
import os
import secrets
import time
from dataclasses import asdict, dataclass

ACCOUNTS_FILE = "accounts.json"
EVENTS_DIR = "events"


def account_id_for(phone: str) -> str:
    return hashlib.sha256(phone.encode("utf-8")).hexdigest()[:16]


@dataclass
class Account:
    phone: str
    display_name: str = ""
    status: str = "offline"  # online | offline | mock
    added_at: float = 0.0
    last_seen: float = 0.0

    @property
    def id(self) -> str:
        return account_id_for(self.phone)


def _chmod(path: str, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass


class AccountStore:
    """Sổ tài khoản + session TDLib trên đĩa, bền qua restart."""

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.tdlib_root = os.path.join(data_dir, "tdlib")
        self.events_dir = os.path.join(data_dir, EVENTS_DIR)
        os.makedirs(self.tdlib_root, exist_ok=True)
        os.makedirs(self.events_dir, exist_ok=True)
        _chmod(self.tdlib_root, 0o700)
        _chmod(self.events_dir, 0o700)
        self._accounts: dict[str, Account] = {}
        self._load()

    @property
    def _path(self) -> str:
        return os.path.join(self.data_dir, ACCOUNTS_FILE)

    def _load(self) -> None:
        if not os.path.exists(self._path):
            return
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            for item in raw:
                acc = Account(**{k: v for k, v in item.items()
                                  if k in Account.__dataclass_fields__})
                self._accounts[acc.phone] = acc
        except (OSError, ValueError):
            self._accounts = {}

    def _save(self) -> None:
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump([asdict(a) for a in self._accounts.values()], fh)
        _chmod(tmp, 0o600)
        os.replace(tmp, self._path)

    def list(self) -> list[Account]:
        return list(self._accounts.values())

    def get(self, phone: str) -> Account | None:
        return self._accounts.get(phone)

    def session_dir(self, phone: str) -> str:
        return os.path.join(self.tdlib_root, account_id_for(phone))

    def ensure_session_dir(self, phone: str) -> str:
        """Tạo cấu trúc session TDLib (files 0700 + khoa 0600) mà KHÔNG
        đăng ký tài khoản — dùng trước khi client TDLib mở (P1: client
        cần files_dir + khóa trước khi setTdlibParameters)."""
        sdir = self.session_dir(phone)
        files_dir = os.path.join(sdir, "files")
        os.makedirs(files_dir, exist_ok=True)
        _chmod(sdir, 0o700)
        _chmod(files_dir, 0o700)
        key_path = os.path.join(sdir, "khoa")
        if not os.path.exists(key_path):
            with open(key_path, "w", encoding="utf-8") as fh:
                fh.write(secrets.token_hex(16))  # 32 ký tự hex
            _chmod(key_path, 0o600)
        return sdir

    def create_session(self, phone: str, display_name: str = "",
                       status: str = "mock") -> Account:
        """Tạo tài khoản + session TDLib (thư mục files + khóa local).

        P0: status mặc định "mock" (khung). P1 đăng nhập thật truyền
        status="online" + display_name từ getMe.
        """
        acc = Account(phone=phone, display_name=display_name,
                      status=status, added_at=time.time(),
                      last_seen=time.time())
        sdir = self.ensure_session_dir(phone)
        ledger = os.path.join(sdir, "tai_khoan.json")
        if not os.path.exists(ledger):
            with open(ledger, "w", encoding="utf-8") as fh:
                json.dump({"phone": phone, "mock": status == "mock"}, fh)
            _chmod(ledger, 0o600)
        self._accounts[phone] = acc
        self._save()
        return acc

    def mark_online(self, phone: str, display_name: str = "") -> Account | None:
        acc = self._accounts.get(phone)
        if acc is None:
            return None
        acc.status = "online"
        acc.last_seen = time.time()
        if display_name:
            acc.display_name = display_name
        self._save()
        return acc

    def set_status(self, phone: str, status: str) -> None:
        acc = self._accounts.get(phone)
        if acc is not None and acc.status != status:
            acc.status = status
            self._save()

    def remove(self, phone: str) -> bool:
        """Xóa tài khoản + session (chỉ qua web UI có xác nhận)."""
        if phone not in self._accounts:
            return False
        import shutil
        shutil.rmtree(self.session_dir(phone), ignore_errors=True)
        del self._accounts[phone]
        self._save()
        return True
