"""Real-tool compatibility checks; run in the built worker image with a checkout.

All fixtures and outputs are disposable. Every child uses the production
launcher. This intentionally fails when expected image tools are missing.
"""

import bz2
import gzip
import lzma
import struct
import sys
import tarfile
import tempfile
import wave
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from worker.arcworker.tools import archives, documents, extraction, flux, images_common, media_transcode  # noqa: E402
from worker.arcworker.tools.base import run_tool  # noqa: E402
from worker.arcworker.tools.process import check_sandbox  # noqa: E402


def successful(result):
    if not result.get('success'):
        raise AssertionError(result)


def main():
    check_sandbox()
    with tempfile.TemporaryDirectory(prefix='sandbox-compat-') as tmp:
        root = Path(tmp)
        source = root / 'source'
        source.mkdir()
        payload = source / 'payload.txt'
        payload.write_text('sandbox compatibility\n')
        # Exercise runtime startup, including Java and tools without encoders.
        for cmd in (
            ['riscosarc'], ['tbafs-extractor', '--help'], ['DiscImageManager', '--help'],
            ['imgviz', '--help'], ['hxcfe', '-help'], ['gw', '--help'],
            ['wmf-cli', '--help'], ['/opt/dexvert/emf2svg.py', '--help'],
            ['replay-transcode', '--help'], ['fcfs2raw', '--help'],
            ['unrar'], ['antiword', '-h'],
            ['catdoc', '-h'], ['xls2csv', '-h'], ['catppt', '-h'],
        ):
            result = run_tool(cmd, timeout=30, write_dirs=())
            if result.returncode not in (0, 1, 2, 7) or b'Permission denied' in result.stderr:
                raise AssertionError((cmd, result.returncode, result.stderr))
        print('Tool runtimes: OK', flush=True)

        zip_path = source / 'sample.zip'
        with zipfile.ZipFile(zip_path, 'w') as archive:
            archive.write(payload, payload.name)
        tar_path = source / 'sample.tar.gz'
        with tarfile.open(tar_path, 'w:gz') as archive:
            archive.add(payload, arcname=payload.name)
        for name, path, extract in (
            ('zip', zip_path, archives.extract_zip),
            ('riscosarc', zip_path, archives.extract_riscosarc),
            ('tar', tar_path, lambda p, d: archives.extract_tar(p, d, 'tar_gz')),
        ):
            destination = root / name
            successful(extract(path, destination))
            assert (destination / payload.name).read_bytes() == payload.read_bytes()
        for name, create, extract in (
            ('7z', ['7z', 'a'], archives.extract_7z),
            ('arj', ['arj', 'a', '-y'], archives.extract_arj),
        ):
            path = source / f'sample.{name}'
            result = run_tool([*create, str(path), payload.name], cwd=str(source), write_dirs=(source,))
            assert result.returncode == 0, result.stderr
            destination = root / name
            successful(extract(path, destination))
            assert (destination / payload.name).read_bytes() == payload.read_bytes()
        # Level-0 stored LHA member with CRC-16/IBM.
        content = payload.read_bytes()
        filename = payload.name.encode()
        crc = 0
        for byte in content:
            crc ^= byte
            for _ in range(8):
                crc = (crc >> 1) ^ (0xA001 if crc & 1 else 0)
        header = bytearray(bytes((22 + len(filename), 0)) + b'-lh0-'
                           + struct.pack('<III', len(content), len(content), 0)
                           + bytes((0x20, 0, len(filename))) + filename + struct.pack('<H', crc))
        header[1] = sum(header[2:]) & 255
        lha = source / 'sample.lzh'
        lha.write_bytes(header + content + b'\0')
        successful(archives.extract_lha(lha, root / 'lha'))
        assert (root / 'lha' / payload.name).read_bytes() == content
        print('Archive extraction (ZIP, Java riscosarc, compressed tar, 7z, ARJ, LHA): OK', flush=True)

        for compressor, encoded in (
            ('gzip', gzip.compress(b'payload')), ('bzip2', bz2.compress(b'payload')),
            ('xz', lzma.compress(b'payload')),
        ):
            path = source / f'compressed.{compressor}'
            path.write_bytes(encoded)
            target = root / f'decoded-{compressor}'
            successful(archives.decompress_single_file(path, target, compressor))
            assert target.read_bytes() == b'payload'
        result = run_tool(['zstd', '-c', str(payload)], write_dirs=())
        assert result.returncode == 0, result.stderr
        path = source / 'compressed.zst'
        path.write_bytes(result.stdout)
        successful(archives.decompress_single_file(path, root / 'decoded-zstd', 'zstd'))
        assert (root / 'decoded-zstd').read_bytes() == payload.read_bytes()
        print('Streaming gzip/bzip2/xz/zstd: OK', flush=True)

        # One-file BBC DFS catalogue on a 40-track single-sided disk.
        disk = bytearray(40 * 10 * 256)
        disk[8:16] = b'HELLO  $'
        disk[0x105] = 8
        disk[0x106:0x108] = bytes((1, 144))  # 400 sectors
        disk[0x10c:0x10e] = struct.pack('<H', 5)
        disk[0x10f] = 2  # file starts at sector 2
        disk[512:517] = b'hello'
        ssd = source / 'sample.ssd'
        ssd.write_bytes(disk)
        successful(extraction.extract_acorn_disc_image_manager(ssd, root / 'dim'))
        assert any(p.read_bytes() == b'hello' for p in (root / 'dim').rglob('*') if p.is_file())
        hfe = root / 'sample.hfe'
        result = run_tool(['gw', 'convert', '--format', 'acorn.dfs.ss', str(ssd), str(hfe)],
                          write_dirs=(root,))
        assert result.returncode == 0, result.stderr
        successful(flux.flux_to_imd_hxcfe(hfe, root / 'sample.imd'))
        successful(flux.flux_visualisation_hxcfe(hfe, root / 'hxc.png'))
        successful(flux.flux_visualisation_fluxfox(hfe, root / 'fluxfox.png'))
        fcfs = source / 'sample.fcfs'
        fcfs.write_bytes(bytes(disk) + b'FCFS' + bytes(252))
        successful(extraction.convert_fcfs_to_raw(fcfs, root / 'sample.raw'))
        assert (root / 'sample.raw').read_bytes() == bytes(disk)
        print('DIM, Greaseweazle, HxCFE, Fluxfox, FCFS: OK', flush=True)

        # ImageMagick's temporary/cache paths and ffmpeg/ffprobe intermediates.
        image = source / 'image.ppm'
        image.write_bytes(b'P6\n16 16\n255\n' + bytes((255, 0, 0)) * 256)
        result = run_tool(['convert', str(image), str(root / 'image.png')], write_dirs=(root,))
        assert result.returncode == 0, result.stderr
        emf = source / 'sample.emf'
        header = struct.pack('<II4i4iIIIIHHIII4i',
                             1, 88, 0, 0, 16, 16, 0, 0, 423, 423,
                             0x464d4520, 0x10000, 108, 2, 1, 0, 0, 0, 0, 16, 16, 4, 4)
        emf.write_bytes(header + struct.pack('<IIIII', 14, 20, 0, 0, 20))
        successful(images_common.convert_image(emf, root, 'emf'))
        wav = source / 'sound.wav'
        with wave.open(str(wav), 'wb') as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(8000)
            audio.writeframes(bytes(16000))
        successful(media_transcode.probe_media(wav))
        successful(media_transcode.transcode_media_to_audio(wav, root / 'audio.m4a', work_dir=root))
        result = run_tool(['ffmpeg', '-y', '-loop', '1', '-i', str(image), '-t', '0.1',
                           '-pix_fmt', 'yuv420p', str(root / 'input.mp4')], write_dirs=(root,))
        assert result.returncode == 0, result.stderr
        successful(media_transcode.transcode_media_to_mp4(root / 'input.mp4', root / 'output.mp4',
                   work_dir=root, has_audio=False, poster_path=root / 'poster.jpg'))
        assert (root / 'poster.jpg').stat().st_size > 0
        print('ImageMagick, EMF, ffprobe and ffmpeg audio/video/poster: OK', flush=True)

        rtf = source / 'document.rtf'
        rtf.write_text(r'{\rtf1\ansi sandbox compatibility}')
        successful(documents.rtf_to_text(rtf))
        assert 'sandbox compatibility' in documents.rtf_to_text(rtf)['text']
        # ImageMagick's PDF writer can be disabled by its distribution policy;
        # use a tiny self-contained PDF rather than loosening that policy.
        pdf = source / 'document.pdf'
        objects = [b'<< /Type /Catalog /Pages 2 0 R >>', b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
                   b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Contents 4 0 R >>',
                   b'<< /Length 0 >>\nstream\n\nendstream']
        data = bytearray(b'%PDF-1.4\n')
        offsets = [0]
        for n, obj in enumerate(objects, 1):
            offsets.append(len(data))
            data.extend(f'{n} 0 obj\n'.encode() + obj + b'\nendobj\n')
        xref = len(data)
        data.extend(b'xref\n0 5\n0000000000 65535 f \n')
        for offset in offsets[1:]:
            data.extend(f'{offset:010d} 00000 n \n'.encode())
        data.extend(f'trailer\n<< /Size 5 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode())
        pdf.write_bytes(data)
        successful(documents.pdf_to_text(pdf))
        result = run_tool(['file', '-b', str(pdf)], write_dirs=())
        assert result.returncode == 0 and b'PDF' in result.stdout, result.stderr
        mbr = bytearray(512 * 16)
        mbr[446:462] = struct.pack('<B3sB3sII', 0, bytes(3), 6, bytes(3), 1, 15)
        mbr[510:512] = b'\x55\xaa'
        path = source / 'mbr.img'
        path.write_bytes(mbr)
        result = run_tool(['sfdisk', '--json', str(path)], write_dirs=())
        assert result.returncode == 0 and b'partitiontable' in result.stdout, result.stderr
        print('Document parsers, file and sfdisk: OK', flush=True)
    print('Worker sandbox compatibility checks passed', flush=True)


if __name__ == '__main__':
    main()
