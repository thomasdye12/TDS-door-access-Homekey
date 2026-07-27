from __future__ import annotations

import base64
import functools
import logging
import os
import time

from pyhap.accessory import Accessory
from pyhap.const import CATEGORY_DOOR_LOCK

from .store import SQLiteKeyStore, ZERO_READER_KEY
from .vendor import activate_vendor

activate_vendor()

from entity import (  # noqa: E402
    ControlPointRequest,
    ControlPointResponse,
    DeviceCredentialRequest,
    DeviceCredentialResponse,
    Endpoint,
    Enrollment,
    Enrollments,
    HardwareFinishColor,
    HardwareFinishResponse,
    Issuer,
    Operation,
    OperationStatus,
    ReaderKeyRequest,
    ReaderKeyResponse,
    SupportedConfigurationResponse,
)
from util.structable import (  # noqa: E402
    pack_into_base64_string,
    unpack_from_base64_string,
)


log = logging.getLogger(__name__)


class EnrollmentService:
    """HomeKit NFC credential-management surface backed by SQLite."""

    def __init__(
        self, store: SQLiteKeyStore, finish: str = "black"
    ) -> None:
        self.store = store
        try:
            self.hardware_finish_color = HardwareFinishColor[finish.upper()]
        except KeyError:
            self.hardware_finish_color = HardwareFinishColor.BLACK

    def update_hap_pairings(self, issuer_public_keys: set[bytes]) -> None:
        current = {
            issuer.public_key: issuer
            for issuer in self.store.get_all_issuers()
        }
        for public_key, issuer in current.items():
            if public_key not in issuer_public_keys:
                log.info("Removing unpaired HomeKit issuer %s", issuer.id.hex())
                self.store.remove_issuer(issuer)
        for public_key in issuer_public_keys:
            if public_key not in current:
                issuer = Issuer(public_key=public_key, endpoints=[])
                log.info("Adding HomeKit issuer %s", issuer.id.hex())
                self.store.upsert_issuer(issuer)

    def get_reader_key(
        self, _request: ReaderKeyRequest
    ) -> ReaderKeyResponse:
        return ReaderKeyResponse(
            key_identifier=self.store.get_reader_group_identifier()
        )

    def add_reader_key(
        self, request: ReaderKeyRequest
    ) -> ReaderKeyResponse:
        changed = False
        if self.store.get_reader_private_key() != request.reader_private_key:
            self.store.set_reader_private_key(request.reader_private_key)
            changed = True
        if self.store.get_reader_identifier() != request.unique_reader_identifier:
            self.store.set_reader_identifier(
                request.unique_reader_identifier
            )
            changed = True
        return ReaderKeyResponse(
            status=(
                OperationStatus.SUCCESS
                if changed
                else OperationStatus.DUPLICATE
            )
        )

    def remove_reader_key(
        self, request: ReaderKeyRequest
    ) -> ReaderKeyResponse:
        matches = (
            request.key_identifier
            == self.store.get_reader_group_identifier()
        )
        if matches:
            self.store.set_reader_private_key(ZERO_READER_KEY)
        return ReaderKeyResponse(
            status=(
                OperationStatus.SUCCESS
                if matches
                else OperationStatus.DOES_NOT_EXIST
            )
        )

    def get_device_credential(
        self, request: DeviceCredentialRequest
    ) -> DeviceCredentialResponse:
        endpoint = self.store.get_endpoint_by_id(request.key_identifier)
        if endpoint is None:
            return DeviceCredentialResponse(
                status=OperationStatus.DOES_NOT_EXIST
            )
        return DeviceCredentialResponse(
            key_identifier=endpoint.id,
            issuer_key_identifier=request.issuer_key_identifier,
            status=OperationStatus.SUCCESS,
        )

    def add_device_credential(
        self, request: DeviceCredentialRequest
    ) -> DeviceCredentialResponse:
        public_key = b"\x04" + request.credential_public_key
        endpoint = self.store.get_endpoint_by_public_key(public_key)
        issuer = self.store.get_issuer_by_id(
            request.issuer_key_identifier
        )
        if issuer is None:
            return DeviceCredentialResponse(
                status=OperationStatus.DOES_NOT_EXIST
            )

        enrollment = Enrollment(
            at=int(time.time()),
            payload=base64.b64encode(request.pack()).decode("ascii"),
        )
        if endpoint is not None:
            if endpoint.enrollments.hap is not None:
                return DeviceCredentialResponse(
                    key_identifier=endpoint.id,
                    issuer_key_identifier=issuer.id,
                    status=OperationStatus.DUPLICATE,
                )
            endpoint.enrollments.hap = enrollment
        else:
            endpoint = Endpoint(
                last_used_at=0,
                counter=0,
                key_type=request.key_type,
                public_key=public_key,
                persistent_key=os.urandom(32),
                enrollments=Enrollments(
                    hap=enrollment,
                    attestation=None,
                ),
            )
        self.store.upsert_endpoint(issuer.id, endpoint)
        log.info("Enrolled endpoint %s", endpoint.id.hex())
        return DeviceCredentialResponse(
            key_identifier=endpoint.id,
            issuer_key_identifier=issuer.id,
            status=OperationStatus.SUCCESS,
        )

    def remove_device_credential(
        self, request: DeviceCredentialRequest
    ) -> DeviceCredentialResponse:
        removed = self.store.remove_endpoint(request.key_identifier)
        return DeviceCredentialResponse(
            key_identifier=request.key_identifier,
            issuer_key_identifier=request.issuer_key_identifier,
            status=(
                OperationStatus.SUCCESS
                if removed
                else OperationStatus.DOES_NOT_EXIST
            ),
        )

    def get_hardware_finish(self) -> str:
        return pack_into_base64_string(
            HardwareFinishResponse(color=self.hardware_finish_color)
        )

    def get_nfc_access_supported_configuration(self) -> str:
        return pack_into_base64_string(
            SupportedConfigurationResponse(
                number_of_issuer_keys=16,
                number_of_inactive_credentials=16,
            )
        )

    def get_nfc_access_control_point(self) -> str:
        return ""

    def set_nfc_access_control_point(self, value: str) -> str:
        packed = unpack_from_base64_string(value)
        request: ControlPointRequest = ControlPointRequest.unpack(packed)
        response = ControlPointResponse()
        if request.device_credential_request is not None:
            child = request.device_credential_request
            response.device_credential_response = (
                self.get_device_credential(child)
                if request.operation == Operation.GET
                else self.add_device_credential(child)
                if request.operation == Operation.ADD
                else self.remove_device_credential(child)
                if request.operation == Operation.REMOVE
                else None
            )
        elif request.reader_key_request is not None:
            child = request.reader_key_request
            response.reader_key_response = (
                self.get_reader_key(child)
                if request.operation == Operation.GET
                else self.add_reader_key(child)
                if request.operation == Operation.ADD
                else self.remove_reader_key(child)
                if request.operation == Operation.REMOVE
                else None
            )
        return pack_into_base64_string(response.pack())

    def get_configuration_state(self) -> int:
        return 0


