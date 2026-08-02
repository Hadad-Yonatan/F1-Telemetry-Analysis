# F1CAP capture format, version 1

F1CAP is an append-only binary container for preserving UDP datagrams before
decoding them. All integer fields use little-endian byte order. Timestamps are
Unix time in nanoseconds.

## File header

| Field | Type | Value |
| --- | --- | --- |
| Magic | 8 bytes | `F1CAP\r\n\x1a` |
| Version | unsigned 16-bit | `1` |
| Header size | unsigned 16-bit | `20` |
| Created timestamp | unsigned 64-bit | Unix nanoseconds |

## Datagram record

| Field | Type | Description |
| --- | --- | --- |
| Magic | 4 bytes | `PKT1` |
| Record size | unsigned 32-bit | Header plus payload bytes |
| Capture timestamp | unsigned 64-bit | Receive time in Unix nanoseconds |
| Sender IPv4 | 4 bytes | Network-order IPv4 address |
| Sender port | unsigned 16-bit | UDP source port |
| Payload length | unsigned 32-bit | Number of original UDP bytes |
| Payload CRC-32 | unsigned 32-bit | Checksum of the original payload |
| Payload | variable bytes | Original datagram, unchanged |

The record header is 30 bytes. Record size and payload length are both stored
so readers can validate framing. CRC-32 detects damaged payloads.

## Interruption and recovery

The recorder appends a complete header-and-payload record in one buffered
write, flushes and calls `fsync` periodically, and performs a final durable
flush after Control+C. A sudden process or power interruption can therefore
affect the final record without making earlier records undecodable.

Readers reject truncation by default rather than silently accepting damaged
input. Passing `--allow-truncated-tail` to the replay command recovers all
complete, checksum-valid records before an interrupted final record.

Capture validity never depends on decoding the F1 packet header. Short packets,
unknown packet IDs, and game formats other than 2025 are preserved exactly.
