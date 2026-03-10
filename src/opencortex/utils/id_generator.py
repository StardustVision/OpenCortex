# src/opencortex/utils/id_generator.py
# SPDX-License-Identifier: Apache-2.0
"""
Distributed unique ID generator based on Twitter's Snowflake algorithm.

Generates 64-bit integers suitable for Qdrant point IDs and future
distributed storage migration.

Structure (64 bits):
  1 bit unused | 41 bits timestamp(ms) | 5 bits datacenter | 5 bits worker | 12 bits sequence
"""
import os
import random
import threading
import time


class SnowflakeGenerator:
    EPOCH = 1704067200000  # 2024-01-01 00:00:00 UTC

    worker_id_bits = 5
    datacenter_id_bits = 5
    sequence_bits = 12

    max_worker_id = -1 ^ (-1 << worker_id_bits)          # 31
    max_datacenter_id = -1 ^ (-1 << datacenter_id_bits)   # 31
    max_sequence = -1 ^ (-1 << sequence_bits)              # 4095

    worker_id_shift = sequence_bits                        # 12
    datacenter_id_shift = sequence_bits + worker_id_bits   # 17
    timestamp_left_shift = sequence_bits + worker_id_bits + datacenter_id_bits  # 22

    def __init__(self, worker_id: int = None, datacenter_id: int = None):
        if worker_id is None:
            worker_id = os.getpid() & self.max_worker_id
        if datacenter_id is None:
            datacenter_id = random.randint(0, self.max_datacenter_id)

        if not (0 <= worker_id <= self.max_worker_id):
            raise ValueError(f"worker_id must be 0..{self.max_worker_id}")
        if not (0 <= datacenter_id <= self.max_datacenter_id):
            raise ValueError(f"datacenter_id must be 0..{self.max_datacenter_id}")

        self.worker_id = worker_id
        self.datacenter_id = datacenter_id
        self.sequence = 0
        self.last_timestamp = -1
        self.lock = threading.Lock()

    def _current_timestamp(self) -> int:
        return int(time.time() * 1000)

    def next_id(self) -> int:
        with self.lock:
            timestamp = self._current_timestamp()

            if timestamp < self.last_timestamp:
                offset = self.last_timestamp - timestamp
                if offset <= 5:
                    time.sleep(offset / 1000.0 + 0.001)
                    timestamp = self._current_timestamp()
                if timestamp < self.last_timestamp:
                    raise RuntimeError(
                        f"Clock moved backwards by {self.last_timestamp - timestamp}ms"
                    )

            if self.last_timestamp == timestamp:
                self.sequence = (self.sequence + 1) & self.max_sequence
                if self.sequence == 0:
                    while timestamp <= self.last_timestamp:
                        timestamp = self._current_timestamp()
            else:
                self.sequence = 0

            self.last_timestamp = timestamp

            return (
                ((timestamp - self.EPOCH) << self.timestamp_left_shift)
                | (self.datacenter_id << self.datacenter_id_shift)
                | (self.worker_id << self.worker_id_shift)
                | self.sequence
            )


_default_generator = SnowflakeGenerator()


def generate_id() -> int:
    """Generate a globally unique 64-bit integer ID."""
    return _default_generator.next_id()
