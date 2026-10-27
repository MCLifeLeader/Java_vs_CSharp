"""Fail-closed schema, semantic, evidence and admission validation for suite records."""

import json
import math
from pathlib import Path

from reporting.report import canonical_hash, enforce_schema, verify_evidence


def validate_suite_record(root, record, manifest, *, verify_raw=True):
    root = Path(root)
    enforce_schema(record,json.loads((root/'config/suite-result.schema.json').read_text(encoding='utf-8')))
    unsigned = {key:value for key,value in record.items() if key!='result_hash'}
    if record.get('result_hash')!=canonical_hash(unsigned) or record.get('fingerprint')!=manifest['fingerprint']:
        raise ValueError('Suite result content/provenance changed')
    if record['campaign_id']!=manifest['campaign_id']:
        raise ValueError('Suite campaign identity differs')
    if manifest['stage'] in ('smoke','pilot') and record['admitted']:
        raise ValueError('Smoke or pilot cannot admit ranking evidence')
    if manifest['stage']=='smoke' and record['performance'] is not None:
        raise ValueError('Smoke cannot contain measured performance')
    if record['admitted'] and (record['status']!='measured' or not record.get('gates') or not all(value is True for value in record['gates'].values())):
        raise ValueError('Admission requires every explicit measured gate to pass')
    if record['admitted']:
        resource = record.get('resource_budget')
        if not resource or resource.get('enforced') is not True:
            raise ValueError('Admitted record lacks enforced resource evidence')
        limits = manifest['config'].get('process_limits')
        if not limits or resource.get('cpu_affinity')!=limits.get('cpu_affinity'):
            raise ValueError('Admitted resource affinity differs from frozen configuration')
    for name,value in (record.get('performance') or {}).items():
        if value is None or isinstance(value,str):
            continue
        if type(value) not in (int,float) or not math.isfinite(value) or value<0:
            raise ValueError('Invalid finite numeric performance metric: '+name)
        if record['admitted'] and value==0:
            raise ValueError('Zero measured scoring metric requires diagnosis')
    check = record.get('check')
    if check:
        if check.get('correctness') is not True or type(check.get('work_units')) is not int or check['work_units']<=0 or not isinstance(check.get('unit'),str):
            raise ValueError('Suite correctness/work-unit contract failed')
        batches = record.get('batches') or ([record['batch_output']] if record.get('batch_output') else [])
        for batch in batches:
            output = batch.get('output',batch)
            count = output.get('iterations')
            if type(count) is not int or count<1 or output.get('unit')!=check['unit'] or output.get('work_units')!=count*check['work_units']:
                raise ValueError('Suite batch unit/count differs from independent CHECK')
            if output.get('correctness') is not True or int(output.get('checksum','-1'),16)!=(int(check['checksum'],16)*count)&((1<<64)-1):
                raise ValueError('Suite batch consumed checksum differs')
            if record['admitted'] and (batch.get('elapsed_ns',0)<1e9 or not batch.get('resources') or batch['resources'].get('errors')):
                raise ValueError('Admitted batch lacks duration or valid common OS samples')
        if record['admitted'] and (record.get('warmup') or {}).get('status')!='stable':
            raise ValueError('Admitted batch warmup was not stable')
    raw = record.get('raw_evidence')
    if verify_raw:
        if not isinstance(raw,dict) or not raw:
            raise ValueError('Suite result lacks archived raw evidence hashes')
        verify_evidence(root,{'evidence':raw})


def finalize_record(root, record, manifest, evidence):
    from controller.benchmarkctl import file_hash
    root,evidence = Path(root).resolve(),Path(evidence).resolve()
    if 'raw_evidence' in record:
        # Finalized checkpoints are immutable. Resume verifies their original
        # bytes rather than attaching a new attempt or repairing a changed hash.
        validate_suite_record(root,record,manifest)
        return record
    names = (record['identity']['case_hash'],canonical_hash(record['command']))
    common={'campaign-manifest.json','preflight.json','source.json','input-manifest.json','binaries.json','config.json','versions.json','builds.json'}
    selected = [path for path in evidence.iterdir() if path.is_file() and
                (path.name in common or any(path.name.startswith(name) for name in names))]
    runtime=record.get('runtime_evidence',{})
    if not isinstance(runtime,dict):
        raise ValueError('Runtime evidence must map root-relative paths to SHA256 hashes')
    if runtime:
        verify_evidence(root,{'evidence':runtime})
    native=record.get('native_harness')
    if native:
        key=canonical_hash({'identity':native['identity'],'fingerprint':native['fingerprint']})
        directory=evidence/'harness'/key
        for name,digest in native['files'].items():
            path=(directory/name).resolve()
            if not path.is_relative_to(directory.resolve()) or file_hash(path)!=digest:
                raise ValueError('Native harness raw evidence changed')
            selected.append(path)
        selected.append(directory/'evidence.json')
    record['raw_evidence'] = {str(path.relative_to(root)).replace('\\','/'):file_hash(path) for path in selected}
    record['raw_evidence'].update(runtime)
    record['raw_evidence'].update(record.get('trace_evidence',{}))
    record['result_hash'] = canonical_hash({key:value for key,value in record.items() if key!='result_hash'})
    validate_suite_record(root,record,manifest)
    return record
