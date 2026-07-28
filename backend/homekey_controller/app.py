from __future__ import annotations

import logging
import signal
import threading
import time
import uuid

from pyhap.accessory_driver import AccessoryDriver

from homekey_bridge.server import (
    ReaderManager,
    ReaderRecord,
    ReaderWebSocketServer,
    load_registry,
)

from .access_api import AccessApiClient
from .button_api import ButtonApiClient
from .config import ControllerConfig
from .firmware_server import FirmwareHttpServer
from .readers import ReaderWorker, install_websocket_nfc_transport
from .reader_enrollment import enroll_fleet_reader
from .registration import EnrollmentService, RegistrationAccessory
from .store import SQLiteKeyStore


log = logging.getLogger(__name__)


class HomeKeyController:
    def __init__(self, config: ControllerConfig) -> None:
        self.config = config
        self.stop_event = threading.Event()
        self._enrollment_lock = threading.Lock()
        self._workers_lock = threading.Lock()
        self.store = SQLiteKeyStore(
            config.database.path,
            config.database.encryption_key_file,
        )
        registry = load_registry(config.reader_registry)
        self.manager = ReaderManager(registry)
        self.button_api = ButtonApiClient(config.button_api)
        self.access_api = AccessApiClient(config.access_api)
        self.authentication_lock = threading.Lock()
        self.websocket_server = ReaderWebSocketServer(
            self.manager,
            config.websocket_host,
            config.websocket_port,
            button_handler=self._handle_button,
            enrollment_handler=self._auto_enroll_reader,
        )
        self.firmware_server = FirmwareHttpServer(
            config.firmware_server,
            config,
            self.manager,
        )
        install_websocket_nfc_transport(self.manager)
        self.workers = [
            self._new_worker(record.reader_id)
            for record in registry.values()
            if record.enabled
        ]
        self.hap_driver: AccessoryDriver | None = None
        if config.homekit.enabled:
            self.hap_driver = self._build_hap_driver()

    def _new_worker(self, reader_id: str) -> ReaderWorker:
        return ReaderWorker(
            reader_id=reader_id,
            manager=self.manager,
            store=self.store,
            access_api=self.access_api,
            config=self.config,
            authentication_lock=self.authentication_lock,
        )

    def _auto_enroll_reader(
        self,
        reader_id: str,
        supplied_token: str,
    ) -> ReaderRecord | None:
        """Persist and start a fleet-authenticated reader on first contact."""
        with self._enrollment_lock:
            if self.stop_event.is_set():
                return None
            existing = self.manager.registry.get(reader_id)
            if existing is not None:
                return existing

            record = enroll_fleet_reader(
                self.config.reader_registry,
                self.config.door_id,
                reader_id,
                supplied_token,
            )
            if record is None:
                return None
            record = self.manager.add_record(record)
            worker = self._new_worker(reader_id)
            with self._workers_lock:
                self.workers.append(worker)
                worker.start()
            log.info(
                "Auto-enrolled reader %s for logical door %s",
                reader_id,
                self.config.door_id,
            )
            return record

    def _handle_button(self, reader) -> bool:
        event_id = str(uuid.uuid4())
        result = self.button_api.send(
            {
                "event_id": event_id,
                "event": "doorbell_button",
                "controller_id": self.config.controller_id,
                "door_id": self.config.door_id,
                "reader_id": reader.reader_id,
                "pressed_at": int(time.time()),
                "timestamp": int(time.time()),
            }
        )
        log.info(
            "Button event reader=%s door=%s delivered=%s reason=%s",
            reader.reader_id,
            self.config.door_id,
            result.delivered,
            result.reason,
        )
        return result.delivered

    def _build_hap_driver(self) -> AccessoryDriver:
        config = self.config.homekit
        config.persist_file.parent.mkdir(parents=True, exist_ok=True)
        driver = AccessoryDriver(
            port=config.port,
            persist_file=str(config.persist_file),
        )
        accessory = RegistrationAccessory(
            driver,
            config.display_name,
            enrollment_service=EnrollmentService(self.store),
        )
        driver.add_accessory(accessory=accessory)
        return driver

    def start(self) -> None:
        # Existing workers wait for their reader connection, so start them
        # before accepting WebSockets. This also prevents an auto-enrolled
        # worker from racing the initial worker-start loop.
        with self._workers_lock:
            workers = list(self.workers)
        for worker in workers:
            worker.start()
        self.websocket_server.start()
        self.firmware_server.start()
        log.info(
            "Controller %s started for logical door %s with %d readers",
            self.config.controller_id,
            self.config.door_id,
            len(self.workers),
        )
        log.info(
            "Reader WebSocket endpoint ws://%s:%d/readers",
            self.config.websocket_host,
            self.config.websocket_port,
        )

    def run(self) -> None:
        self.start()
        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, lambda *_args: self.stop())
        if self.hap_driver is not None:
            log.info(
                "HomeKit registration accessory listening on port %d",
                self.config.homekit.port,
            )
            self.hap_driver.start()
        else:
            self.stop_event.wait()

    def stop(self) -> None:
        if self.stop_event.is_set():
            return
        self.stop_event.set()
        log.info("Stopping Home Key controller")
        # Wait for any in-flight enrollment to finish before taking the final
        # worker snapshot. New enrollment attempts see stop_event and reject.
        with self._enrollment_lock:
            with self._workers_lock:
                workers = list(self.workers)
        for worker in workers:
            worker.stop()
        self.firmware_server.stop()
        self.websocket_server.stop()
        if self.hap_driver is not None:
            self.hap_driver.stop()
        for worker in workers:
            worker.join(timeout=3)
