import argparse
import shutil
import subprocess
import tempfile
from pathlib import Path

MAINTAINER = "Lisselde_E <Lisselde.E@outlook.com>"
ARCH = "amd64"
DEPENDS = "libxcb-cursor0, libgl1, libxkbcommon-x11-0"
INSTALL_ROOT = "/usr/lib"


def build(args) -> None:
    source_dir = Path(args.source_dir).resolve()
    icon = Path(args.icon).resolve()
    output = Path(args.output).resolve()
    if not source_dir.is_dir():
        raise SystemExit(f"missing source dir: {source_dir}")
    if not (source_dir / args.package).is_file():
        raise SystemExit(f"missing executable: {source_dir / args.package}")
    if not icon.is_file():
        raise SystemExit(f"missing icon: {icon}")

    staging = Path(tempfile.mkdtemp(prefix="deb-"))
    try:
        lib_dir = staging / INSTALL_ROOT.lstrip("/") / args.package
        shutil.copytree(source_dir, lib_dir)
        (lib_dir / args.package).chmod(0o755)

        bin_dir = staging / "usr/bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / args.package).symlink_to(f"{INSTALL_ROOT}/{args.package}")

        apps_dir = staging / "usr/share/applications"
        apps_dir.mkdir(parents=True)
        (apps_dir / args.desktop_name).write_text(
            "[Desktop Entry]\n"
            "Type=Application\n"
            f"Name={args.app_name}\n"
            f"Exec=/usr/bin/{args.package}\n"
            f"Icon={args.package}\n"
            "Terminal=false\n"
            "Categories=Utility;Network;\n",
            encoding="utf-8",
        )

        icon_dir = staging / "usr/share/icons/hicolor/256x256/apps"
        icon_dir.mkdir(parents=True)
        shutil.copy2(icon, icon_dir / f"{args.package}.png")

        installed_size = sum(f.stat().st_size for f in staging.rglob("*") if f.is_file()) // 1024
        debian = staging / "DEBIAN"
        debian.mkdir()
        (debian / "control").write_text(
            f"Package: {args.package}\n"
            f"Version: {args.version}\n"
            f"Architecture: {ARCH}\n"
            f"Maintainer: {MAINTAINER}\n"
            "Section: utils\n"
            "Priority: optional\n"
            f"Installed-Size: {installed_size}\n"
            f"Depends: {DEPENDS}\n"
            f"Description: {args.app_name} - LAN file sync tool\n"
            " Real-time file synchronization over local network.\n",
            encoding="utf-8",
        )

        output.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["dpkg-deb", "--build", "--root-owner-group", str(staging), str(output)],
            check=True,
        )
        print(f"deb: {output}")
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-name", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--source-dir", required=True)
    parser.add_argument("--icon", required=True)
    parser.add_argument("--package", required=True)
    parser.add_argument("--desktop-name", required=True)
    parser.add_argument("--output", required=True)
    build(parser.parse_args())


if __name__ == "__main__":
    main()