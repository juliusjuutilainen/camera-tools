"""Headless entry point used by the Lightroom plug-in's bundled executable."""

from camera_tools.lightroom import main


if __name__ == "__main__":
    raise SystemExit(main())
