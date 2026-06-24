"""traintap — RTL-SDR End/Head-of-Train (EOT/HOT) telemetry decoder."""

__version__ = "0.1.0"

# Channel center frequencies (North America, AAR S-9152).
EOT_FREQ_HZ = 457_937_500   # rear -> loco telemetry (frequent)
HOT_FREQ_HZ = 452_937_500   # loco -> rear commands (infrequent)
DPU_FREQ_HZ = 457_925_000   # distributed power (future)
