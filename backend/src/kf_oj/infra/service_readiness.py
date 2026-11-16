"""One bounded startup notification from the initialized ordinary main process."""

import os
import socket


def notify_ready() -> bool:
    address = os.environ.pop("NOTIFY_SOCKET", None)
    if address is None:
        return False
    if (
        len(address) < 2
        or address[0] not in "/@"
        or len(os.fsencode(address)) > 107
        or any(ord(character) < 32 or ord(character) == 127 for character in address)
    ):
        raise ValueError("invalid service notification socket")
    destination = "\0" + address[1:] if address.startswith("@") else address
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as channel:
        channel.settimeout(1)
        if channel.sendto(b"READY=1", destination) != len(b"READY=1"):
            raise OSError("incomplete service startup notification")
    return True
