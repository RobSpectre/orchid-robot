"""Launch the local operator console. Simulation is the default."""
import argparse
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--enable-hardware", action="store_true", help="explicitly allow connections to physical motors")
    parser.add_argument("--port", type=int, default=8080, help="local web port (default: 8080)")
    parser.add_argument("--studio-port", type=int, default=8765,
                        help="with hardware, check each play's notes with Orchid Studio's key monitor on this port (0: off)")
    parser.add_argument("--data-dir", type=Path,
                        default=Path(__file__).resolve().parent.parent / "data")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("Web port must be 1..65535")
    from .api import create_app
    import uvicorn
    mode = "hardware" if args.enable_hardware else "simulation"
    print(f"Orchid · {mode}\nOpen http://127.0.0.1:{args.port}\nData: {args.data_dir / mode}", flush=True)
    uvicorn.run(create_app(args.data_dir, mode, studio_port=args.studio_port if args.enable_hardware else None), host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
