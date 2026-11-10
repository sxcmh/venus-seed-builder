#!/usr/bin/env python3
"""Build an iOS Data seed (DBRDISK1) containing the /private/var skeleton.

macOS only: hdiutil creates a small APFS volume, this script writes the
/private/var skeleton (directories, ownership, the tzinit links and the
skip-setup preferences), the raw image is extended to the requested size and
`diskutil apfs resizeContainer` grows the container to fill it, and the result
is zero-page packed into the DBRDISK1 format the DarwinBoot loader and
NativeRamBlock expect.

The volume is created and populated by Apple's own APFS implementation; only the
directory list is ours (the supplied IPSW OS image has no Data volume, so there
is nothing to copy it from).

  make_data_seed.py --build 20H392 --gib 2 --output data-seed-20H392.dbr
  make_data_seed.py --pack-only seed.raw data-seed.dbr     (host self-test)
"""
import argparse
import hashlib
import os
import plistlib
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

PAGE = 4096
MIB = 1 << 20
GIB = 1 << 30
MAX_INPUT = 256 * MIB
VOLUME_NAME = 'OwnerData'
MOBILE = 501

# (relative path, uid, gid, mode). Ownership is applied in one pass at the end.
DIRS = [
    ('db', 0o755),
    ('db/timezone', 0o755),
    ('db/timezone/tz', 0o755),
    ('db/icu', 0o755),
    ('log', 0o755),
    ('lib', 0o755),
    ('spool', 0o755),
    ('vm', 0o755),
    ('audit', 0o755),
    ('at', 0o755),
    ('cron', 0o755),
    ('backups', 0o755),
    ('cache', 0o755),
    ('networkd', 0o755),
    ('preferences', 0o755),
    ('protected', 0o700),
    ('run', 0o755),
    ('tmp', 0o1777),
    ('keybags', 0o700),
    ('root', 0o700),
    ('containers', 0o755),
    ('containers/Bundle', 0o755),
    ('containers/Data', 0o755),
    ('containers/Data/Application', 0o755),
    ('containers/Data/Library', 0o755),
    ('containers/Data/System', 0o755),
    ('containers/Shared', 0o755),
    ('containers/Shared/SystemGroup', 0o755),
    ('containers/Temp', 0o755),
    ('mobile', 0o755),
    ('mobile/tmp', 0o755),
    ('mobile/Documents', 0o755),
    ('mobile/Media', 0o755),
    ('mobile/Library', 0o755),
    ('mobile/Library/Preferences', 0o755),
    ('mobile/Library/Caches', 0o755),
]

# tzinit (com.apple.tzinit, POSIX_SPAWN_PANIC_ON_NON_ZERO_EXIT) resolves
# /usr/share/zoneinfo and /var/db/timezone/localtime through these.
LINKS = [
    ('db/timezone/zoneinfo', '/usr/share/zoneinfo.default'),
    ('db/timezone/localtime', 'zoneinfo/UTC'),
]

PURPLEBUDDY = dict(SetupDone=True, SetupFinishedAllSteps=True, SetupVersion=11,
                   daemonizeNeedsToRun=False)
PURPLEBUDDY_NOTBACKEDUP = {name: False for name in (
    'ShouldRunDiagnosticsMiniBuddy', 'ShouldRunHSA2MiniBuddy', 'ShouldRunKeychainSyncMiniBuddy',
    'ShouldRunMesaMiniBuddy', 'ShouldRunPaymentMiniBuddy', 'ShouldRunStandardMiniBuddy',
    'SilentICDPUpgrade')}
MANAGED_NOTBACKEDUP = dict(LockdownActivationIndicatesCloudConfigurationAvailable=False)
SPRINGBOARD = dict(BKNoWatchdogs=True, FBLaunchWatchdogExceptions={'com.apple.purplebuddy': 100.0},
                   MCForceShowCloudConfigurationUI=False)
CA_FLAGS = dict(CAEnableDeepFramebuffer=False, CAEnableFramebufferCompression=False,
                CAEnableIOSurfaceCompression=False)

# (home, preferences file, values); home is 'mobile' or 'root'.
PREFERENCES = [
    ('mobile', 'com.apple.purplebuddy', PURPLEBUDDY),
    ('mobile', 'com.apple.purplebuddy.notbackedup', PURPLEBUDDY_NOTBACKEDUP),
    ('mobile', 'com.apple.managedconfiguration.notbackedup', MANAGED_NOTBACKEDUP),
    ('mobile', 'com.apple.springboard', SPRINGBOARD),
    ('root', 'backboardd', CA_FLAGS),
    ('root', '.GlobalPreferences', CA_FLAGS),
    ('mobile', 'backboardd', CA_FLAGS),
    ('mobile', '.GlobalPreferences', CA_FLAGS),
]


