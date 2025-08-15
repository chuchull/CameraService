import os
import sys
import json
import logging
from datetime import datetime
from typing import Optional, Dict, Any
import time

# Optional import: Pillow for image cropping if available
try:
    from PIL import Image
except Exception:  # pragma: no cover - pillow might be missing
    Image = None

# NetSDK imports. These modules are expected to be available in the runtime
# environment provided by the camera vendor. The service only requires their
# APIs at runtime, so we keep the imports but they are not executed during
# tests that only compile the source code.
try:  # pragma: no cover - external dependency
    from ctypes import (
        POINTER,
        c_ubyte,
        cast,
        c_void_p,
        C_LLONG,
        C_DWORD,
        C_LDWORD,
        CB_FUNCTYPE,
        sizeof,
    )
    from NetSDK.NetSDK import NetClient
    from NetSDK.SDK_Struct import (
        NET_IN_LOGIN_WITH_HIGHLEVEL_SECURITY,
        NET_OUT_LOGIN_WITH_HIGHLEVEL_SECURITY,
        DEV_EVENT_TRAFFICJUNCTION_INFO,
        LOG_SET_PRINT_INFO,
    )
    from NetSDK.SDK_Enum import (
        EM_LOGIN_SPAC_CAP_TYPE,
        EM_EVENT_IVS_TYPE,
    )
    from NetSDK.SDK_Callback import (
        fDisConnect,
        fHaveReConnect,
    )
except Exception:  # pragma: no cover - allows unit tests without SDK
    NetClient = object  # type: ignore
    NET_IN_LOGIN_WITH_HIGHLEVEL_SECURITY = object  # type: ignore
    NET_OUT_LOGIN_WITH_HIGHLEVEL_SECURITY = object  # type: ignore
    DEV_EVENT_TRAFFICJUNCTION_INFO = object  # type: ignore
    LOG_SET_PRINT_INFO = object  # type: ignore
    EM_LOGIN_SPAC_CAP_TYPE = object  # type: ignore
    EM_EVENT_IVS_TYPE = object  # type: ignore
    fDisConnect = object  # type: ignore
    fHaveReConnect = object  # type: ignore
    POINTER = c_ubyte = cast = c_void_p = C_LLONG = C_DWORD = C_LDWORD = CB_FUNCTYPE = None  # type: ignore


###############################################################################
# Database layer
###############################################################################


