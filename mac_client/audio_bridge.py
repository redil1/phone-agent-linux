"""PhoneAgent Audio Bridge.

Handles low-latency full-duplex 16kHz 16-bit Linear PCM audio streaming
between Mac and the phone over USB sockets.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from collections.abc import Callable

logger = logging.getLogger("PhoneAgentAudioBridge")

SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH = 2  # 16-bit PCM = 2 bytes per sample
CHUNK_SIZE = 640  # 20ms of audio (16000 * 0.02 * 2 = 640 bytes)


class AudioStreamBridge:
    """Manages audio reception (Rx) and transmission (Tx) over USB TCP sockets."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        rx_port: int = 8766,
        tx_port: int = 8767,
    ) -> None:
        self.host = host
        self.rx_port = rx_port
        self.tx_port = tx_port

        self._rx_socket: socket.socket | None = None
        self._tx_socket: socket.socket | None = None

        self._rx_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._rx_callbacks: list[Callable[[bytes], None]] = []
        self.received_bytes = 0
        self.sent_bytes = 0

    def connect_rx(self, timeout: float = 3.0) -> bool:
        """Connects to the Downlink Audio stream (Caller -> Mac)."""
        try:
            self._rx_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._rx_socket.settimeout(timeout)
            self._rx_socket.connect((self.host, self.rx_port))
            self._rx_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            if self._rx_callbacks and (self._rx_thread is None or not self._rx_thread.is_alive()):
                self._start_rx_worker()
            logger.info("Connected to Downlink Audio stream on %s:%d", self.host, self.rx_port)
            return True
        except Exception as exc:
            logger.warning("Downlink audio socket not active yet on %d: %s", self.rx_port, exc)
            return False

    def connect_tx(self, timeout: float = 3.0) -> bool:
        """Connects to the Uplink Audio stream (Mac -> Cellular call)."""
        try:
            self._tx_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._tx_socket.settimeout(timeout)
            self._tx_socket.connect((self.host, self.tx_port))
            self._tx_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            logger.info("Connected to Uplink Audio stream on %s:%d", self.host, self.tx_port)
            return True
        except Exception as exc:
            logger.warning("Uplink audio socket not active yet on %d: %s", self.tx_port, exc)
            return False

    def on_audio_received(self, callback: Callable[[bytes], None]) -> None:
        """Registers a callback for incoming 16kHz PCM audio chunks."""
        self._rx_callbacks.append(callback)
        if self._rx_thread is None or not self._rx_thread.is_alive():
            self._start_rx_worker()

    def send_audio_chunk(self, pcm_bytes: bytes) -> None:
        """Sends a synthesized PCM audio chunk to the phone uplink."""
        if self._tx_socket:
            try:
                self._tx_socket.sendall(pcm_bytes)
                self.sent_bytes += len(pcm_bytes)
            except Exception as exc:
                logger.error("Failed to send audio chunk: %s", exc)

    def _start_rx_worker(self) -> None:
        self._stop_event.clear()
        self._rx_thread = threading.Thread(target=self._rx_loop, daemon=True)
        self._rx_thread.start()

    def _rx_loop(self) -> None:
        while not self._stop_event.is_set():
            if self._rx_socket is None:
                time.sleep(0.5)
                continue
            try:
                chunk = self._rx_socket.recv(CHUNK_SIZE)
                if not chunk:
                    logger.info("Downlink audio peer closed the stream")
                    return
                self.received_bytes += len(chunk)
                for cb in self._rx_callbacks:
                    try:
                        cb(chunk)
                    except Exception as exc:
                        logger.exception("Error in audio rx callback: %s", exc)
            except TimeoutError:
                continue
            except Exception as exc:
                if not self._stop_event.is_set():
                    logger.debug("Audio rx read error: %s", exc)
                time.sleep(0.1)

    def close(self) -> None:
        """Closes all audio sockets."""
        self._stop_event.set()
        if self._rx_socket:
            try:
                self._rx_socket.close()
            except Exception:
                pass
            self._rx_socket = None
        if self._tx_socket:
            try:
                self._tx_socket.close()
            except Exception:
                pass
            self._tx_socket = None
        if self._rx_thread and self._rx_thread.is_alive():
            self._rx_thread.join(timeout=1)
        self._rx_thread = None

    @property
    def rx_connected(self) -> bool:
        return self._rx_socket is not None

    @property
    def tx_connected(self) -> bool:
        return self._tx_socket is not None
