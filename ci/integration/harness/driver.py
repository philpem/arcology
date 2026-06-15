"""PipelineDriver: run a fixture through the real worker against the fake server.

For one fixture it:
  1. builds isolated temp ``uploads/``, ``outputs/`` and ``work/`` roots and
     points ``arcworker.config.OUTPUT_DIR``/``UPLOAD_DIR`` at them;
  2. constructs a real ``AnalysisWorker`` with real ``LocalStorage`` and swaps
     in a ``FakeServerAPI``;
  3. seeds the manifest's initial analyses, then dispatches queued jobs (whose
     type is in the manifest's ``run_types``) through the real ``HANDLERS`` until
     no runnable job remains;
  4. returns a result document (events, partitions+files, artefacts,
     leftover queue, output tree) for golden comparison.

Jobs whose type is *not* in ``run_types`` are intentionally left queued and
surface in ``final_queue`` — that is how, e.g., a promoted disc image's
``PARTITION_DETECT`` is asserted without needing the partition tool.
"""

import json
import shutil
import tempfile
from pathlib import Path
from arcworker import config as worker_config
from arcworker.analyses import HANDLERS
from arcworker.analysis import AnalysisWorker
from arcology_shared.enums import ArtefactType
from arcology_shared.storage import LocalStorage
from .analysis_map import IT_ANALYSIS_MAP
from .fake_api import FakeServerAPI


class PipelineError(RuntimeError):
    pass


class PipelineDriver:
    def __init__(self, case_dir: Path):
        self.case_dir = Path(case_dir)
        self.manifest = json.loads((self.case_dir / 'manifest.json').read_text())
        self.name = self.case_dir.name
        # Absolute temp roots → stable placeholders, populated during run() and
        # consumed by normalise() after the temp tree is gone.
        self.roots: dict[str, str] = {}

    def run(self) -> dict:
        tmp = Path(tempfile.mkdtemp(prefix=f'it-{self.name}-'))
        uploads = tmp / 'uploads'
        outputs = tmp / 'outputs'
        work = tmp / 'work'
        for d in (uploads, outputs, work):
            d.mkdir(parents=True, exist_ok=True)

        # Longest paths first so nested replacements are unambiguous.
        self.roots = {
            str(outputs): '<outputs>',
            str(uploads): '<uploads>',
            str(work): '<work>',
            str(tmp): '<tmp>',
        }

        saved_output_dir = worker_config.OUTPUT_DIR
        saved_upload_dir = worker_config.UPLOAD_DIR
        worker_config.OUTPUT_DIR = outputs
        worker_config.UPLOAD_DIR = uploads
        try:
            return self._run_in(uploads, outputs, work)
        finally:
            worker_config.OUTPUT_DIR = saved_output_dir
            worker_config.UPLOAD_DIR = saved_upload_dir
            shutil.rmtree(tmp, ignore_errors=True)

    def _run_in(self, uploads: Path, outputs: Path, work: Path) -> dict:
        storage = LocalStorage(uploads, outputs)
        worker = AnalysisWorker(
            api_url='http://fake-server/api',
            upload_dir=uploads,
            output_dir=outputs,
            api_key='integration-test',
            storage=storage,
        )
        fake = FakeServerAPI(
            api_url='http://fake-server/api',
            upload_dir=uploads,
            output_dir=outputs,
            api_key='integration-test',
            storage=storage,
            analysis_map=IT_ANALYSIS_MAP,
        )
        worker.api = fake

        # Seed the uploaded fixture as the root artefact.
        original_filename = self.manifest['original_filename']
        shutil.copy(self.case_dir / self.manifest['input'], uploads / original_filename)
        artefact_type = ArtefactType[self.manifest['artefact_type']]
        root = fake.add_root_artefact({
            'uuid': 'it-art-0000',
            'slug': 'root',
            'original_filename': original_filename,
            'storage_path': original_filename,
            'storage_directory': 'uploads',
            'artefact_type': artefact_type.value,
        })

        for seed in self.manifest['seed_analyses']:
            fake.seed_analysis(root['uuid'], seed['type'], seed.get('hints'))

        run_types = set(self.manifest['run_types'])
        max_steps = self.manifest.get('max_steps', 50)
        self._drive(worker, fake, run_types, max_steps, work)

        return self._result(fake)

    def _drive(self, worker, fake, run_types, max_steps, work: Path):
        steps = 0
        while True:
            runnable = next(
                (aid for aid in fake.pending
                 if fake.analyses[aid]['analysis_type'] in run_types),
                None,
            )
            if runnable is None:
                return
            if steps >= max_steps:
                raise PipelineError(
                    f"{self.name}: exceeded max_steps={max_steps}; "
                    f"queue still holds "
                    f"{[fake.analyses[a]['analysis_type'] for a in fake.pending]}"
                )
            fake.pending.remove(runnable)
            analysis = fake.analyses[runnable]
            analysis['status'] = 'running'
            handler = HANDLERS.get(analysis['analysis_type'])
            if handler is None:
                raise PipelineError(
                    f"{self.name}: no handler registered for "
                    f"{analysis['analysis_type']}"
                )
            artefact = fake.artefacts[analysis['artefact_uuid']]
            work_dir = work / analysis['uuid']
            work_dir.mkdir(parents=True, exist_ok=True)
            handler(worker, analysis, artefact, work_dir)
            steps += 1

    def _result(self, fake) -> dict:
        partitions = []
        for partition in fake.partitions.values():
            files = [f for f in fake.files
                     if f.get('partition_uuid') == partition['uuid']]
            partitions.append({**partition, 'files': files})

        final_queue = [
            {
                'type': fake.analyses[aid]['analysis_type'],
                'artefact': fake.analyses[aid]['artefact_uuid'],
                'hints': self._decode(fake.analyses[aid].get('hints')),
            }
            for aid in fake.pending
        ]

        outputs = fake.outputs
        output_tree = [
            str(p.relative_to(outputs))
            for p in outputs.rglob('*') if p.is_file()
        ]

        return {
            'case': self.name,
            'events': fake.events,
            'partitions': partitions,
            'artefacts': list(fake.artefacts.values()),
            'final_queue': final_queue,
            'output_tree': output_tree,
        }

    @staticmethod
    def _decode(hints_json):
        if not hints_json:
            return None
        try:
            return json.loads(hints_json)
        except (ValueError, TypeError):
            return hints_json

# vim: ts=4 sw=4 et