class DatabaseClient:
    """Simple wrapper around sqlite3 with graceful failure handling.

    The class can be easily adapted for other DB-API compatible clients. When
    connection cannot be established, :attr:`conn` remains ``None`` and calls
    to :meth:`save_event` only log warnings but never raise exceptions.
    """

    def __init__(self, path: Optional[str], logger: logging.Logger) -> None:
        self.logger = logger
        self.conn = None
        if not path:
            self.logger.warning("DB path not provided. Events will not be stored.")
            return
        try:
            import sqlite3

            self.conn = sqlite3.connect(path, check_same_thread=False)
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    plate TEXT,
                    payload TEXT,
                    global_image TEXT,
                    plate_image TEXT
                )
                """
            )
            self.conn.commit()
            self.logger.info("Connected to DB at %s", path)
        except Exception as exc:  # pragma: no cover - connection may fail
            self.logger.exception("Failed to connect to DB: %s", exc)
            self.conn = None

    # ------------------------------------------------------------------
    def save_event(self, data: Dict[str, Any]) -> bool:
        """Persist event information.

        Parameters
        ----------
        data:
            Dictionary that must contain ``ts``, ``plate``, ``payload``,
            ``global_image`` and ``plate_image`` keys.

        Returns
        -------
        bool
            ``True`` on success, ``False`` otherwise.
        """

        if not self.conn:  # DB is not available
            self.logger.warning(
                "DB not available. Event for plate %s not saved.",
                data.get("plate", "unknown"),
            )
            return False
        try:
            self.conn.execute(
                """
                INSERT INTO events (ts, plate, payload, global_image, plate_image)
                VALUES (:ts, :plate, :payload, :global_image, :plate_image)
                """,
                data,
            )
            self.conn.commit()
            return True
        except Exception as exc:  # pragma: no cover - depends on DB state
            self.logger.exception("Failed to save event: %s", exc)
            return False


###############################################################################
# Image utilities
###############################################################################


def crop_plate_from_global(global_path: str, rect: Dict[str, int], plate_path: str) -> None:
    """Crop plate region from global image.

    ``rect`` must contain ``left``, ``top``, ``right`` and ``bottom`` values in
    pixels. The function uses Pillow if available and logs a warning otherwise.
    """

    if not Image:
        logging.getLogger(__name__).warning(
            "Pillow is not installed; skipping plate cropping.")
        return

    try:
        with Image.open(global_path) as img:
            crop = img.crop((rect["left"], rect["top"], rect["right"], rect["bottom"]))
            crop.save(plate_path)
    except Exception as exc:  # pragma: no cover - image processing may fail
        logging.getLogger(__name__).exception("Failed to crop plate: %s", exc)


###############################################################################
# Camera service
###############################################################################


SERVICE_INSTANCE: "CameraService" | None = None


class CameraService:
    """Service responsible for connecting to camera and handling events."""

    def __init__(self, logger: logging.Logger, db_client: DatabaseClient) -> None:
        global SERVICE_INSTANCE
        SERVICE_INSTANCE = self
        self.logger = logger
        self.db = db_client
        self.sdk = None  # type: ignore
        self.login_id = C_LLONG(0) if C_LLONG else 0
        self.attach_id = C_LLONG(0) if C_LLONG else 0
        self._disconnect_cb = fDisConnect(self._on_disconnect) if fDisConnect else None
        self._reconnect_cb = fHaveReConnect(self._on_reconnect) if fHaveReConnect else None

        # directories for saving images
        self.global_dir = os.path.join(os.getcwd(), "images", "global")
        self.plate_dir = os.path.join(os.getcwd(), "images", "plates")
        os.makedirs(self.global_dir, exist_ok=True)
        os.makedirs(self.plate_dir, exist_ok=True)

    # ------------------------------------------------------------------
    def connect(self, host: str, port: int, username: str, password: str) -> bool:
        """Connect to camera device."""
        if NetClient is object:  # pragma: no cover - SDK missing
            self.logger.error("NetSDK not available. Connection aborted.")
            return False

        self.sdk = NetClient()
        self.sdk.InitEx(self._disconnect_cb)
        self.sdk.SetAutoReconnect(self._reconnect_cb)

        stu_in = NET_IN_LOGIN_WITH_HIGHLEVEL_SECURITY()
        stu_in.dwSize = sizeof(NET_IN_LOGIN_WITH_HIGHLEVEL_SECURITY)
        stu_in.szIP = host.encode()
        stu_in.nPort = port
        stu_in.szUserName = username.encode()
        stu_in.szPassword = password.encode()
        stu_in.emSpecCap = EM_LOGIN_SPAC_CAP_TYPE.TCP
        stu_out = NET_OUT_LOGIN_WITH_HIGHLEVEL_SECURITY()
        stu_out.dwSize = sizeof(NET_OUT_LOGIN_WITH_HIGHLEVEL_SECURITY)

        self.login_id, _, err = self.sdk.LoginWithHighLevelSecurity(stu_in, stu_out)
        if not self.login_id:
            self.logger.error("Login failed: %s", err)
            return False
        self.logger.info("Connected to camera %s:%s", host, port)
        return True

    # ------------------------------------------------------------------
    def subscribe(self, channel: int = 0) -> bool:
        if not self.login_id:
            self.logger.error("Cannot subscribe without login")
            return False
        if not self.sdk:
            return False

        bNeedPicFile = 1
        dwUser = 0
        self.attach_id = self.sdk.RealLoadPictureEx(
            self.login_id,
            channel,
            EM_EVENT_IVS_TYPE.TRAFFICJUNCTION,
            bNeedPicFile,
            AnalyzerDataCallBack,
            dwUser,
            None,
        )
        if not self.attach_id:
            self.logger.error("Subscribe failed: %s", self.sdk.GetLastError())
            return False
        self.logger.info("Subscribed to channel %s", channel)
        return True

    # ------------------------------------------------------------------
    def unsubscribe(self) -> None:
        if self.attach_id and self.sdk:
            self.sdk.StopLoadPic(self.attach_id)
            self.logger.info("Unsubscribed from events")
            self.attach_id = C_LLONG(0) if C_LLONG else 0

    # ------------------------------------------------------------------
    def logout(self) -> None:
        self.unsubscribe()
        if self.login_id and self.sdk:
            self.sdk.Logout(self.login_id)
            self.logger.info("Logged out from camera")
            self.login_id = C_LLONG(0) if C_LLONG else 0

    # ------------------------------------------------------------------
    def _on_disconnect(self, lLoginID, pchDVRIP, nDVRPort, dwUser) -> None:  # pragma: no cover
        self.logger.warning("Disconnected from camera %s:%s", pchDVRIP.decode(), nDVRPort)

    def _on_reconnect(self, lLoginID, pchDVRIP, nDVRPort, dwUser) -> None:  # pragma: no cover
        self.logger.info("Reconnected to camera %s:%s", pchDVRIP.decode(), nDVRPort)

    # ------------------------------------------------------------------
    def analyzer_callback(self, lAnalyzerHandle, dwAlarmType, pAlarmInfo, pBuffer, dwBufSize, dwUser, nSequence, reserved):
        """Handle raw data from callback."""
        if dwAlarmType != EM_EVENT_IVS_TYPE.TRAFFICJUNCTION:
            return

        # Extract event info
        alarm_info = cast(pAlarmInfo, POINTER(DEV_EVENT_TRAFFICJUNCTION_INFO)).contents

        ts = datetime(
            alarm_info.UTC.dwYear,
            alarm_info.UTC.dwMonth,
            alarm_info.UTC.dwDay,
            alarm_info.UTC.dwHour,
            alarm_info.UTC.dwMinute,
            alarm_info.UTC.dwSecond,
        ).isoformat()
        plate = alarm_info.stTrafficCar.szPlateNumber.decode("gb2312")

        event_data = {
            "ts": ts,
            "plate": plate,
            "payload": json.dumps({  # store raw info for future use
                "plate_color": alarm_info.stTrafficCar.szPlateColor.decode("utf-8", errors="ignore"),
                "vehicle_color": alarm_info.stTrafficCar.szVehicleColor.decode("utf-8", errors="ignore"),
                "country": alarm_info.stCommInfo.szCountry.decode("utf-8", errors="ignore"),
            }),
        }

        # Save images --------------------------------------------------
        global_path = os.path.join(self.global_dir, f"{ts}_{plate}_global.jpg")
        plate_path = os.path.join(self.plate_dir, f"{ts}_{plate}_plate.jpg")

        is_global = False
        is_small = False
        if alarm_info.stuObject.bPicEnble:
            is_global = True
            global_buf = cast(pBuffer, POINTER(c_ubyte * alarm_info.stuObject.stPicInfo.dwOffSet)).contents
            with open(global_path, "wb") as f:
                f.write(bytes(global_buf))
            if alarm_info.stuObject.stPicInfo.dwFileLenth > 0:
                is_small = True
                small_buf = pBuffer[
                    alarm_info.stuObject.stPicInfo.dwOffSet:
                    alarm_info.stuObject.stPicInfo.dwOffSet + alarm_info.stuObject.stPicInfo.dwFileLenth
                ]
                with open(plate_path, "wb") as f:
                    f.write(bytes(small_buf))
        elif dwBufSize > 0:
            is_global = True
            global_buf = cast(pBuffer, POINTER(c_ubyte * dwBufSize)).contents
            with open(global_path, "wb") as f:
                f.write(bytes(global_buf))

        if not is_small and is_global:
            # Device did not provide plate image. Try cropping from global image.
            rect = {
                "left": alarm_info.stuObject.stuRect.left,
                "top": alarm_info.stuObject.stuRect.top,
                "right": alarm_info.stuObject.stuRect.right,
                "bottom": alarm_info.stuObject.stuRect.bottom,
            }
            crop_plate_from_global(global_path, rect, plate_path)

        event_data["global_image"] = global_path if is_global else ""
        event_data["plate_image"] = plate_path if os.path.exists(plate_path) else ""

        saved = self.db.save_event(event_data)
        if saved:
            self.logger.info("Event saved for plate %s", plate)
        else:
            self.logger.warning("Event not saved for plate %s", plate)

        # Placeholder for user-defined post-processing ----------------
        self.process_event(event_data)

    # ------------------------------------------------------------------
    def process_event(self, event: Dict[str, Any]) -> None:
        """Hook for user-defined processing of events.

        This method is intentionally left empty and can be extended later. The
        service calls it after saving the event to disk and attempting DB
        persistence.
        """
        # TODO: implement custom logic
        return  # pragma: no cover


###############################################################################
# Global callback wrapper required by the SDK
###############################################################################


@CB_FUNCTYPE(None, C_LLONG, C_DWORD, c_void_p, POINTER(c_ubyte), C_DWORD, C_LDWORD, c_int, c_void_p)  # type: ignore
def AnalyzerDataCallBack(lAnalyzerHandle, dwAlarmType, pAlarmInfo, pBuffer, dwBufSize, dwUser, nSequence, reserved):  # pragma: no cover - executed by SDK
    if SERVICE_INSTANCE:
        SERVICE_INSTANCE.analyzer_callback(
            lAnalyzerHandle,
            dwAlarmType,
            pAlarmInfo,
            pBuffer,
            dwBufSize,
            dwUser,
            nSequence,
            reserved,
        )


###############################################################################
# Entry point
###############################################################################


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    logger = logging.getLogger("camera_service")

    db_path = os.environ.get("DB_PATH")
    db_client = DatabaseClient(db_path, logger)
    service = CameraService(logger, db_client)

    host = os.environ.get("CAMERA_HOST", "127.0.0.1")
    port = int(os.environ.get("CAMERA_PORT", "37777"))
    username = os.environ.get("CAMERA_USER", "admin")
    password = os.environ.get("CAMERA_PASS", "admin")

    if not service.connect(host, port, username, password):
        logger.error("Unable to start service without camera connection")
        return

    channel = int(os.environ.get("CAMERA_CHANNEL", "0"))
    if not service.subscribe(channel):
        logger.error("Subscription failed. Service will exit.")
        return

    logger.info("Camera service started. Listening for events...")
    try:
        while True:
            # The SDK operates in the background. We just keep main thread alive.
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    finally:
        service.logout()


if __name__ == "__main__":  # pragma: no cover
    main()
