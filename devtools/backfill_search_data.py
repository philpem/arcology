#!/usr/bin/env python3
"""
Backfill search index tables from existing completed analysis results.

Reads all completed DISC_PROTECTION_DETECT, DISC_MASTERING_DETECT, and
PARTITION_DETECT analyses in the database and populates:
  - artefact_protection  (from DISC_PROTECTION_DETECT)
  - artefact_mastering   (from DISC_MASTERING_DETECT)
  - partitions.gnu_file_type  (from PARTITION_DETECT)

Run once after applying the b2e8f4a1c9d3 migration:
  python devtools/backfill_search_data.py

The SQLALCHEMY_DATABASE_URI environment variable must be set, or a
myapp/myapp.cfg must exist with the database URI configured.

The script is idempotent: it deletes existing rows for each artefact
before inserting fresh ones, so it is safe to run multiple times.
"""

import json
import os
import sys

# Allow imports from the project root
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


def main():
	from myapp.app import create_app
	from myapp.extensions import db
	from myapp.database import (
		Analysis, AnalysisType, AnalysisStatus,
		Partition, ArtefactProtection, ArtefactMastering,
	)

	app = create_app()

	with app.app_context():
		prot_count = 0
		mast_count = 0
		part_count = 0

		# --- DISC_PROTECTION_DETECT ---
		analyses = (
			Analysis.query
			.filter_by(
				analysis_type=AnalysisType.DISC_PROTECTION_DETECT,
				status=AnalysisStatus.COMPLETED,
				success=True,
			)
			.all()
		)
		print(f"Processing {len(analyses)} DISC_PROTECTION_DETECT analyses…")
		for analysis in analyses:
			if not analysis.details:
				continue
			try:
				details = json.loads(analysis.details)
			except (ValueError, TypeError):
				print(f"  WARNING: could not parse details for analysis {analysis.uuid}")
				continue

			ArtefactProtection.query.filter_by(artefact_id=analysis.artefact_id).delete()
			for ind in details.get('indicators', []):
				db.session.add(ArtefactProtection(
					artefact_id=analysis.artefact_id,
					protection_type=ind.get('type', 'unknown'),
					track=ind.get('track'),
					side=ind.get('side'),
					details=ind.get('sector_id') or ind.get('details'),
				))
				prot_count += 1

		# --- DISC_MASTERING_DETECT ---
		analyses = (
			Analysis.query
			.filter_by(
				analysis_type=AnalysisType.DISC_MASTERING_DETECT,
				status=AnalysisStatus.COMPLETED,
				success=True,
			)
			.all()
		)
		print(f"Processing {len(analyses)} DISC_MASTERING_DETECT analyses…")
		for analysis in analyses:
			if not analysis.details:
				continue
			try:
				details = json.loads(analysis.details)
			except (ValueError, TypeError):
				print(f"  WARNING: could not parse details for analysis {analysis.uuid}")
				continue

			ArtefactMastering.query.filter_by(artefact_id=analysis.artefact_id).delete()
			for ind in details.get('indicators', []):
				db.session.add(ArtefactMastering(
					artefact_id=analysis.artefact_id,
					mastering_type=ind.get('type', 'unknown'),
					track=ind.get('track'),
					decoded=ind.get('decoded') or ind.get('data'),
				))
				mast_count += 1

		# --- PARTITION_DETECT (gnu_file_type) ---
		analyses = (
			Analysis.query
			.filter_by(
				analysis_type=AnalysisType.PARTITION_DETECT,
				status=AnalysisStatus.COMPLETED,
				success=True,
			)
			.all()
		)
		print(f"Processing {len(analyses)} PARTITION_DETECT analyses…")
		for analysis in analyses:
			if not analysis.details:
				continue
			try:
				details = json.loads(analysis.details)
			except (ValueError, TypeError):
				print(f"  WARNING: could not parse details for analysis {analysis.uuid}")
				continue

			gnu_file_type = details.get('file', {}).get('file_type')
			if gnu_file_type:
				updated = (
					Partition.query
					.filter_by(artefact_id=analysis.artefact_id)
					.update({'gnu_file_type': gnu_file_type})
				)
				part_count += updated

		db.session.commit()
		print(
			f"\nDone.\n"
			f"  Protection indicators inserted: {prot_count}\n"
			f"  Mastering indicators inserted:  {mast_count}\n"
			f"  Partitions updated (gnu_file_type): {part_count}\n"
		)


if __name__ == '__main__':
	main()
