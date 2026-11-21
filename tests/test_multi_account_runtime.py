"""Chạy logic runtime thật với các biên HA giả; không thay thế kiểm thử HA đầu-cuối."""
import ast
import asyncio
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from test_account_policy import accounts

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "telegram_call"


def definitions(filename, names, **namespace):
    """Nạp định nghĩa nguyên bản, chỉ tiêm biên framework để không cần cài HA."""
    path = ROOT / filename
    tree = ast.parse(path.read_text())
    nodes = [n for n in tree.body if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    assert {n.name for n in nodes} == set(names)
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *nodes], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)
    return SimpleNamespace(**namespace)


class Coordinator:
    def __class_getitem__(cls, item):
        return cls

    def __init__(self, hass, logger, **kwargs):
        self.hass, self.config_entry = hass, kwargs["config_entry"]


def test_account_poll_renames_only_its_device_and_preserves_identifiers():
    registry_calls = []
    device = SimpleNamespace(id="device-a", name="Telegram Call")
    registry = SimpleNamespace(
        async_get_device=lambda **kwargs: device if kwargs["identifiers"] == {("telegram_call", "entry-a")} else None,
        async_update_device=lambda key, **kwargs: registry_calls.append((key, kwargs)),
    )
    module = definitions("__init__.py", ["AccountCoordinator"], DataUpdateCoordinator=Coordinator, _LOGGER=None,
                         DOMAIN="telegram_call", ACCOUNT_POLL_S=60, timedelta=timedelta, CONF_ACCOUNT="account",
                         dr=SimpleNamespace(async_get=lambda hass: registry), device_name=accounts.device_name)
    rows = [{"phone": "+84900000001", "display_name": "Khác", "status": "online"},
            {"phone": "+84900000000", "display_name": "Nhà", "status": "online"}]

    async def get_accounts():
        return rows

    entry = SimpleNamespace(entry_id="entry-a", data={"account": "+84900000000"})
    coordinator = module.AccountCoordinator(object(), entry, SimpleNamespace(accounts=get_accounts))
    result = asyncio.run(coordinator._async_update_data())
    assert result == {"phone": "+84900000000", "display_name": "Nhà", "status": "online"}
    assert registry_calls == [("device-a", {"name": "Telegram Call — Nhà"})]
    rows[1]["display_name"] = "Tên mới"
    asyncio.run(coordinator._async_update_data())
    assert registry_calls[-1] == ("device-a", {"name": "Telegram Call — Tên mới"})
    rows.pop()
    assert asyncio.run(coordinator._async_update_data())["status"] == "missing"
    assert registry_calls[-1] == ("device-a", {"name": "Telegram Call — +84…000"})


def test_device_info_uses_current_profile_and_entity_unique_id_is_stable():
    module = definitions("entity.py", ["device_info", "TelegramCallEntity"], DeviceInfo=dict, Entity=object,
                         DOMAIN="telegram_call", CONF_ACCOUNT="account", device_name=accounts.device_name)
    data = {"display_name": "Nhà"}
    entry = SimpleNamespace(entry_id="entry-a", data={"account": "+84900000000"},
                            runtime_data=SimpleNamespace(account=SimpleNamespace(data=data)))
    entity = module.TelegramCallEntity(entry, "call_state")
    assert entity._attr_unique_id == "entry-a_call_state"
    assert entity.device_info["identifiers"] == {("telegram_call", "entry-a")}
    data["display_name"] = "Tên mới"
    assert entity.device_info["name"] == "Telegram Call — Tên mới"
    assert entity._attr_unique_id == "entry-a_call_state"
