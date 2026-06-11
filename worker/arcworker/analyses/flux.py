"""
Flux-image analysis handlers.

Covers the flux-stream pipeline: visualisation, density-mismatch detection,
flux→IMD/HFE/RAW_SECTOR decode, and HFE mastering / protection scans.
"""

import json
from pathlib import Path
from shared.enums import AnalysisType, ArtefactType
from ..config import MASTERING_TRACK_SCAN_COUNT, log
from ..tools import (
    a2r_to_scp_gw,
    compute_file_hash,
    dfi_to_scp_hxcfe,
    flux_to_hfe_hxcfe,
    flux_to_imd_hxcfe,
    flux_visualisation_fluxfox,
    flux_visualisation_hxcfe,
    sector_image_to_raw_greaseweazle,
)
from ..tools.flux import (
    _geometry_to_gw_format,
    scp_fix_track_density,
    sector_image_to_raw_greaseweazle_one_side,
)
from ..tools.imd import (
    detect_geometry_from_boot_data,
    detect_independent_sides,
    detect_track_density_mismatch,
    parse_imd_track0,
    parse_imd_tracks,
)
from ._common import analysis_handler

# Flux formats that cannot be visualised or decoded directly — they must be
# converted to SCP first and their SCP sibling's own FLUX_DECODE handles the
# rest of the pipeline.  Add new "SCP-via-conversion" types here; update
# process_flux_visualisation() and process_flux_decode() with an elif branch
# that calls the appropriate conversion tool.
_SCP_VIA_CONVERSION_TYPES = frozenset({
    ArtefactType.DFI,   # hxcfe: dfi_to_scp_hxcfe()
    ArtefactType.A2R,   # greaseweazle: a2r_to_scp_gw()
})


@analysis_handler("flux visualisation")
def process_flux_visualisation(self, analysis: dict, artefact: dict, work_dir: Path):
    """Process FLUX_VISUALISATION analysis."""
    analysis_id = analysis['id']
    analysis_uuid = analysis['uuid']

    input_path = self.get_input_path(artefact, work_dir)

    # Build output subdirectory: {item_uuid}_{item_slug}/{artefact_uuid}_{artefact_slug}
    item = artefact.get('item', {})
    item_uuid = item.get('uuid', '')
    item_slug = item.get('slug', '')
    artefact_uuid = artefact.get('uuid', '')
    artefact_slug = artefact.get('slug', '')
    item_part = f"{item_uuid}_{item_slug}" if item_slug else item_uuid
    artefact_part = f"{artefact_uuid}_{artefact_slug}" if artefact_slug else artefact_uuid
    output_subdir = f"{item_part}/{artefact_part}" if (item_part and artefact_part) else None

    outputs = []
    source_type = ArtefactType(artefact['artefact_type'])

    # Both fluxfox and hxcfe run against the SCP stream.
    # For formats that cannot be visualised directly (DFI, A2R), convert to
    # SCP first so both tools operate on the same source.  For SCP sources,
    # use the file directly.
    vis_input_path = input_path
    to_scp_result = None
    if source_type in _SCP_VIA_CONVERSION_TYPES:
        hints = json.loads(analysis.get('hints') or '{}')
        scp_path = work_dir / f"{input_path.stem}_vis.scp"
        if source_type == ArtefactType.DFI:
            clock_mhz = hints.get('dfi_clock_mhz')
            to_scp_result = dfi_to_scp_hxcfe(input_path, scp_path, clock_mhz=clock_mhz)
        elif source_type == ArtefactType.A2R:
            to_scp_result = a2r_to_scp_gw(input_path, scp_path)
        if not to_scp_result['success']:
            self.fail_analysis(analysis_id, f"→SCP conversion failed: {to_scp_result.get('error', '')}")
            return
        vis_input_path = scp_path

    # Run Fluxfox (more detailed visualisation)
    output_fluxfox = work_dir / f"{analysis_uuid}_fluxfox.png"
    result_fluxfox = flux_visualisation_fluxfox(vis_input_path, output_fluxfox)

    if result_fluxfox['success']:
        saved_name = self.save_output_file(output_fluxfox, f"{analysis_uuid}_fluxfox.png", subdir=output_subdir)
        outputs.append({
            'tool': 'fluxfox',
            'type': 'image',
            'filename': saved_name,
            'description': 'Fluxfox visualisation'
        })

    # Also run HxCFE (different visualisation style)
    output_hxcfe = work_dir / f"{analysis_uuid}_hxcfe.png"
    result_hxcfe = flux_visualisation_hxcfe(vis_input_path, output_hxcfe)

    if result_hxcfe['success']:
        saved_name = self.save_output_file(output_hxcfe, f"{analysis_uuid}_hxcfe.png", subdir=output_subdir)
        outputs.append({
            'tool': 'hxcfe',
            'type': 'image',
            'filename': saved_name,
            'description': 'HxCFE visualisation'
        })

    if outputs:
        details = {'outputs': outputs, 'fluxfox': result_fluxfox, 'hxcfe': result_hxcfe}
        if to_scp_result is not None:
            details['to_scp'] = to_scp_result
        self.complete_analysis(
            analysis_id,
            tool_name='fluxfox,hxcfe',
            summary=f'Generated {len(outputs)} flux visualisation(s)',
            details=json.dumps(details)
        )
    else:
        self.fail_analysis(
            analysis_id,
            f"Fluxfox: {result_fluxfox.get('error', 'unknown')}; HxCFE: {result_hxcfe.get('error', 'unknown')}"
        )


