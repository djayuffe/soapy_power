"""Packet continuity and sample-clock metadata, separate from host UTC."""
from soapypower.numerics import finite, integer


class ContinuityTracker:
    def __init__(self, sample_rate, tolerance_samples=0.25):
        self.rate = finite(sample_rate, 'sample rate', positive=True)
        self.tolerance_ns = max(2.0, finite(tolerance_samples, 'timestamp tolerance', nonnegative=True) * 1e9 / self.rate)
        self.packets = self.timed = self.checked = 0
        self.first_timed_packet = self.last_timed_packet = None
        self.next_time = None
        self.max_error_ns = 0.0

    def observe(self, count, has_time, timestamp=None):
        count = integer(count, 'packet count', 1)
        self.packets += 1
        current = self.next_time
        if has_time:
            timestamp = integer(timestamp, 'hardware timestamp', -(1 << 63))
            if current is not None:
                anchor, offset = current
                error = (timestamp - anchor) - offset * 1e9 / self.rate
                self.max_error_ns = max(self.max_error_ns, abs(error))
                if abs(error) > self.tolerance_ns:
                    raise RuntimeError('SDR timestamp gap: IQ is not continuous')
                self.checked += 1
            current = (timestamp, 0)
            self.timed += 1
            if self.first_timed_packet is None:
                self.first_timed_packet = self.packets
            self.last_timed_packet = self.packets
        self.next_time = None if current is None else (current[0], current[1] + count)
        return current

    def timestamp(self, position):
        if position is None:
            return None
        return position[0] + round(position[1] * 1e9 / self.rate)

    def metadata(self):
        if not self.timed:
            confidence = 'unknown'
        elif self.timed == self.packets and self.checked:
            confidence = 'verified'
        elif (self.first_timed_packet == 1 and self.last_timed_packet == self.packets and self.checked):
            confidence = 'anchored'
        else:
            confidence = 'inferred'
        return {'continuity': confidence, 'timestamped_packets': self.timed,
                'stream_packets': self.packets, 'timestamp_checks': self.checked,
                'timestamp_coverage': self.timed / self.packets if self.packets else 0,
                'max_timestamp_error_ns': self.max_error_ns,
                'timestamp_tolerance_ns': self.tolerance_ns}


def shifted(position, count):
    return None if position is None else (position[0], position[1] + count)
