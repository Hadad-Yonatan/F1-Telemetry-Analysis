"""Versioned binary format for lossless F1 UDP packet captures."""

from __future__ import annotations

import os
import socket
import struct
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator

CAPTURE_MAGIC = b"F1CAP\r\n\x1a"
CAPTURE_VERSION = 1
RECORD_MAGIC = b"PKT1"
MAX_UDP_PAYLOAD_SIZE = 65_535

# magic, format version, header size, file creation Unix timestamp (nanoseconds)
FILE_HEADER = struct.Struct("<8sHHQ")

# magic, full record size, capture timestamp, IPv4, sender port, payload length,
# CRC-32 of the payload
RECORD_HEADER = struct.Struct("<4sIQ4sHII")


class CaptureFormatError(ValueError):
    """Base exception for invalid capture files."""


class UnsupportedCaptureVersionError(CaptureFormatError):
    """Raised when a capture uses an unsupported file-format version."""


class TruncatedCaptureError(CaptureFormatError):
    """Raised when a capture ends partway through a header or record."""


@dataclass(frozen=True)
class CaptureHeader:
    version: int
    created_unix_ns: int


@dataclass(frozen=True)
class CaptureRecord:
    captured_unix_ns: int
    sender_ip: str
    sender_port: int
    payload: bytes


class CaptureWriter:
    """Append independently framed datagrams to a new capture file."""

    def __init__(
        self,
        path: str | Path,
        *,
        sync_interval: float = 1.0,
        created_unix_ns: int | None = None,
    ) -> None:
        if sync_interval < 0:
            raise ValueError("sync_interval cannot be negative")

        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._sync_interval = sync_interval
        self._last_sync = time.monotonic()
        self._file: BinaryIO = self.path.open("xb")
        self.records_written = 0
        self.payload_bytes_written = 0

        created = time.time_ns() if created_unix_ns is None else created_unix_ns
        try:
            self._file.write(
                FILE_HEADER.pack(
                    CAPTURE_MAGIC,
                    CAPTURE_VERSION,
                    FILE_HEADER.size,
                    created,
                )
            )
            self.sync()
        except BaseException:
            self._file.close()
            raise

    def write_datagram(
        self,
        payload: bytes,
        sender: tuple[str, int],
        *,
        captured_unix_ns: int | None = None,
    ) -> None:
        """Write metadata and the original payload without decoding it."""
        if self._file.closed:
            raise ValueError("cannot write to a closed capture")
        if len(payload) > MAX_UDP_PAYLOAD_SIZE:
            raise ValueError("payload exceeds the maximum UDP payload size")

        sender_ip, sender_port = sender
        if not 0 <= sender_port <= 65_535:
            raise ValueError("sender port must be between 0 and 65535")

        try:
            packed_ip = socket.inet_aton(sender_ip)
        except OSError as error:
            raise ValueError(f"sender is not an IPv4 address: {sender_ip}") from error

        timestamp = time.time_ns() if captured_unix_ns is None else captured_unix_ns
        checksum = zlib.crc32(payload) & 0xFFFFFFFF
        record_size = RECORD_HEADER.size + len(payload)
        record_header = RECORD_HEADER.pack(
            RECORD_MAGIC,
            record_size,
            timestamp,
            packed_ip,
            sender_port,
            len(payload),
            checksum,
        )

        # One buffered write keeps a process interruption confined to at most
        # the final record. Earlier records remain independently readable.
        written = self._file.write(record_header + payload)
        if written != record_size:
            raise OSError(
                f"short capture write: expected {record_size}, wrote {written}"
            )

        self.records_written += 1
        self.payload_bytes_written += len(payload)
        if time.monotonic() - self._last_sync >= self._sync_interval:
            self.sync()

    def sync(self) -> None:
        """Flush Python and operating-system buffers to durable storage."""
        if self._file.closed:
            return
        self._file.flush()
        os.fsync(self._file.fileno())
        self._last_sync = time.monotonic()

    def close(self) -> None:
        if not self._file.closed:
            try:
                self.sync()
            finally:
                self._file.close()

    def __enter__(self) -> CaptureWriter:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()


def read_capture_header(file: BinaryIO) -> CaptureHeader:
    """Read and validate a v1 file header from the current file position."""
    raw_header = file.read(FILE_HEADER.size)
    if len(raw_header) != FILE_HEADER.size:
        raise TruncatedCaptureError(
            f"capture header is truncated: expected {FILE_HEADER.size} bytes, "
            f"found {len(raw_header)}"
        )

    magic, version, header_size, created_unix_ns = FILE_HEADER.unpack(raw_header)
    if magic != CAPTURE_MAGIC:
        raise CaptureFormatError("not an F1CAP capture file")
    if version != CAPTURE_VERSION:
        raise UnsupportedCaptureVersionError(
            f"capture version {version} is not supported; expected {CAPTURE_VERSION}"
        )
    if header_size != FILE_HEADER.size:
        raise CaptureFormatError(
            f"invalid v1 header size {header_size}; expected {FILE_HEADER.size}"
        )

    return CaptureHeader(version=version, created_unix_ns=created_unix_ns)


def iter_capture(
    path: str | Path,
    *,
    allow_truncated_tail: bool = False,
) -> Iterator[CaptureRecord]:
    """Yield verified records, optionally recovering past a truncated tail."""
    with Path(path).open("rb") as file:
        read_capture_header(file)

        while True:
            record_offset = file.tell()
            raw_header = file.read(RECORD_HEADER.size)
            if not raw_header:
                return
            if len(raw_header) != RECORD_HEADER.size:
                if allow_truncated_tail:
                    return
                raise TruncatedCaptureError(
                    f"record header at byte {record_offset} is truncated"
                )

            (
                magic,
                record_size,
                captured_unix_ns,
                packed_ip,
                sender_port,
                payload_length,
                expected_checksum,
            ) = RECORD_HEADER.unpack(raw_header)

            if magic != RECORD_MAGIC:
                raise CaptureFormatError(
                    f"invalid record magic at byte {record_offset}"
                )
            if payload_length > MAX_UDP_PAYLOAD_SIZE:
                raise CaptureFormatError(
                    f"invalid payload length {payload_length} at byte {record_offset}"
                )
            expected_record_size = RECORD_HEADER.size + payload_length
            if record_size != expected_record_size:
                raise CaptureFormatError(
                    f"invalid record size {record_size} at byte {record_offset}; "
                    f"expected {expected_record_size}"
                )

            payload = file.read(payload_length)
            if len(payload) != payload_length:
                if allow_truncated_tail:
                    return
                raise TruncatedCaptureError(
                    f"payload at byte {record_offset} is truncated: expected "
                    f"{payload_length} bytes, found {len(payload)}"
                )

            actual_checksum = zlib.crc32(payload) & 0xFFFFFFFF
            if actual_checksum != expected_checksum:
                raise CaptureFormatError(
                    f"payload checksum mismatch at byte {record_offset}"
                )

            yield CaptureRecord(
                captured_unix_ns=captured_unix_ns,
                sender_ip=socket.inet_ntoa(packed_ip),
                sender_port=sender_port,
                payload=payload,
            )