@analysis_handler("track density detection")
def process_detect_track_density(self, analysis: dict, artefact: dict, work_dir: Path):
    """
    Detect 40-track disc captured in 80-track drive and produce corrected SCP.

    Pipeline:
      1. Convert SCP → IMD via hxcfe (temp, for track metadata only)
      2. Parse all IMD tracks with parse_imd_tracks()
      3. Run detect_track_density_mismatch() on the track list
      4. If detected: use gw convert to strip odd tracks → derived 40-track SCP
         and queue FLUX_VISUALISATION + FLUX_DECODE on the corrected SCP only.
      5. If not detected: queue FLUX_VISUALISATION + FLUX_DECODE on the original SCP.

    FLUX_VISUALISATION and FLUX_DECODE are not queued at upload time for SCP
    artefacts (see ANALYSIS_MAP).  This handler gates them so only the correct
    image (original or density-corrected) enters the HFE/IMD/RAW_SECTOR pipeline,
    preventing duplicate derived artefacts from both the 80-track and 40-track images.
    """
    analysis_id    = analysis['id']
    input_path     = self.get_input_path(artefact, work_dir)
    artefact_label = artefact['label']
    artefact_uuid  = artefact.get('uuid')

    # Step 1: SCP → IMD (temporary; used only for track metadata)
    imd_path   = work_dir / f"{input_path.stem}_tddetect.imd"
    imd_result = flux_to_imd_hxcfe(input_path, imd_path)
    if not imd_result['success']:
        self.fail_analysis(
            analysis_id,
            f"hxcfe conversion failed: {imd_result.get('error', '')}",
        )
        return

    # Step 2: parse all tracks
    tracks = parse_imd_tracks(imd_path)
    if not tracks:
        self.fail_analysis(analysis_id, "IMD parse failed")
        return

    # Step 3: detect mismatch
    detection = detect_track_density_mismatch(tracks)

    if not detection['detected']:
        self.complete_analysis(
            analysis_id,
            tool_name='hxcfe',
            summary='No track density mismatch detected',
            details=json.dumps({'detection': detection}),
        )
        # No density mismatch: queue downstream analyses on the original SCP.
        if artefact_uuid:
            self.api.queue_analysis(artefact_uuid, AnalysisType.FLUX_VISUALISATION.value)
            self.api.queue_analysis(artefact_uuid, AnalysisType.FLUX_DECODE.value)
        return

    # Step 4: extract even tracks → density-corrected SCP
    fix_heads = detection['data_heads'] if detection['data_heads'] else None
    fixed_path = work_dir / f"{input_path.stem}_40track.scp"
    fix_result = scp_fix_track_density(input_path, fixed_path, heads=fix_heads)

    if fix_result['success']:
        derived = self.api.register_derived_artefact(
            analysis_id,
            f"{artefact_label} (40-track, density corrected)",
            fixed_path,
            ArtefactType.SCP,
            skip_analyses=[AnalysisType.DETECT_TRACK_DENSITY.name],
        )
        log.info(f"Created density-corrected SCP: {derived}")
        # Density mismatch detected: queue downstream analyses on the corrected
        # SCP only — not on the original 80-track image — to prevent duplicate
        # HFE/IMD/RAW_SECTOR artefacts from both images.
        if derived and 'artefact' in derived:
            corrected_uuid = derived['artefact']['uuid']
            self.api.queue_analysis(corrected_uuid, AnalysisType.FLUX_VISUALISATION.value)
            self.api.queue_analysis(corrected_uuid, AnalysisType.FLUX_DECODE.value)

    conf_pct   = f"{detection['confidence']:.0%}"
    odd_duplicate = detection['odd_tracks_with_duplicate_data']
    odd_varied = detection['odd_tracks_with_varied_data']
    blank_heads = detection['blank_heads']
    duplicate_suffix = (
        f"; NOTE: {odd_duplicate} odd track(s) also decode as track/2, consistent with a head-alignment or wide-head duplicate read"
        if odd_duplicate else ''
    )
    odd_suffix = (
        f"; WARNING: {odd_varied} odd track(s) contain non-uniform data "
        f"from a prior 80-track format — disc was reformatted, not re-imaged"
        if odd_varied else ''
    )
    side_suffix = (
        f"; blank side(s) {', '.join(str(h) for h in blank_heads)} omitted from corrected copy"
        if blank_heads and len(detection['data_heads']) == 1 else ''
    )
    self.complete_analysis(
        analysis_id,
        tool_name='hxcfe,greaseweazle',
        summary=(
            f"Track density mismatch detected (confidence {conf_pct}); "
            + ('derived SCP registered' if fix_result['success'] else 'correction failed')
            + duplicate_suffix
            + odd_suffix
            + side_suffix
        ),
        details=json.dumps({'detection': detection, 'fix': fix_result}),
    )


