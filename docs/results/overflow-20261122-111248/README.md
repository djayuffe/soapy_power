# B200mini overflow A/B (140-150 MHz hop scan, 2 MS/s, 150 s per variant, antenna)
Run via `queue.sh scan --seconds 150 --overflow-probe 1`; device string differs only in num_recv_frames.

| device string            | seconds | records | settle-phase overflows | outcome |
|--------------------------|---------|---------|------------------------|---------|
| default (no frame args)  | 74      | 117     | 13                     | FAILED: measurement-phase SDR overflow |
| num_recv_frames=64       | 152     | 248     | 3                      | completed (exit 2 = partial_deadline at the 150 s limit, not an overflow) |
| num_recv_frames=512      | 152     | 248     | 0                      | completed (same deadline exit) |

Earlier 40 s run: default 3 settle overflows; 64 and 512 none.
num_recv_frames is a UHD device-open arg (USB ring), not a stream arg.