def run(*args):
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode:
        raise SystemExit(f"{' '.join(args)} failed ({result.returncode}): "
                         f"{(result.stderr or result.stdout).strip()}")
    return result.stdout


def sudo(*args):
    result = subprocess.run(['sudo', *args], capture_output=True, text=True)
    if result.returncode:
        raise SystemExit(f"sudo {' '.join(args)} failed ({result.returncode}): "
                         f"{(result.stderr or result.stdout).strip()}")
    return result.stdout


def create_volume(image, gib):
    run('hdiutil', 'create', '-size', f'{gib}g', '-layout', 'NONE', '-fs', 'APFS',
        '-volname', VOLUME_NAME, '-type', 'UDTO', str(image))


def populate(image, build):
    mount = image.parent / 'mount'
    mount.mkdir()
    attached = plistlib.loads(run('hdiutil', 'attach', '-nobrowse', '-owners', 'on',
                                  '-mountpoint', str(mount), '-plist', str(image)))
    device = attached['system-entities'][0]['dev-entry']
    root = Path(mount)
    try:
        if root.is_symlink() or not root.is_dir() or not (root / '.fseventsd').parent.is_dir():
            raise SystemExit('attach did not produce exactly one mounted volume')
        for name, mode in DIRS:
            path = root / name
            path.mkdir(parents=True, exist_ok=True)
            os.chmod(path, mode)
        for name, target in LINKS:
            path = root / name
            if path.is_symlink() or path.exists():
                path.unlink()
            os.symlink(target, path)
        for home, name, values in PREFERENCES:
            if home == 'mobile':
                folder = root / 'mobile/Library/Preferences'
            else:
                folder = root / 'root/Library/Preferences'
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / (name + '.plist')
            data = plistlib.loads(path.read_bytes()) if path.is_file() else {}
            data.update(values)
            path.write_bytes(plistlib.dumps(data, fmt=plistlib.FMT_BINARY))
            os.chmod(path, 0o600)
        migration = root / 'mobile/Library/Preferences/com.apple.migration.plist'
        data = plistlib.loads(migration.read_bytes()) if migration.is_file() else {}
        data.update(LastSystemVersion=build,
                    DMUserDataDisposition=dict(SyntheticDidUpgrade=False, buildVersion=build))
        migration.write_bytes(plistlib.dumps(data, fmt=plistlib.FMT_BINARY))
        os.chmod(migration, 0o600)
        # One ownership pass while the volume is still mounted: everything
        # root:wheel, the mobile tree as on a device, and the sticky tmp dirs.
        sudo('chown', '-R', '0:0', str(root))
        sudo('chown', '-R', f'{MOBILE}:{MOBILE}', str(root / 'mobile'))
        sudo('chmod', '1777', str(root / 'tmp'))
        sudo('chmod', '1777', str(root / 'mobile/tmp'))
        # Read the stored values back: an owners-off mount silently keeps 99:99.
        checks = [(root / 'db', 0, 0, 0o755), (root / 'mobile', MOBILE, MOBILE, 0o755),
                  (root / 'tmp', 0, 0, 0o1777), (root / 'keybags', 0, 0, 0o700)]
        report = []
        for path, uid, gid, mode in checks:
            info = path.stat()
            report.append(f'{path.name}:{info.st_uid}:{info.st_gid}:{info.st_mode & 0o7777:o}')
            if (info.st_uid, info.st_gid) != (uid, gid) or (info.st_mode & 0o7777) != mode:
                raise SystemExit('ownership/mode did not persist: ' + ' '.join(report))
        print('seed ownership ' + ' '.join(report))
    finally:
        subprocess.run(['hdiutil', 'detach', str(device)], capture_output=True)


def disk_prefix(image, work):
    """Copy the page-aligned disk bytes (UDTO may append a 512-byte trailer) and check the container."""
    size = image.stat().st_size
    disk_bytes = size - (size % PAGE)
    if disk_bytes <= 0:
        raise SystemExit('the created image is empty')
    disk = work / 'disk.raw'
    with image.open('rb') as src, disk.open('wb') as dst:
        remaining = disk_bytes
        while remaining:
            chunk = src.read(min(8 * MIB, remaining))
            if not chunk:
                raise SystemExit('short read while copying the seed image')
            dst.write(chunk)
            remaining -= len(chunk)
    header = disk.open('rb').read(4096)
    if header[32:36] != b'NXSB':
        raise SystemExit('the seed image does not start with an APFS container')
    block_size = int.from_bytes(header[36:40], 'little')
    blocks = int.from_bytes(header[40:48], 'little')
    if block_size != PAGE or blocks * block_size > disk_bytes or blocks * block_size < disk_bytes - 64 * MIB:
        raise SystemExit(f'the container holds {blocks * block_size} bytes of a {disk_bytes}-byte image')
    return disk


