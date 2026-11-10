"""Lỗi API của gateway — ngữ nghĩa giữ nguyên từ thiết kế module w2a.

Thân lỗi luôn có dạng {"error": {"code", "message", "retry_after_seconds?"}}.
retry_after_seconds CHỈ lấy từ Telegram (FLOOD_WAIT); gateway không tự bịa.
"""

from fastapi import HTTPException


class ApiError(HTTPException):
    def __init__(self, status: int, code: str, message: str,
                 retry_after_seconds: int | None = None):
        detail: dict = {"error": {"code": code, "message": message}}
        if retry_after_seconds is not None:
            detail["error"]["retry_after_seconds"] = retry_after_seconds
        super().__init__(status_code=status, detail=detail)
        self.code = code


def bad_request(msg: str) -> ApiError:
    return ApiError(400, "BAD_REQUEST", msg)


def not_found(msg: str) -> ApiError:
    return ApiError(404, "NOT_FOUND", msg)


def account_busy() -> ApiError:
    return ApiError(409, "ACCOUNT_BUSY",
                    "Tài khoản đang ở trong một cuộc gọi 1-1 khác.")


def call_not_active() -> ApiError:
    return ApiError(409, "CALL_NOT_ACTIVE", "Cuộc gọi không còn hoạt động.")


def endpoint_busy() -> ApiError:
    return ApiError(409, "ENDPOINT_BUSY",
                    "Mic/loa đã thuộc về một cuộc gọi khác.")


def flood_wait(retry_after_seconds: int) -> ApiError:
    return ApiError(429, "FLOOD_WAIT",
                    "Telegram yêu cầu chờ trước khi thử lại.",
                    retry_after_seconds=retry_after_seconds)


def login_locked() -> ApiError:
    return ApiError(429, "LOGIN_LOCKED",
                    "Đăng nhập bị khóa tạm thời sau nhiều mã sai.")


def media_engine_error(msg: str) -> ApiError:
    return ApiError(502, "MEDIA_ENGINE_ERROR", msg)


def account_offline() -> ApiError:
    return ApiError(503, "ACCOUNT_OFFLINE",
                    "Tài khoản đang mất kết nối tới Telegram.")


def not_implemented_yet(stage: str) -> ApiError:
    """501 kèm giai đoạn — đúng bảng thiết kế (P1/P2/P3/P4)."""
    return ApiError(501, "NOT_IMPLEMENTED",
                    f"Chưa có ở khung này, thuộc giai đoạn {stage}.")
