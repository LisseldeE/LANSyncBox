import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT))

from config import Config

COPYRIGHT_YEAR = "2026"


def _version_tuple(version: str) -> tuple:
    nums = [int(n) for n in re.findall(r'\d+', version)[:4]]
    while len(nums) < 4:
        nums.append(0)
    return tuple(nums)


def main():
    ver = _version_tuple(Config.APP_VERSION)
    ver_str = '.'.join(str(x) for x in ver)
    copyright_text = f"Copyright (c) {COPYRIGHT_YEAR} {Config.APP_AUTHOR}"
    content = f"""# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={ver},
    prodvers={ver},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo(
      [
        StringTable(
          '080404B0',
          [StringStruct('CompanyName', '{Config.APP_AUTHOR}'),
           StringStruct('FileDescription', '{Config.APP_NAME}'),
           StringStruct('FileVersion', '{ver_str}'),
           StringStruct('InternalName', '{Config.APP_NAME}'),
           StringStruct('LegalCopyright', '{copyright_text}'),
           StringStruct('OriginalFilename', '{Config.APP_NAME}.exe'),
           StringStruct('ProductName', '{Config.APP_NAME}'),
           StringStruct('ProductVersion', '{ver_str}')])
      ]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
"""
    out = Path(__file__).resolve().parent / 'version_info.txt'
    out.write_text(content, encoding='utf-8')
    print(f"version_info: {out}")
    print(f"version: {ver_str}")
    print(f"name: {Config.APP_NAME}")


if __name__ == '__main__':
    main()