def pack(raw, output):
    """Zero-page packing: keep only nonzero 4096-byte pages, merged into extents."""
    logical = raw.stat().st_size
    if not 0 < logical <= 1 << 40 or logical % PAGE:
        raise SystemExit(f'image size {logical} is not a page multiple')
    extents = []
    with raw.open('rb') as handle:
        offset = 0
        while offset < logical:
            handle.seek(offset)
            page = handle.read(PAGE)
            if len(page) != PAGE:
                raise SystemExit('short read')
            if page.count(0) == PAGE:
                offset += PAGE
                continue
            start = offset
            length = PAGE
            while offset + length < logical:
                handle.seek(offset + length)
                following = handle.read(PAGE)
                if len(following) != PAGE:
                    raise SystemExit('short read')
                if following.count(0) == PAGE:
                    break
                length += PAGE
            extents.append((start, length))
            offset += length
    if len(extents) > 65536:
        raise SystemExit('too many extents for the DBRDISK1 table')
    body = bytearray()
    table = bytearray()
    with raw.open('rb') as handle:
        for current, length in extents:
            table += struct.pack('<QQ', current, length)
            handle.seek(current)
            body += handle.read(length)
    header = struct.pack('<8sIIQQ', b'DBRDISK1', 1, PAGE, logical, len(extents))
    blob = header + bytes(table) + bytes(body)
    if len(blob) > MAX_INPUT:
        raise SystemExit('packed seed exceeds the 256 MiB input limit')
    output.write_bytes(blob)
    verify(blob, raw)
    return dict(logical=logical, extents=len(extents), pages=sum(l // PAGE for _, l in extents),
                bytes=len(blob), sha256=hashlib.sha256(blob).hexdigest())


def verify(blob, raw):
    """Re-parse the written seed and prove every kept extent matches the source image."""
    magic, version, page, logical, count = struct.unpack_from('<8sIIQQ', blob)
    if (magic, version, page) != (b'DBRDISK1', 1, PAGE) or logical != raw.stat().st_size:
        raise SystemExit('packed seed header does not describe the source image')
    cursor = 32 + 16 * count
    end = 0
    with raw.open('rb') as handle:
        for index in range(count):
            offset, length = struct.unpack_from('<QQ', blob, 32 + 16 * index)
            if (not length or (offset | length) % PAGE or offset < end or
                    offset > logical or length > logical - offset):
                raise SystemExit(f'packed extent {index} is invalid')
            handle.seek(offset)
            if handle.read(length) != blob[cursor:cursor + length]:
                raise SystemExit(f'packed extent {index} disagrees with the source image')
            cursor += length
            end = offset + length
    if cursor != len(blob):
        raise SystemExit('packed seed has trailing bytes')


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--build', default='20H392', help='OS build recorded in LastSystemVersion')
    parser.add_argument('--gib', type=int, default=2, help='container size in GiB (1..1024)')
    parser.add_argument('--output', type=Path, help='DBRDISK1 output path')
    parser.add_argument('--pack-only', nargs=2, metavar=('RAW', 'DBR'),
                        help='host self-test: pack an existing raw image, no hdiutil')
    args = parser.parse_args()

    if args.pack_only:
        result = pack(Path(args.pack_only[0]), Path(args.pack_only[1]))
        for key, value in result.items():
            print(f'{key}: {value}')
        return

    if not args.output:
        parser.error('--output is required unless --pack-only is used')
    if not 1 <= args.gib <= 1024:
        parser.error('--gib must be in 1..1024')
    if args.output.exists():
        parser.error(f'refuse to overwrite existing {args.output}')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='data-seed-', dir=str(args.output.parent)) as td:
        work = Path(td)
        raw = work / 'seed.cdr'
        create_volume(raw, args.gib)
        populate(raw, args.build)
        result = pack(disk_prefix(raw, work), args.output)
    print(f'output: {args.output}')
    for key, value in result.items():
        print(f'{key}: {value}')


if __name__ == '__main__':
    main()
