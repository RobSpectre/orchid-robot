#!/usr/bin/env python3
"""Compatibility entry point. New operators should run `python app.py`."""
import sys
from orchid_demo import motion

if __name__ == "__main__":
    try:
        motion.main()
    except (Exception, KeyboardInterrupt) as exc:
        print(f"Stopped: {exc or type(exc).__name__}", file=sys.stderr)
        sys.exit(1)
else:
    # Preserve imports and mocks used by the original safety tests and scripts.
    sys.modules[__name__] = motion
