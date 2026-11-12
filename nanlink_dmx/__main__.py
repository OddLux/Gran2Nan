"""Entry point: GUI by default, `--headless` for a console-only bridge."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time


def run_headless():
    from .config import CONFIG_PATH, load_config
    from .engine import Bridge

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    cfg = load_config()
    if not cfg.mesh.net_key:
        sys.exit(f"No mesh keys in {CONFIG_PATH} - run the GUI once to set things up.")

    last = {"t": 0.0}

    def emit(kind, data):
        if kind == "log":
            logging.info(data)
        elif kind == "status" and (data["rate"] or data["pending"]):
            now = time.monotonic()
            if now - last["t"] >= 1.0:
                last["t"] = now
                outputs = "  ".join(",".join(f"{k}={v}" for k, v in sent.items()) for _, sent in data["fixtures"])
                logging.info("%s · %d msg/s · %d queued · %s", "Art-Net" if data["artnet_live"] else "manual",
                             data["rate"], data["pending"], outputs)

    async def main():
        bridge = Bridge(cfg, emit)
        await bridge.start()
        try:
            await asyncio.Event().wait()
        finally:
            await bridge.stop()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


def main():
    parser = argparse.ArgumentParser(prog="Gran2Nan", description="Gran2Nan - Art-Net to Nanlink WS-TB-1 bridge")
    parser.add_argument("--headless", action="store_true", help="run without a window (uses config.json)")
    args = parser.parse_args()
    if args.headless:
        run_headless()
    else:
        from .gui import main as gui_main
        gui_main()


if __name__ == "__main__":
    main()