class RegistrationAccessory(Accessory):
    """HomeKit lock-shaped accessory used only to provision Home Keys."""

    category = CATEGORY_DOOR_LOCK

    def __init__(
        self,
        *args,
        enrollment_service: EnrollmentService,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.enrollment_service = enrollment_service
        self._last_client_public_keys: set[bytes] | None = None
        self._add_lock_service()
        self._add_nfc_access_service()
        self._add_unpair_hook()

    def _add_service(self, name: str):
        service = self.driver.loader.get_service(name)
        self.add_service(service)
        return service

    def _add_lock_service(self) -> None:
        lock = self._add_service("LockMechanism")
        self.lock_current_state = lock.configure_char(
            "LockCurrentState", getter_callback=lambda: 1, value=1
        )
        self.lock_target_state = lock.configure_char(
            "LockTargetState",
            getter_callback=lambda: 1,
            setter_callback=self._ignore_lock_command,
            value=1,
        )
        management = self._add_service("LockManagement")
        management.configure_char(
            "LockControlPoint", setter_callback=lambda _value: None
        )
        management.configure_char("Version", getter_callback=lambda: "")

    def _ignore_lock_command(self, value: int) -> int:
        log.info(
            "Ignoring HomeKit lock command %s; accessory is registration-only",
            value,
        )
        self.lock_current_state.set_value(1, should_notify=True)
        self.lock_target_state.set_value(1, should_notify=True)
        return 1

    def _add_nfc_access_service(self) -> None:
        service = self._add_service("NFCAccess")
        service.configure_char(
            "NFCAccessSupportedConfiguration",
            getter_callback=self._get_supported_configuration,
        )
        service.configure_char(
            "NFCAccessControlPoint",
            getter_callback=self._get_control_point,
            setter_callback=self._set_control_point,
        )
        service.configure_char(
            "ConfigurationState",
            getter_callback=self._get_configuration_state,
        )

    def _refresh_pairings(self) -> None:
        public_keys = set(self.driver.state.paired_clients.values())
        if public_keys == self._last_client_public_keys:
            return
        self._last_client_public_keys = public_keys
        self.enrollment_service.update_hap_pairings(public_keys)

    def _get_supported_configuration(self) -> str:
        self._refresh_pairings()
        return (
            self.enrollment_service
            .get_nfc_access_supported_configuration()
        )

    def _get_control_point(self) -> str:
        self._refresh_pairings()
        return self.enrollment_service.get_nfc_access_control_point()

    def _set_control_point(self, value: str) -> str:
        self._refresh_pairings()
        return self.enrollment_service.set_nfc_access_control_point(value)

    def _get_configuration_state(self) -> int:
        self._refresh_pairings()
        return self.enrollment_service.get_configuration_state()

    def _add_unpair_hook(self) -> None:
        original_unpair = self.driver.unpair

        @functools.wraps(original_unpair)
        def patched_unpair(client_uuid):
            result = original_unpair(client_uuid)
            self._last_client_public_keys = None
            self._refresh_pairings()
            return result

        self.driver.unpair = patched_unpair
