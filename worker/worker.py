#!/usr/bin/env python3
"""
Arcology Analysis Worker

Runs inside a Docker container with analysis tools installed.
Polls the Arcology API for pending jobs and processes them.

Tools required in container:
- imgviz (Fluxfox) - flux visualisation
- hxcfe (HxC Floppy Emulator) - flux conversion and visualisation  
- gw (Greaseweazle) - sector image conversion
- xvfb-run + DiscImageManager - Acorn filesystem extraction
- 7z - DOS/ISO file extraction
- zstd, gzip, bzip2 - decompression
"""

import os
import sys
import json
import time
import shutil
import hashlib
import logging
import tempfile
import subprocess
from pathlib import Path
from typing import Optional
from enum import Enum

import requests

# =============================================================================
# Configuration
# =============================================================================

ARCOLOGY_API = os.environ.get('ARCOLOGY_API', 'http://host.docker.internal:5000/api')
UPLOAD_DIR = Path(os.environ.get('UPLOAD_DIR', '/data/uploads'))
OUTPUT_DIR = Path(os.environ.get('OUTPUT_DIR', '/data/outputs'))
POLL_INTERVAL = int(os.environ.get('POLL_INTERVAL', '30'))
LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO')

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format='%(asctime)s - %(levelname)s - %(message)s'
)
log = logging.getLogger(__name__)


# =============================================================================
# Artefact Types (must match database.py)
# =============================================================================

class ArtefactType(str, Enum):
    # Flux-level
    SCP = "scp"
    KF = "kf"
    IPF = "ipf"
    FLUX_RAW = "flux_raw"
    
    # Sector-level floppy
    IMD = "imd"
    TD0 = "td0"
    D64 = "d64"
    ADF = "adf"
    DSK = "dsk"
    IMG = "img"
    HFE = "hfe"
    
    # CD/DVD
    ISO = "iso"
    BIN_CUE = "bin_cue"
    
    # Hard drive
    HDD_RAW = "hdd_raw"
    
    # Documents/images
    PDF = "pdf"
    JPEG = "jpeg"
    PNG = "png"
    TIFF = "tiff"
    
    # Archives
    ZIP = "zip"
    TARGZ = "tar_gz"
    
    UNKNOWN = "unknown"


class AnalysisType(str, Enum):
    FLUX_VISUALISATION = "flux_visualisation"
    FLUX_DECODE = "flux_decode"
    SECTOR_DUMP = "sector_dump"
    FILE_LISTING = "file_listing"
    FILE_EXTRACTION = "file_extraction"
    METADATA_EXTRACT = "metadata_extract"
    PARTITION_DETECT = "partition_detect"
    CHECKSUM_COMPUTE = "checksum_compute"
    FORMAT_IDENTIFY = "format_identify"


# =============================================================================
# Compression Handling
# =============================================================================

COMPRESSION_EXTENSIONS = {
    '.zst': ['zstd', '-d', '-k', '-f'],
    '.gz': ['gzip', '-d', '-k', '-f'],
    '.bz2': ['bzip2', '-d', '-k', '-f'],
}


def decompress_if_needed(input_path: Path, work_dir: Path) -> Path:
    """
    If file is compressed, decompress to work_dir and return new path.
    Otherwise return original path.
    """
    suffix = input_path.suffix.lower()
    
    if suffix in COMPRESSION_EXTENSIONS:
        cmd = COMPRESSION_EXTENSIONS[suffix]
        decompressed_name = input_path.stem  # Remove compression extension
        decompressed_path = work_dir / decompressed_name
        
        # Copy compressed file to work dir first
        compressed_copy = work_dir / input_path.name
        shutil.copy(input_path, compressed_copy)
        
        # Decompress
        log.info(f"Decompressing {input_path.name} with {cmd[0]}")
        result = subprocess.run(
            cmd + [str(compressed_copy)],
            capture_output=True,
            cwd=work_dir
        )
        
        if result.returncode != 0:
            raise RuntimeError(f"Decompression failed: {result.stderr.decode()}")
        
        # Clean up compressed copy
        compressed_copy.unlink(missing_ok=True)
        
        return decompressed_path
    
    return input_path


# =============================================================================
# Tool Runners
# =============================================================================

