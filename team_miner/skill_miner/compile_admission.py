"""Native Compile admission protocol; the host supplies durable reservations."""

OPTIMIZED_PROTOCOL = "te-mining-optimized.v1"
MAX_NATIVE_COMPILES = 2
TERMINAL_NATIVE_STATES = {"completed", "succeeded", "success", "failed", "cancelled", "canceled"}
