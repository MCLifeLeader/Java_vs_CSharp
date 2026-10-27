"""Verified frozen diagnostic receipts required for engine admission."""

from reporting.report import canonical_hash,verify_evidence,is_hash


def sqlite_diagnostics(metadata,pilot):
    receipt=(pilot or {}).get('sqlite_native_parity')
    if not isinstance(receipt,dict) or not isinstance(metadata,dict):
        return {'passed':False,'reason':'Frozen SQLite native parity receipt or actual metadata is absent'}
    hashes=sorted(item.get('sha256') for item in metadata.get('native_libraries',[]) if isinstance(item,dict) and is_hash(item.get('sha256')))
    wanted=receipt.get('native_sha256')
    passed=(bool(hashes) and isinstance(wanted,list) and hashes==sorted(wanted)
            and all(metadata.get(key) and metadata.get(key)==receipt.get(key) for key in ('sqlite_version','sqlite_source_id','compile_options')))
    return {'passed':bool(passed),'native_sha256':hashes,
            'reason':'Actual SQLite version, source, compile options and loaded native bytes compared with frozen receipt'}


def network_diagnostics(root,row,pilot,metadata,image):
    key=row['engine']+'/'+row['access']
    receipt=(pilot or {}).get('database_diagnostics',{}).get(key)
    if not isinstance(receipt,dict):
        return {'passed':False,'reason':'Frozen realized catalog, durability/isolation and provider query diagnostics are absent'}
    unsigned={key:value for key,value in receipt.items() if key!='receipt_hash'}
    if receipt.get('receipt_hash')!=canonical_hash(unsigned):
        raise ValueError('Frozen network diagnostic receipt content changed')
    verify_evidence(root,{'evidence':receipt.get('evidence',{})})
    provider=receipt.get('providers',{}).get(row['language'],{})
    passed=(receipt.get('engine')==row['engine'] and receipt.get('access')==row['access'] and receipt.get('image')==image
            and receipt.get('source_hash')==(pilot or {}).get('source_hash') and receipt.get('input_hash')==(pilot or {}).get('input_hash')
            and receipt.get('durability_verified') is True and receipt.get('isolation')=='read_committed'
            and receipt.get('statistics_refreshed') is True and receipt.get('read_preheated') is True
            and bool(receipt.get('evidence')) and all(is_hash(receipt.get(key)) for key in ('catalog_sha256','settings_sha256'))
            and provider.get('metadata_hash')==canonical_hash(metadata)
            and all(is_hash(provider.get(key)) for key in ('command_counts_sha256','generated_sql_sha256','query_plans_sha256')))
    if passed:
        known=set(receipt['evidence'].values())
        passed=all(receipt[key] in known for key in ('catalog_sha256','settings_sha256')) and all(provider[key] in known for key in ('command_counts_sha256','generated_sql_sha256','query_plans_sha256'))
    return {'passed':bool(passed),'receipt_hash':receipt['receipt_hash'],'reason':'Exact frozen engine, catalog, settings and per-provider diagnostics compared with this process metadata'}