def run_tool(cmd: list[str], timeout: int = 3600) -> subprocess.CompletedProcess:
    """Run a tool command with logging."""
    log.debug(f"Running: {' '.join(cmd)}")
    result = subprocess.run(
        cmd,
        capture_output=True,
        timeout=timeout
    )
    if result.returncode != 0:
        log.warning(f"Tool returned {result.returncode}: {result.stderr.decode()[:500]}")
    return result


def compute_file_hash(filepath: Path) -> tuple[str, str, int]:
    """Compute MD5, SHA256 and file size."""
    md5 = hashlib.md5()
    sha256 = hashlib.sha256()
    size = 0
    
    with open(filepath, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            md5.update(chunk)
            sha256.update(chunk)
            size += len(chunk)
    
    return md5.hexdigest(), sha256.hexdigest(), size


# =============================================================================
# Flux Visualisation
# =============================================================================

def flux_visualisation_fluxfox(input_path: Path, output_path: Path) -> dict:
    """
    Generate flux visualisation using Fluxfox imgviz.
    Produces a detailed flux graph PNG.
    """
    result = run_tool([
        'imgviz',
        '-i', str(input_path),
        f'-o={output_path}',
        '--angle=2.88',
        '--hole_ratio=0.66',
        '--index_hole',
        '--data',
        '--metadata',
        '--decode',
        '--resolution=2048',
        '--ss=4'
    ])
    
    if result.returncode == 0 and output_path.exists():
        return {
            'success': True,
            'tool': 'fluxfox/imgviz',
            'output_path': str(output_path),
            'summary': 'Flux visualisation generated with Fluxfox'
        }
    
    return {
        'success': False,
        'tool': 'fluxfox/imgviz',
        'error': result.stderr.decode()[:1000]
    }


def flux_visualisation_hxcfe(input_path: Path, output_path: Path) -> dict:
    """
    Generate flux visualisation using HxC Floppy Emulator.
    Alternative visualisation style.
    """
    result = run_tool([
        'hxcfe',
        f'-finput:{input_path}',
        '-conv:PNG_DISK_IMAGE',
        f'-foutput:{output_path}'
    ])
    
    if result.returncode == 0 and output_path.exists():
        return {
            'success': True,
            'tool': 'hxcfe',
            'output_path': str(output_path),
            'summary': 'Flux visualisation generated with HxCFE'
        }
    
    return {
        'success': False,
        'tool': 'hxcfe',
        'error': result.stderr.decode()[:1000]
    }


# =============================================================================
# Flux Decode / Conversion
# =============================================================================

def flux_to_imd_hxcfe(input_path: Path, output_path: Path) -> dict:
    """Convert flux image (SCP) to ImageDisk format using HxCFE."""
    result = run_tool([
        'hxcfe',
        f'-finput:{input_path}',
        '-conv:IMD_IMG',
        f'-foutput:{output_path}'
    ])
    
    if result.returncode == 0 and output_path.exists():
        return {
            'success': True,
            'tool': 'hxcfe',
            'output_path': str(output_path),
            'output_type': ArtefactType.IMD,
            'summary': 'Converted to ImageDisk format'
        }
    
    return {
        'success': False,
        'tool': 'hxcfe',
        'error': result.stderr.decode()[:1000]
    }


def flux_to_hfe_hxcfe(input_path: Path, output_path: Path) -> dict:
    """Convert flux image (SCP) to HFE format using HxCFE."""
    result = run_tool([
        'hxcfe',
        f'-finput:{input_path}',
        '-conv:HXC_HFEV3',
        f'-foutput:{output_path}'
    ])
    
    if result.returncode == 0 and output_path.exists():
        return {
            'success': True,
            'tool': 'hxcfe',
            'output_path': str(output_path),
            'output_type': ArtefactType.HFE,
            'summary': 'Converted to HFE format'
        }
    
    return {
        'success': False,
        'tool': 'hxcfe',
        'error': result.stderr.decode()[:1000]
    }


def sector_image_to_raw_greaseweazle(input_path: Path, output_path: Path) -> dict:
    """
    Convert sector image (IMD, HFE, SCP) to raw sector image using Greaseweazle.
    Greaseweazle is preferred as it fills in bad sectors.
    """
    result = run_tool([
        'gw', 'convert',
        '--format', 'ibm.scan',
        str(input_path),
        str(output_path)
    ])
    
    if result.returncode == 0 and output_path.exists():
        return {
            'success': True,
            'tool': 'greaseweazle',
            'output_path': str(output_path),
            'output_type': ArtefactType.IMG,
            'summary': 'Converted to raw sector image (bad sectors filled)'
        }
    
    return {
        'success': False,
        'tool': 'greaseweazle',
        'error': result.stderr.decode()[:1000]
    }


# =============================================================================
# File Extraction
# =============================================================================

def extract_acorn_disc_image_manager(input_path: Path, output_dir: Path) -> dict:
    """
    Extract files from Acorn DFS/ADFS disc image using Disc Image Manager.
    Creates INF files for metadata.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Create DIM script
    script_content = f"""insert {input_path}
report
chdir {output_dir}
config CreateINF true
extract * {output_dir}
exit
"""
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.dim', delete=False) as f:
        f.write(script_content)
        script_path = f.name
    
    try:
        result = run_tool([
            'xvfb-run',
            'DiscImageManager',
            '-c', script_path
        ])
        
        # Count extracted files
        extracted_files = list(output_dir.rglob('*'))
        file_count = sum(1 for f in extracted_files if f.is_file() and not f.suffix == '.inf')
        
        if file_count > 0:
            return {
                'success': True,
                'tool': 'DiscImageManager',
                'output_dir': str(output_dir),
                'file_count': file_count,
                'summary': f'Extracted {file_count} files from Acorn disc image'
            }
        
        return {
            'success': False,
            'tool': 'DiscImageManager',
            'error': 'No files extracted - may not be Acorn format'
        }
    
    finally:
        os.unlink(script_path)


def extract_dos_7z(input_path: Path, output_dir: Path) -> dict:
    """
    Extract files from DOS/FAT disc image using 7z.
    Works for FAT12/16/32 filesystems.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    
    result = run_tool([
        '7z', 'x',
        f'-o{output_dir}',
        '-y',  # Yes to all
        str(input_path)
    ])
    
    # Count extracted files
    extracted_files = list(output_dir.rglob('*'))
    file_count = sum(1 for f in extracted_files if f.is_file())
    
    if file_count > 0:
        return {
            'success': True,
            'tool': '7z',
            'output_dir': str(output_dir),
            'file_count': file_count,
            'summary': f'Extracted {file_count} files from DOS image'
        }
    
    return {
        'success': False,
        'tool': '7z',
        'error': result.stderr.decode()[:1000] if result.returncode != 0 else 'No files extracted'
    }


def extract_iso_7z(input_path: Path, output_dir: Path) -> dict:
    """Extract files from ISO image using 7z."""
    return extract_dos_7z(input_path, output_dir)  # Same process


def list_files_7z(input_path: Path) -> dict:
    """
    List files in an image using 7z without extracting.
    Returns structured file listing.
    """
    result = run_tool([
        '7z', 'l',
        '-slt',  # Technical listing format
        str(input_path)
    ])
    
    if result.returncode != 0:
        return {
            'success': False,
            'tool': '7z',
            'error': result.stderr.decode()[:1000]
        }
    
    # Parse 7z output
    files = []
    current_file = {}
    
    for line in result.stdout.decode().split('\n'):
        line = line.strip()
        if line.startswith('Path = '):
            if current_file and 'path' in current_file:
                files.append(current_file)
            current_file = {'path': line[7:]}
        elif line.startswith('Size = '):
            try:
                current_file['size'] = int(line[7:])
            except ValueError:
                pass
        elif line.startswith('Modified = '):
            current_file['modified'] = line[11:]
        elif line.startswith('CRC = '):
            current_file['crc32'] = line[6:].lower()
    
    if current_file and 'path' in current_file:
        files.append(current_file)
    
    # Filter out directory entries (size 0 or no size)
    files = [f for f in files if f.get('size', 0) > 0]
    
    return {
        'success': True,
        'tool': '7z',
        'files': files,
        'file_count': len(files),
        'summary': f'Found {len(files)} files'
    }


# =============================================================================
# Analysis Pipeline
# =============================================================================

class AnalysisWorker:
    """Main worker class that processes analysis jobs."""
    
    def __init__(self, api_url: str, upload_dir: Path, output_dir: Path):
        self.api = api_url.rstrip('/')
        self.uploads = upload_dir
        self.outputs = output_dir
        self.outputs.mkdir(parents=True, exist_ok=True)
    
    def api_get(self, endpoint: str) -> Optional[dict]:
        """GET request to API."""
        try:
            resp = requests.get(f"{self.api}{endpoint}", timeout=30)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            log.error(f"API GET {endpoint} failed: {e}")
            return None
    
    def api_put(self, endpoint: str, data: dict) -> Optional[dict]:
        """PUT request to API."""
        try:
            resp = requests.put(
                f"{self.api}{endpoint}",
                json=data,
                timeout=30
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            log.error(f"API PUT {endpoint} failed: {e}")
            return None
    
    def api_post(self, endpoint: str, data: dict) -> Optional[dict]:
        """POST request to API."""
        try:
            resp = requests.post(
                f"{self.api}{endpoint}",
                json=data,
                timeout=30
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            log.error(f"API POST {endpoint} failed: {e}")
            return None
    
    def update_analysis(self, analysis_id: int, **kwargs):
        """Update analysis record in API."""
        self.api_put(f"/analysis/{analysis_id}", kwargs)
    
    def register_derived_artefact(
        self,
        analysis_id: int,
        label: str,
        source_path: Path,
        artefact_type: ArtefactType
    ) -> Optional[dict]:
        """
        Register a derived artefact produced by an analysis.
        Copies file to outputs directory (not uploads) and calls API.
        """
        import uuid
        storage_name = f"{uuid.uuid4().hex}{source_path.suffix}"
        storage_path = self.outputs / storage_name

        # Copy file to outputs directory (derived artefacts go here)
        shutil.copy(source_path, storage_path)

        # Compute hashes
        md5, sha256, file_size = compute_file_hash(storage_path)

        # Register via API with storage_directory='outputs'
        return self.api_post(f"/analysis/{analysis_id}/produce-artefact", {
            'label': label,
            'original_filename': source_path.name,
            'storage_path': storage_name,
            'storage_directory': 'outputs',
            'artefact_type': artefact_type.value,
            'file_size': file_size,
            'md5': md5,
            'sha256': sha256
        })
    
    def register_file_listing(self, artefact_id: int, files: list[dict], filesystem: str = 'unknown'):
        """Register extracted file listing in API."""
        # First create partition
        partition_resp = self.api_post(f"/artefacts/{artefact_id}/partitions", {
            'partition_index': 0,
            'filesystem': filesystem,
            'total_files': len(files)
        })
        
        if not partition_resp:
            log.error("Failed to create partition")
            return
        
        partition_id = partition_resp.get('id')
        
        # Add files in batches
        batch_size = 100
        for i in range(0, len(files), batch_size):
            batch = files[i:i+batch_size]
            file_records = []
            for f in batch:
                path = f.get('path', '')
                file_records.append({
                    'path': path,
                    'filename': Path(path).name,
                    'extension': Path(path).suffix.lstrip('.').lower() or None,
                    'file_size': f.get('size'),
                    'crc32': f.get('crc32'),
                    'md5': f.get('md5'),
                    'sha1': f.get('sha1')
                })
            
            self.api_post(f"/partitions/{partition_id}/files", {'files': file_records})
    
    def get_input_path(self, artefact: dict, work_dir: Path) -> Path:
        """Get input file path, decompressing if needed."""
        storage_path = artefact['storage_path']
        input_path = self.uploads / storage_path
        
        if not input_path.exists():
            raise FileNotFoundError(f"Input file not found: {input_path}")
        
        return decompress_if_needed(input_path, work_dir)
    
    def save_output_file(self, source_path: Path, filename: str) -> str:
        """
        Save an output file (like a visualisation) to the outputs directory.
        Returns the relative path that can be used in URLs.
        """
        dest_path = self.outputs / filename
        shutil.copy(source_path, dest_path)
        return filename
    
    def process_flux_visualisation(self, analysis: dict, artefact: dict, work_dir: Path):
        """Process FLUX_VISUALISATION analysis."""
        import uuid
        analysis_id = analysis['id']
        artefact_id = artefact['id']
        input_path = self.get_input_path(artefact, work_dir)

        outputs = []

        # Generate unique filename prefix using UUID to prevent any collisions
        unique_id = uuid.uuid4().hex

        # Try Fluxfox first (more detailed)
        output_fluxfox = work_dir / f"{unique_id}_fluxfox.png"
        result_fluxfox = flux_visualisation_fluxfox(input_path, output_fluxfox)

        if result_fluxfox['success']:
            saved_name = self.save_output_file(output_fluxfox, f"{unique_id}_fluxfox.png")
            outputs.append({
                'tool': 'fluxfox',
                'type': 'image',
                'filename': saved_name,
                'description': 'Fluxfox visualisation',
                'artefact_id': artefact_id
            })

        # Also generate HxCFE visualisation (different style)
        output_hxcfe = work_dir / f"{unique_id}_hxcfe.png"
        result_hxcfe = flux_visualisation_hxcfe(input_path, output_hxcfe)

        if result_hxcfe['success']:
            saved_name = self.save_output_file(output_hxcfe, f"{unique_id}_hxcfe.png")
            outputs.append({
                'tool': 'hxcfe',
                'type': 'image',
                'filename': saved_name,
                'description': 'HxCFE visualisation',
                'artefact_id': artefact_id
            })
        
        if outputs:
            self.update_analysis(
                analysis_id,
                status='completed',
                success=True,
                tool_name='fluxfox,hxcfe',
                summary=f'Generated {len(outputs)} flux visualisation(s)',
                details=json.dumps({
                    'outputs': outputs,
                    'fluxfox': result_fluxfox,
                    'hxcfe': result_hxcfe
                })
            )
        else:
            self.update_analysis(
                analysis_id,
                status='failed',
                success=False,
                error_message=f"Fluxfox: {result_fluxfox.get('error', 'unknown')}; HxCFE: {result_hxcfe.get('error', 'unknown')}"
            )
    
    def process_flux_decode(self, analysis: dict, artefact: dict, work_dir: Path):
        """
        Process FLUX_DECODE analysis.
        Attempts to decode flux to sector image, producing derived artefacts.
        """
        analysis_id = analysis['id']
        input_path = self.get_input_path(artefact, work_dir)
        artefact_label = artefact['label']
        
        results = []
        
        # 1. Convert to IMD (preserves track metadata)
        imd_path = work_dir / f"{input_path.stem}.imd"
        imd_result = flux_to_imd_hxcfe(input_path, imd_path)
        results.append(('IMD', imd_result))
        
        if imd_result['success']:
            derived = self.register_derived_artefact(
                analysis_id,
                f"{artefact_label} (IMD)",
                imd_path,
                ArtefactType.IMD
            )
            log.info(f"Created derived IMD artefact: {derived}")
        
        # 2. Convert to HFE (for emulators)
        hfe_path = work_dir / f"{input_path.stem}.hfe"
        hfe_result = flux_to_hfe_hxcfe(input_path, hfe_path)
        results.append(('HFE', hfe_result))
        
        if hfe_result['success']:
            derived = self.register_derived_artefact(
                analysis_id,
                f"{artefact_label} (HFE)",
                hfe_path,
                ArtefactType.HFE
            )
            log.info(f"Created derived HFE artefact: {derived}")
        
        # 3. Convert to raw IMG via Greaseweazle (best for file extraction)
        # Use the IMD as input if available, otherwise try direct
        if imd_result['success']:
            img_input = imd_path
        else:
            img_input = input_path
        
        img_path = work_dir / f"{input_path.stem}.img"
        img_result = sector_image_to_raw_greaseweazle(img_input, img_path)
        results.append(('IMG', img_result))
        
        if img_result['success']:
            derived = self.register_derived_artefact(
                analysis_id,
                f"{artefact_label} (raw sectors)",
                img_path,
                ArtefactType.IMG
            )
            log.info(f"Created derived IMG artefact: {derived}")
        
        # Report results
        any_success = any(r[1]['success'] for r in results)
        summary_parts = [f"{name}: {'OK' if r['success'] else 'FAIL'}" for name, r in results]
        
        self.update_analysis(
            analysis_id,
            status='completed' if any_success else 'failed',
            success=any_success,
            tool_name='hxcfe,greaseweazle',
            summary='; '.join(summary_parts),
            details=json.dumps({name: r for name, r in results})
        )
    
    def process_file_listing(self, analysis: dict, artefact: dict, work_dir: Path):
        """
        Process FILE_LISTING analysis.
        Lists files in sector image without extracting.
        """
        analysis_id = analysis['id']
        artefact_id = artefact['id']
        input_path = self.get_input_path(artefact, work_dir)
        hints = json.loads(analysis.get('hints') or '{}')
        
        # Try 7z first (works for most formats)
        result = list_files_7z(input_path)
        
        if result['success'] and result['files']:
            # Determine filesystem type from hints or detection
            # TODO: Add format identification
            filesystem = hints.get('filesystem', 'unknown')
            
            self.register_file_listing(artefact_id, result['files'], filesystem)
            
            self.update_analysis(
                analysis_id,
                status='completed',
                success=True,
                tool_name=result['tool'],
                summary=result['summary'],
                details=json.dumps({'file_count': result['file_count']})
            )
        else:
            self.update_analysis(
                analysis_id,
                status='failed',
                success=False,
                error_message=result.get('error', 'Could not list files')
            )
    
    def process_file_extraction(self, analysis: dict, artefact: dict, work_dir: Path):
        """
        Process FILE_EXTRACTION analysis.
        Extracts files based on detected/hinted filesystem type.
        """
        analysis_id = analysis['id']
        input_path = self.get_input_path(artefact, work_dir)
        hints = json.loads(analysis.get('hints') or '{}')
        
        filesystem = hints.get('filesystem', '').lower()
        extract_dir = work_dir / 'extracted'
        
        # Choose extraction method based on filesystem
        if filesystem in ('dfs', 'adfs', 'acorn'):
            result = extract_acorn_disc_image_manager(input_path, extract_dir)
        elif filesystem in ('fat', 'fat12', 'fat16', 'fat32', 'dos', 'msdos'):
            result = extract_dos_7z(input_path, extract_dir)
        else:
            # Try 7z as default (handles many formats)
            result = extract_dos_7z(input_path, extract_dir)
            
            # If that fails and no filesystem hint, try Acorn
            if not result['success'] and not filesystem:
                result = extract_acorn_disc_image_manager(input_path, extract_dir)
        
        if result['success']:
            # TODO: Copy extracted files to permanent storage
            # TODO: Register extracted files in database with hashes
            
            self.update_analysis(
                analysis_id,
                status='completed',
                success=True,
                tool_name=result['tool'],
                summary=result['summary'],
                output_path=str(extract_dir),
                details=json.dumps({'file_count': result.get('file_count', 0)})
            )
        else:
            self.update_analysis(
                analysis_id,
                status='failed',
                success=False,
                error_message=result.get('error', 'Extraction failed')
            )
    
    def process_metadata_extract(self, analysis: dict, artefact: dict, work_dir: Path):
        """
        Process METADATA_EXTRACT analysis.
        Extracts format-specific metadata.
        """
        analysis_id = analysis['id']
        input_path = self.get_input_path(artefact, work_dir)
        artefact_type = artefact['artefact_type']
        
        metadata = {}
        
        # Get basic file info
        md5, sha256, size = compute_file_hash(input_path)
        metadata['file'] = {
            'size': size,
            'md5': md5,
            'sha256': sha256
        }
        
        # Format-specific metadata extraction
        # TODO: Add more format-specific extraction
        
        self.update_analysis(
            analysis_id,
            status='completed',
            success=True,
            summary=f'Extracted metadata for {artefact_type}',
            details=json.dumps(metadata)
        )
    
    def process_format_identify(self, analysis: dict, artefact: dict, work_dir: Path):
        """
        Process FORMAT_IDENTIFY analysis.
        Attempts to identify the exact format of an image.
        
        TODO: Implement proper format identification:
        - Read boot sector / filesystem signatures
        - Check for known patterns (Acorn ADFS, DFS, DOS FAT, etc.)
        - For hard drive images: partition table detection (MBR, GPT)
        - Carve partitions from HDD images
        """
        analysis_id = analysis['id']
        input_path = self.get_input_path(artefact, work_dir)
        
        # Placeholder - format identification not yet implemented
        self.update_analysis(
            analysis_id,
            status='completed',
            success=True,
            summary='Format identification not yet implemented',
            details=json.dumps({'detected': 'unknown'})
        )
    
    def process_analysis(self, analysis: dict):
        """Process a single analysis job."""
        analysis_id = analysis['id']
        analysis_type = analysis['analysis_type']
        artefact = analysis.get('artefact', {})
        
        log.info(f"Processing analysis {analysis_id}: {analysis_type} for {artefact.get('label', 'unknown')}")
        
        # Mark as running
        self.update_analysis(analysis_id, status='running')
        
        # Create temporary work directory
        with tempfile.TemporaryDirectory(prefix=f'arcology_{analysis_id}_') as work_dir:
            work_path = Path(work_dir)
            
            try:
                # Dispatch to appropriate handler
                if analysis_type == AnalysisType.FLUX_VISUALISATION.value:
                    self.process_flux_visualisation(analysis, artefact, work_path)
                
                elif analysis_type == AnalysisType.FLUX_DECODE.value:
                    self.process_flux_decode(analysis, artefact, work_path)
                
                elif analysis_type == AnalysisType.FILE_LISTING.value:
                    self.process_file_listing(analysis, artefact, work_path)
                
                elif analysis_type == AnalysisType.FILE_EXTRACTION.value:
                    self.process_file_extraction(analysis, artefact, work_path)
                
                elif analysis_type == AnalysisType.METADATA_EXTRACT.value:
                    self.process_metadata_extract(analysis, artefact, work_path)
                
                elif analysis_type == AnalysisType.FORMAT_IDENTIFY.value:
                    self.process_format_identify(analysis, artefact, work_path)
                
                else:
                    log.warning(f"Unknown analysis type: {analysis_type}")
                    self.update_analysis(
                        analysis_id,
                        status='failed',
                        error_message=f'Unknown analysis type: {analysis_type}'
                    )
            
            except Exception as e:
                log.exception(f"Analysis {analysis_id} failed with exception")
                self.update_analysis(
                    analysis_id,
                    status='failed',
                    error_message=str(e)[:1000]
                )
    
    def claim_and_process(self):
        """
        Atomically claim a pending analysis and process it.
        
        This is safe for multiple workers - each worker claims one job at a time
        by setting status to 'running' before processing. The API ensures only
        one worker can claim each job.
        """
        # Get list of pending analyses
        response = self.api_get('/analysis/pending')
        
        if not response:
            return 0
        
        analyses = response.get('analyses', [])
        
        if not analyses:
            return 0
        
        # Try to claim the first available job
        for analysis in analyses:
            analysis_id = analysis['id']
            
            # Attempt to claim by setting status to 'running'
            # The API should reject this if already claimed by another worker
            claim_result = self.api_put(f"/analysis/{analysis_id}", {
                'status': 'running',
                'claim_worker': True  # Signal this is a claim attempt
            })
            
            if claim_result and claim_result.get('status') == 'running':
                # Successfully claimed - process it
                log.info(f"Claimed analysis {analysis_id}")
                self.process_analysis(analysis)
                return 1
            else:
                # Already claimed by another worker, try next
                log.debug(f"Analysis {analysis_id} already claimed, trying next")
                continue
        
        return 0
    
    def poll_and_process(self):
        """Poll for pending analyses and process them."""
        return self.claim_and_process()
    
    def run(self):
        """Main worker loop."""
        log.info(f"Starting Arcology worker")
        log.info(f"API: {self.api}")
        log.info(f"Uploads: {self.uploads}")
        log.info(f"Outputs: {self.outputs}")
        
        while True:
            try:
                processed = self.poll_and_process()
                
                if processed == 0:
                    log.debug(f"No pending analyses, sleeping {POLL_INTERVAL}s")
                    time.sleep(POLL_INTERVAL)
                else:
                    log.info(f"Processed {processed} analyses")
                    # Small delay between batches
                    time.sleep(1)
            
            except KeyboardInterrupt:
                log.info("Shutting down")
                break
            
            except Exception as e:
                log.exception("Unexpected error in main loop")
                time.sleep(POLL_INTERVAL)


# =============================================================================
# Main
# =============================================================================

def main():
    worker = AnalysisWorker(
        api_url=ARCOLOGY_API,
        upload_dir=UPLOAD_DIR,
        output_dir=OUTPUT_DIR
    )
    worker.run()


if __name__ == '__main__':
    main()