@analysis_handler("flux decode")
def process_flux_decode(self, analysis: dict, artefact: dict, work_dir: Path):
    """
    Process FLUX_DECODE analysis.
    Attempts to decode flux to sector image, producing derived artefacts.

    Pipeline depends on source type:
      SCP  → register HFE sibling (skip_analyses=[FLUX_DECODE, FLUX_VISUALISATION])
             + IMD sibling (skip_analyses=[FLUX_DECODE]),
             then gw(SCP, detected_format) → RAW_SECTOR
      HFE  → register IMD sibling (skip_analyses=[FLUX_DECODE]),
             then gw(HFE, detected_format) → RAW_SECTOR
      IMD  → no siblings; gw(IMD, detected_format) → RAW_SECTOR
      DFI  → register SCP sibling (no skip_analyses); the SCP's own
             FLUX_DECODE runs the HFE/IMD/RAW_SECTOR pipeline.
      A2R  → same as DFI but uses greaseweazle (gw convert) instead of hxcfe.
    """
    analysis_id = analysis['id']
    results = []

    input_path = self.get_input_path(artefact, work_dir)
    artefact_label = artefact['label']
    source_type = ArtefactType(artefact['artefact_type'])

    # ── Step 1: produce format-conversion siblings ──────────────────────

    imd_path = work_dir / f"{input_path.stem}.imd"

    hints = json.loads(analysis.get('hints') or '{}')

    if source_type == ArtefactType.SCP:
        # IMD sibling
        imd_result = flux_to_imd_hxcfe(input_path, imd_path)
        results.append(('IMD', imd_result))
        if imd_result['success']:
            derived = self.api.register_derived_artefact(
                analysis_id,
                f"{artefact_label} (IMD)",
                imd_path,
                ArtefactType.IMD,
                skip_analyses=[AnalysisType.FLUX_DECODE.name],
            )
            log.info(f"Created derived IMD artefact: {derived}")

        # HFE sibling
        hfe_path = work_dir / f"{input_path.stem}.hfe"
        hfe_result = flux_to_hfe_hxcfe(input_path, hfe_path)
        results.append(('HFE', hfe_result))
        if hfe_result['success']:
            # Skip FLUX_VISUALISATION on the intermediate HFE: it is a
            # lossy re-encoding of the SCP flux, so its plots would be worse
            # than the ones already produced from the source SCP. Flux plots
            # are only generated for HFEs uploaded directly by the user (via
            # the HFE ANALYSIS_MAP entry at upload time).
            derived = self.api.register_derived_artefact(
                analysis_id,
                f"{artefact_label} (HFE)",
                hfe_path,
                ArtefactType.HFE,
                skip_analyses=[
                    AnalysisType.FLUX_DECODE.name,
                    AnalysisType.FLUX_VISUALISATION.name,
                ],
            )
            log.info(f"Created derived HFE artefact: {derived}")

    elif source_type == ArtefactType.HFE:
        # IMD sibling only (source is already HFE)
        imd_result = flux_to_imd_hxcfe(input_path, imd_path)
        results.append(('IMD', imd_result))
        if imd_result['success']:
            derived = self.api.register_derived_artefact(
                analysis_id,
                f"{artefact_label} (IMD)",
                imd_path,
                ArtefactType.IMD,
                skip_analyses=[AnalysisType.FLUX_DECODE.name],
            )
            log.info(f"Created derived IMD artefact: {derived}")

    elif source_type == ArtefactType.DFI:
        # DFI → SCP conversion; SCP sibling's own FLUX_DECODE handles the rest.
        # The clock frequency may be overridden via the dfi_clock_mhz hint, which
        # is passed through an hxcfe script (the only way to set DFILOADER_SAMPLE_FREQUENCY_MHZ).
        clock_mhz = hints.get('dfi_clock_mhz')
        scp_path = work_dir / f"{input_path.stem}.scp"
        scp_result = dfi_to_scp_hxcfe(input_path, scp_path, clock_mhz=clock_mhz)
        results.append(('SCP', scp_result))
        if scp_result['success']:
            derived = self.api.register_derived_artefact(
                analysis_id,
                f"{artefact_label} (SCP)",
                scp_path,
                ArtefactType.SCP,
            )
            log.info(f"Created derived SCP artefact: {derived}")

    elif source_type == ArtefactType.A2R:
        # A2R → SCP conversion; SCP sibling's own FLUX_DECODE handles the rest.
        # Greaseweazle handles A2R natively and auto-detects the clock frequency.
        scp_path = work_dir / f"{input_path.stem}.scp"
        scp_result = a2r_to_scp_gw(input_path, scp_path)
        results.append(('SCP', scp_result))
        if scp_result['success']:
            derived = self.api.register_derived_artefact(
                analysis_id,
                f"{artefact_label} (SCP)",
                scp_path,
                ArtefactType.SCP,
            )
            log.info(f"Created derived SCP artefact: {derived}")

    else:
        # IMD source — no conversion siblings; source is already sector-decoded
        imd_result = {'success': True}

    # ── Step 2 & 3: format detection and gw conversion ───────────────────
    # Skipped for DFI: the SCP sibling's own FLUX_DECODE will handle these.
    # For SCP/HFE: read the IMD sibling we just produced.
    # For IMD: read the source directly.

    independent_sides = None

    if source_type not in _SCP_VIA_CONVERSION_TYPES:
        gw_format = hints.get('gw_format')
        gw_format_source = 'hint' if gw_format else None

        imd_track0_summary = None
        detected_geometry  = None

        imd_for_detection = imd_path if source_type != ArtefactType.IMD else input_path

        if not gw_format and imd_result['success']:
            track0 = parse_imd_track0(imd_for_detection)
            if track0:
                imd_track0_summary = {
                    'encoding':    track0['encoding'],
                    'sector_size': track0['sector_size'],
                    'cylinders':   track0['cylinders'],
                    'heads':       track0['heads'],
                    'sector_ids':  sorted(track0['sectors'].keys()),
                }
                geometry = detect_geometry_from_boot_data(track0)
                if geometry:
                    detected_geometry = {k: v for k, v in geometry.items()}
                    gw_format = _geometry_to_gw_format(**geometry)
                    if gw_format:
                        gw_format_source = 'detected'
                        log.info(f"Detected disc format: {geometry['filesystem']} "
                                 f"(probe {geometry.get('probe', '?')}) "
                                 f"→ gw format: {gw_format}")
                    else:
                        log.info(f"Detected disc geometry {geometry} — "
                                 f"no gw format match, using ibm.scan")

        if not gw_format:
            gw_format = 'ibm.scan'
            gw_format_source = 'fallback'

        # ── Step 2b: detect independent-sides capture ──────────────────
        # Some double-sided drives have head-select wired to a drive-select
        # pin on the controller — to the controller and filesystem the two
        # sides look like independent single-sided drives (e.g. BBC Micro
        # drives 0/2, RM 380Z/480Z).  Each side records IDAM head=0 regardless
        # of which physical head wrote it.  Detect this by checking whether
        # all sectors on physical head 1 carry IDAM head=0.
        #
        # When detected: produce two single-sided RAW_SECTOR images (one per
        # physical side) instead of one merged image.  The HFE and IMD siblings
        # already produced above still represent the complete physical disc and
        # are kept as-is.

        if imd_result['success']:
            all_tracks = parse_imd_tracks(imd_for_detection)
            if all_tracks:
                independent_sides = detect_independent_sides(all_tracks)

        if independent_sides and independent_sides['detected']:
            log.info(
                f"Independent sides detected: {independent_sides['reason']}; "
                f"splitting into two single-sided RAW_SECTOR artefacts"
            )
            # Derive the single-sided gw format: same geometry but heads=1.
            # _geometry_to_gw_format may return None if no map entry exists;
            # fall back to 'ibm.scan' (same as the merged-image fallback).
            if detected_geometry:
                ss_gw_format = _geometry_to_gw_format(
                    **{**detected_geometry, 'heads': 1}
                ) or 'ibm.scan'
            else:
                ss_gw_format = 'ibm.scan'

            cylinders = detected_geometry.get('cylinders', 80) if detected_geometry else 80

            # Generate both single-sided images first, then decide how to
            # register them based on whether their content actually differs.
            side_paths = {}
            for head in (0, 1):
                side_path = work_dir / f"{input_path.stem}_side{head}.img"
                side_result = sector_image_to_raw_greaseweazle_one_side(
                    input_path, side_path, ss_gw_format, head, cylinders
                )
                results.append((f'IMG side {head}', side_result))
                if side_result['success']:
                    side_paths[head] = side_path

            # A freshly-formatted disc (e.g. blank/identical DFS catalogues on
            # both sides) can produce byte-identical side images.  The
            # (item_id, sha256) uniqueness constraint forbids two
            # identical-content artefacts in one item, so the second
            # register_derived_artefact would collide and the web side would
            # re-home/relabel the first (leaving a single artefact mislabelled
            # "Side 1").  Detect that here and register a single combined
            # artefact instead.
            side_hashes = {
                head: compute_file_hash(path)[1]  # (md5, sha256, size) → sha256
                for head, path in side_paths.items()
            }
            sides_identical = (
                len(side_paths) == 2 and side_hashes[0] == side_hashes[1]
            )
            independent_sides['sides_identical'] = sides_identical

            if sides_identical:
                log.info(
                    "Both physical sides are byte-identical (e.g. a blank "
                    "formatted disc) — registering a single combined artefact"
                )
                derived = self.api.register_derived_artefact(
                    analysis_id,
                    f"{artefact_label} (Sides 0 & 1, identical)",
                    side_paths[0],
                    ArtefactType.RAW_SECTOR,
                )
                log.info(f"Created derived combined-sides artefact: {derived}")
            else:
                for head, side_path in side_paths.items():
                    derived = self.api.register_derived_artefact(
                        analysis_id,
                        f"{artefact_label} (Side {head})",
                        side_path,
                        ArtefactType.RAW_SECTOR,
                        # Number each side's partition by its physical side so the
                        # parent disc's aggregated partition list reads
                        # "partition 0" / "partition 1" rather than two "0"s.
                        analysis_hints={'partition_index_base': head},
                    )
                    log.info(f"Created derived side-{head} artefact: {derived}")

        else:
            # ── Step 3: gw convert source → RAW_SECTOR ──────────────────
            # Normal path: single merged image.
            # Always feed gw the original source artefact (closest-to-original).

            img_path = work_dir / f"{input_path.stem}.img"
            img_result = sector_image_to_raw_greaseweazle(input_path, img_path, gw_format=gw_format)
            results.append(('IMG', img_result))

            if img_result['success']:
                derived = self.api.register_derived_artefact(
                    analysis_id,
                    f"{artefact_label} (raw sectors)",
                    img_path,
                    ArtefactType.RAW_SECTOR
                )
                log.info(f"Created derived IMG artefact: {derived}")

    # ── Report ───────────────────────────────────────────────────────────
    any_success = any(r[1]['success'] for r in results)
    summary_parts = [f"{name}: {'OK' if r['success'] else 'FAIL'}" for name, r in results]
    details_dict = {name: r for name, r in results}

    if source_type not in _SCP_VIA_CONVERSION_TYPES:
        details_dict['gw_format_used'] = gw_format
        details_dict['gw_format_source'] = gw_format_source
        if imd_track0_summary:
            details_dict['gw_track0'] = imd_track0_summary
        if detected_geometry:
            details_dict['gw_geometry'] = detected_geometry
        if independent_sides:
            details_dict['independent_sides'] = independent_sides

    if any_success:
        self.complete_analysis(
            analysis_id,
            tool_name='hxcfe,greaseweazle',
            summary='; '.join(summary_parts),
            details=json.dumps(details_dict)
        )
    else:
        self.fail_analysis(
            analysis_id,
            '; '.join(summary_parts),
            tool_name='hxcfe,greaseweazle',
            details=json.dumps(details_dict)
        )


