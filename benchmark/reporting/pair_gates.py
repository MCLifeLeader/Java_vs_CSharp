"""Cross-language correctness gates, computed only after both results exist."""
from collections import defaultdict
from reporting.report import canonical_hash

def pair_gate_results(records,stage):
    groups=defaultdict(dict)
    for record in records:
        row=record['identity']
        key=canonical_hash({key:row[key] for key in ('case_id','parameter_id','engine','access','profile','dataset','workers','repetition','track')})
        if row['language'] in groups[key]:
            raise ValueError('Duplicate language observation in pair correctness gate')
        groups[key][row['language']]=record
    for languages in groups.values():
        row=next(iter(languages.values()))['identity']
        complete=set(languages)=={'csharp','java'}
        checks={}
        if row['case_id']=='C06' and row['parameter_id'].startswith('compress'):
            sizes=[((record.get('check') or {}).get('details') or {}).get('compressed_bytes') for record in languages.values()]
            valid=complete and all(type(value) is int and value>0 for value in sizes)
            difference=abs(sizes[0]-sizes[1])/max(sizes) if valid else None
            checks['compressed_output_size']={'passed':valid and difference<=.01,'sizes_bytes':sizes,'relative_difference':difference,'maximum_relative_difference':.01}
        if row['case_id']=='I04' and row['parameter_id']=='handshake':
            details=[(record.get('check') or {}).get('details') or {} for record in languages.values()]
            protocols=[str(item.get('tls_protocol','')).replace('.','').replace('_','').lower().replace('v','') for item in details]
            ciphers=[item.get('cipher') for item in details]
            checks['tls_policy']={'passed':complete and protocols==['tls13','tls13'] and bool(ciphers[0]) and len(set(ciphers))==1,'protocols':protocols,'ciphers':ciphers}
        if row['engine']=='sqlite' and row['case_id'].startswith(('D','H')):
            metadata=[record.get('metadata') or {} for record in languages.values()]
            valid=complete and all(metadata[0].get(key)==metadata[1].get(key) and metadata[0].get(key) for key in ('sqlite_version','sqlite_source_id','compile_options'))
            hashes=[sorted(item['sha256'] for item in value.get('native_libraries',[])) for value in metadata]
            checks['sqlite_native_build']={'passed':bool(valid and hashes[0] and hashes[0]==hashes[1]),'native_sha256':hashes,'reason':'Exact native version/source/options and loaded native bytes required for primary parity'}
        for record in languages.values():
            if checks:
                record['paired_checks']=checks
                if stage=='full':
                    record.setdefault('gates',{}).update({name:value['passed'] for name,value in checks.items()})
                    if 'sqlite_native_build' in checks:
                        record['gates']['native_provider_parity']=checks['sqlite_native_build']['passed']
                    record['admitted']=all(record['gates'].values())