@analysis_handler("disc mastering data detection")
def process_disc_mastering_detect(self, analysis: dict, artefact: dict, work_dir: Path):
    """Process DISC_MASTERING_DETECT analysis.

    Scans the trailing tracks of an HFE image for mastering/duplicator
    fingerprint data (TRACEBACK format and Formaster record).
    """
    from ..tools.hfe import analyse_hfe_mastering  # numpy: worker-only
    analysis_id = analysis['id']
    input_path = self.get_input_path(artefact, work_dir)
    result = analyse_hfe_mastering(input_path, scan_count=MASTERING_TRACK_SCAN_COUNT)
    indicators = result.get('indicators', [])
    if indicators:
        types_found = ', '.join(sorted({i['type'] for i in indicators}))
        summary = f"Mastering data found: {types_found}"
    else:
        summary = "No mastering data found"
    self.complete_analysis(
        analysis_id,
        tool_name='hfe_parser',
        summary=summary,
        details=json.dumps(result),
    )


@analysis_handler("disc copy protection detection")
def process_disc_protection_detect(self, analysis: dict, artefact: dict, work_dir: Path):
    """Process DISC_PROTECTION_DETECT analysis.

    Scans all tracks of an HFE image for copy protection indicators:
    weak/fuzzy bits, intentional bad CRCs, cylinder ID mismatches,
    deleted data address marks, and duplicate sector IDs.
    """
    from ..tools.hfe import analyse_hfe_protection  # numpy: worker-only
    analysis_id = analysis['id']
    input_path = self.get_input_path(artefact, work_dir)
    result = analyse_hfe_protection(input_path)
    indicators = result.get('indicators', [])
    if indicators:
        types_found = ', '.join(sorted({i['type'] for i in indicators}))
        summary = f"Protection indicators found: {types_found}"
    else:
        summary = "No protection indicators found"
    self.complete_analysis(
        analysis_id,
        tool_name='hfe_parser',
        summary=summary,
        details=json.dumps(result),
    )
# vim: ts=4 sw=4 et
