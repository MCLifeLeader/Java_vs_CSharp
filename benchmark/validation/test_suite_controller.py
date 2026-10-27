"""Full-suite controller failure paths, weighted inventory and native evidence parsers."""

from copy import deepcopy
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sys
import struct
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from controller.database import validate_batch,warm_database,operation_trace
from controller.harness import parse_bdn,parse_jmh,corroborates
from controller.suite_http import http_plan,http_contract
from reporting.campaign import expected_leaves,metric_value,analyze_campaign
from reporting.analysis import paired_comparison
from reporting.report import canonical_hash
from reporting.suite_validation import validate_suite_record,finalize_record
from reporting.pair_gates import pair_gate_results
from controller.admission import network_diagnostics,sqlite_diagnostics
from controller.lifecycle import http_load


class SuiteControllerTests(unittest.TestCase):
    def test_interrupted_smoke_closes_cached_http_server(self):
        from controller.suite_http import run_http_rows
        closed = []
        @contextmanager
        def fake_service(*args, **kwargs):
            try:
                yield {"ready": {"port": 12345}, "stdout": [], "stderr": []}
            finally:
                closed.append(True)
        row = {"case_id":"H01", "case_hash":"test", "language":"csharp", "engine":None,
               "access":None, "profile":"controlled", "workers":1, "dataset":"smoke",
               "parameter_id":"get", "repetition":1, "track":"lts"}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            with patch("controller.suite_http.service_process", fake_service), patch("controller.suite_http.first_response", side_effect=KeyboardInterrupt("Injected interrupt")):
                with self.assertRaises(KeyboardInterrupt):
                    run_http_rows(ROOT, path, {"process_timeout_seconds":1}, [row], "smoke", "interrupted", "frozen", path, path)
        self.assertEqual([True], closed)

    def test_full_weight_inventory_exactly_one_hundred_without_campaign_narrowing(self):
        config = json.loads((ROOT/'config/campaign.json').read_text())
        weights = json.loads((ROOT/'config/weights.json').read_text())
        leaves = expected_leaves(ROOT,config,weights)
        self.assertEqual(457,len(leaves))
        self.assertAlmostEqual(100,sum(leaf['weight'] for leaf in leaves))
        self.assertAlmostEqual(3.6,sum(leaf['weight'] for leaf in leaves if leaf['domain']=='D' and leaf['engine']=='sqlite'))
        self.assertFalse(any(leaf['identity']['case_id'] in ('A05','P03') for leaf in leaves))

    def test_failed_slo_does_not_become_sustainable_throughput(self):
        row={'identity':{'case_id':'H01'},'performance':{'slo_throughput':999},'http_load':{'slo':{'slo_satisfied':False}}}
        self.assertIsNone(metric_value(row,'slo_throughput'))

    def test_native_harness_five_forks_required(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'jmh.json'
            path.write_text(json.dumps([{'forks':1,'measurementIterations':5,'primaryMetric':{'score':100,'scoreUnit':'ns/op'}}]))
            with self.assertRaisesRegex(ValueError,'five'):
                parse_jmh(path)

    def test_native_jmh_unit_conversion_and_agreement(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'jmh.json'
            path.write_text(json.dumps([{'forks':5,'measurementIterations':5,'primaryMetric':{'score':1000,'scoreUnit':'ops/s'}}]))
            result=parse_jmh(path)
            self.assertEqual(.001,result['seconds_per_iteration'])
            self.assertTrue(corroborates(.0011,{'parsed':result})['agrees'])
            self.assertFalse(corroborates(.002,{'parsed':result})['agrees'])

    def test_bdn_proves_actual_independent_launches(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'bdn.json'
            row={'Statistics':{'Mean':1000},'Measurements':[{'LaunchIndex':i,'IterationMode':'Workload','IterationStage':'Actual'} for i in range(5)]}
            path.write_text(json.dumps({'Benchmarks':[row]}))
            self.assertAlmostEqual(.000001,parse_bdn(path)['seconds_per_iteration'])
            row['Measurements']=row['Measurements'][:4]
            path.write_text(json.dumps({'Benchmarks':[row]}))
            with self.assertRaisesRegex(ValueError,'five'):
                parse_bdn(path)

    def test_database_latency_count_and_failures_fail_closed(self):
        valid={'attempts':2,'successes':2,'categorized_failures':{},'workers':1,'latencies_ns':[1,2],'wall_seconds':.1}
        validate_batch(valid,2,1)
        for change in ({'latencies_ns':[1]},{'successes':1},{'workers':8},{'categorized_failures':{'conflict':1}},{'latencies_ns':[0,1]}):
            with self.assertRaises(ValueError):
                validate_batch({**valid,**change},2,1)

    def test_database_warmup_counts_operation_time_and_resets_outside_it(self):
        class Worker:
            resets=0
            def command_json(self,case,**fields):
                if case=='RESET':
                    self.resets+=1
                    return {}
                return {'attempts':2,'successes':2,'categorized_failures':{},'workers':1,'latencies_ns':[1,2],'wall_seconds':20}
        worker=Worker()
        result=warm_database(worker,{'case_id':'D05','workers':1},[{},{}],60)
        self.assertEqual('stable',result['status'])
        self.assertEqual(60,result['operation_seconds'])
        self.assertEqual(3,worker.resets)
        self.assertFalse(result['resets_timed'])

    def test_http_finite_checkout_plan_has_independent_final_state(self):
        data=ROOT/'.work/suite-data/smoke'
        if not (data/'trace-measurement.json').is_file():
            self.skipTest('Generate suite smoke fixture to execute this data-backed contract')
        plans,oracle=http_plan(data,{'case_id':'H06','parameter_id':'post-common70'},2)
        self.assertEqual([201,201],[plan['status'] for plan in plans])
        self.assertNotEqual(json.loads(plans[0]['body'])['order_id'],json.loads(plans[1]['body'])['order_id'])
        self.assertEqual(202,oracle.state()['counts']['orders'])
        self.assertTrue(plans[0]['validator'](json.dumps({'id':json.loads(plans[0]['body'])['order_id'],'total_cents':515,'version':2}).encode()))
        with self.assertRaisesRegex(ValueError,'exhausted'):
            http_plan(data,{'case_id':'H06','parameter_id':'post-common70'},101)

    def test_database_warmup_and_measurement_deletes_are_disjoint(self):
        data=ROOT/'.work/suite-data/smoke'
        if not (data/'trace-measurement.json').is_file():
            self.skipTest('Generate suite smoke fixture to execute this data-backed contract')
        warm=operation_trace(data,'D07',100,'warmup')
        measured=operation_trace(data,'D07',100)
        self.assertFalse({x['input']['order_id'] for x in warm}&{x['input']['order_id'] for x in measured})

    def test_compression_output_size_gate_requires_both_languages_within_one_percent(self):
        rows=[]
        for language,size in (('csharp',1000),('java',1005)):
            rows.append({'identity':{'case_id':'C06','parameter_id':'compress-text','language':language,'engine':None,'access':None,
                                     'profile':'controlled','dataset':'S','workers':1,'repetition':1,'track':'lts'},
                         'check':{'details':{'compressed_bytes':size}},'gates':{'oracle':True},'admitted':True})
        pair_gate_results(rows,'full')
        self.assertTrue(all(row['admitted'] for row in rows))
        rows[1]['check']['details']['compressed_bytes']=1100
        pair_gate_results(rows,'full')
        self.assertFalse(any(row['admitted'] for row in rows))
        pair_gate_results(rows[:1],'full')
        self.assertFalse(rows[0]['admitted'])

    def test_tls13_protocol_normalization_still_requires_matching_cipher(self):
        rows=[]
        for language,protocol in (('csharp','Tls13'),('java','TLSv1.3')):
            rows.append({'identity':{'case_id':'I04','parameter_id':'handshake','language':language,'engine':None,'access':None,
                                     'profile':'controlled','dataset':'S','workers':1,'repetition':1,'track':'lts'},
                         'check':{'details':{'tls_protocol':protocol,'cipher':'TLS_AES_256_GCM_SHA384'}},'gates':{'oracle':True},'admitted':True})
        pair_gate_results(rows,'full')
        self.assertTrue(all(row['admitted'] for row in rows))
        rows[1]['check']['details']['cipher']='TLS_AES_128_GCM_SHA256'
        pair_gate_results(rows,'full')
        self.assertFalse(any(row['admitted'] for row in rows))

    def test_database_read_batches_distribute_keys_across_entire_population(self):
        from data.generate import generate_values
        with tempfile.TemporaryDirectory() as directory:
            data=Path(directory)
            (data/'orders.csv').write_text('id\n'+'1\n'*100000)
            (data/'customers.csv').write_text('id\n'+'1\n'*10000)
            (data/'integers.i64le').write_bytes(b''.join(struct.pack('<q',value) for value in generate_values(1000,20261025)))
            (data/'trace-measurement.json').write_text(json.dumps([{'customer_id':index+1} for index in range(1000)]))
            customers=[item['input']['customer_id'] for item in operation_trace(data,'D01',1000)]
            orders=[item['input']['order_id'] for item in operation_trace(data,'D03',1000)]
            pages=[item['input']['cursor_id'] for item in operation_trace(data,'D02',1000)]
            self.assertGreater(max(customers),9000)
            self.assertGreater(len(set(customers)),900)
            self.assertGreater(max(orders),90000)
            self.assertGreater(len(set(pages)),900)

    def test_network_engine_is_not_automatically_admitted(self):
        self.assertFalse(network_diagnostics(ROOT,{'engine':'postgresql','access':'raw'},None,{},'pinnedimage')['passed'])

    def test_sqlite_http_can_admit_an_exact_frozen_native_receipt(self):
        actual={'sqlite_version':'3.53.4','sqlite_source_id':'source','compile_options':['THREADSAFE=1'],
                'native_libraries':[{'sha256':'a'*64}]}
        receipt={key:actual[key] for key in ('sqlite_version','sqlite_source_id','compile_options')}
        receipt['native_sha256']=['a'*64]
        self.assertTrue(sqlite_diagnostics(actual,{'sqlite_native_parity':receipt})['passed'])

    def test_sqlite_http_mismatched_version_or_loaded_native_bytes_stay_excluded(self):
        receipt={'sqlite_version':'3.53.4','sqlite_source_id':'source','compile_options':['THREADSAFE=1'],'native_sha256':['a'*64]}
        actual={**receipt,'native_libraries':[{'sha256':'a'*64}]}
        for changed in ({'sqlite_version':'3.53.3'},{'native_libraries':[{'sha256':'b'*64}]},{'compile_options':['THREADSAFE=0']}):
            self.assertFalse(sqlite_diagnostics({**actual,**changed},{'sqlite_native_parity':receipt})['passed'])

    def test_sqlite_http_without_receipt_or_native_hash_stays_excluded(self):
        self.assertFalse(sqlite_diagnostics({},None)['passed'])
        receipt={'sqlite_version':'3.53.4','sqlite_source_id':'source','compile_options':['THREADSAFE=1'],'native_sha256':[]}
        self.assertFalse(sqlite_diagnostics(receipt,{'sqlite_native_parity':receipt})['passed'])

    def test_http_os_sampling_includes_quiescence_tail_and_archives_arrivals_first(self):
        calls=[]
        class Sampler:
            def __init__(self,pid): pass
            def start(self): calls.append('start')
            def finish(self):
                calls.append('finish')
                return {'cpu_seconds':1,'peak_rss_bytes':1,'errors':[]}
            def close(self): calls.append('close')
        class Process: pid=1
        with patch('controller.lifecycle.ProcessSampler',Sampler),patch('controller.lifecycle.open_loop_http',side_effect=lambda *args,**kwargs:calls.append('arrivals') or {'observations':[{'arrival':0,'status':'success','latency_ms':1}]}):
            result=http_load({'process':Process(),'url':'http://127.0.0.1:1'},'/',b'',1,1,
                             drain_callback=lambda:calls.append('quiesce'),archive_callback=lambda value:calls.append('archive'))
        self.assertEqual(['start','arrivals','archive','quiesce','finish','close'],calls)
        self.assertTrue(result['server_drained'])


class SuiteEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.root=Path(self.temporary.name)
        (self.root/'config').mkdir()
        (self.root/'config/suite-result.schema.json').write_bytes((ROOT/'config/suite-result.schema.json').read_bytes())
        self.log=self.root/'evidence/test/worker.json'
        self.log.parent.mkdir(parents=True)
        self.log.write_text('{}')
        self.manifest={'fingerprint':'a'*64,'campaign_id':'test','stage':'smoke'}
        self.record={'contract_version':'suite-1','campaign_id':'test','fingerprint':'a'*64,'command':['worker'],'status':'validated',
                     'utc_completed':'2026-10-27T00:00:00Z','performance':None,'admitted':False,'reason':'smoke',
                     'identity':{'case_id':'C01','parameter_id':'transform','language':'java','engine':None,'access':None,'profile':'controlled',
                                 'dataset':'smoke','workers':1,'repetition':1,'track':'lts','case_hash':'b'*64},
                     'raw_evidence':{'evidence/test/worker.json':hashlib.sha256(self.log.read_bytes()).hexdigest()}}
        self.sign()
    def tearDown(self):
        self.temporary.cleanup()
    def sign(self):
        self.record['result_hash']=canonical_hash({key:value for key,value in self.record.items() if key!='result_hash'})
    def test_valid_evidence_passes(self):
        validate_suite_record(self.root,self.record,self.manifest)
    def test_raw_tamper_is_not_resigned_during_resume(self):
        self.log.write_text('{"changed":true}')
        with self.assertRaisesRegex(ValueError,'hash differs'):
            finalize_record(self.root,self.record,self.manifest,self.log.parent)
    def test_finalized_record_is_unchanged_when_later_attempt_evidence_appears(self):
        before=json.dumps(self.record,sort_keys=True)
        later=self.log.parent/'attempts/later'
        later.mkdir(parents=True)
        (later/'services.json').write_text('{"port":123}')
        (later/(self.record['identity']['case_hash']+'.worker.json')).write_text('{"new":true}')
        observed=finalize_record(self.root,self.record,self.manifest,self.log.parent)
        self.assertIs(observed,self.record)
        self.assertEqual(json.dumps(observed,sort_keys=True),before)
    def test_changed_finalized_record_is_not_resigned(self):
        original=self.record['result_hash']
        self.record['reason']='changed after finalization'
        with self.assertRaisesRegex(ValueError,'content/provenance'):
            finalize_record(self.root,self.record,self.manifest,self.log.parent)
        self.assertEqual(self.record['result_hash'],original)
    def test_new_record_attaches_only_explicit_attempt_receipts(self):
        del self.record['raw_evidence']
        attempt=self.log.parent/'attempts/current'
        attempt.mkdir(parents=True)
        current=attempt/'services.json'
        current.write_text('{"port":123}')
        other=self.log.parent/'attempts/other'
        other.mkdir(parents=True)
        (other/'services.json').write_text('{"port":456}')
        (self.log.parent/'config.json').write_text('{}')
        self.record['runtime_evidence']={current.relative_to(self.root).as_posix():hashlib.sha256(current.read_bytes()).hexdigest()}
        observed=finalize_record(self.root,self.record,self.manifest,self.log.parent)
        self.assertEqual(set(observed['raw_evidence']),{'evidence/test/config.json','evidence/test/attempts/current/services.json'})
    def test_nonfinite_metric_rejected_even_when_rehashed(self):
        self.manifest['stage']='full'
        self.record.update(status='measured',performance={'throughput_per_second':float('nan')})
        self.sign()
        with self.assertRaisesRegex(ValueError,'numeric'):
            validate_suite_record(self.root,self.record,self.manifest)

    def test_admission_cannot_override_failed_gate(self):
        self.manifest['stage']='full'
        self.record.update(status='measured',performance={'throughput_per_second':1},admitted=True,gates={'oracle':False})
        self.sign()
        with self.assertRaisesRegex(ValueError,'every explicit'):
            validate_suite_record(self.root,self.record,self.manifest)
    def test_batch_work_units_must_match_check(self):
        self.record['check']={'correctness':True,'work_units':2,'unit':'items','checksum':'0000000000000001'}
        self.record['batch_output']={'correctness':True,'iterations':1,'work_units':1,'unit':'items','checksum':'0000000000000001'}
        self.sign()
        with self.assertRaisesRegex(ValueError,'unit/count'):
            validate_suite_record(self.root,self.record,self.manifest)


class CampaignAnalysisIntegrationTests(unittest.TestCase):
    def setUp(self):
        config=json.loads((ROOT/'config/campaign.json').read_text())
        weights=json.loads((ROOT/'config/weights.json').read_text())
        weights['frozen']=True
        self.manifest={'stage':'full','config':config,'weights':weights}
        templates={leaf['identity_key']:leaf['identity'] for leaf in expected_leaves(ROOT,config,weights)}
        self.records=[]
        for row in templates.values():
            for repetition in range(1,31 if row['case_id']=='P01' else 6):
                for language in ('csharp','java'):
                    faster=language=='csharp'
                    identity={**row,'language':language,'repetition':repetition}
                    if identity['case_id'].startswith('H'):
                        identity['parameter_id']=identity['parameter_id'].rsplit('-',1)[0]+'-common70'
                    low,high=(1,2) if faster else (2,1)
                    self.records.append({'identity':identity,'admitted':True,'gates':{'verified':True},
                                         'performance':{'throughput_per_second':high,'cpu_per_work_unit':low,'peak_rss_bytes':low,
                                                        'slo_throughput':high,'p99_ms':low,'cpu_seconds':low,
                                                        'total_cpu_per_work_unit':low,'first_correct_seconds':low,
                                                        'idle_rss_bytes':low,'runtime_inclusive_bytes':low},
                                         'http_load':{'successful':1,'slo':{'slo_satisfied':True}}})
        self.bootstrap=patch('reporting.campaign.paired_comparison',side_effect=lambda pairs,direction,seed:paired_comparison(pairs,direction,seed,resamples=50))
        self.bootstrap.start()
    def tearDown(self):
        self.bootstrap.stop()
    def test_complete_portfolio_produces_expected_index_and_sensitivity_weights(self):
        result=analyze_campaign(ROOT,self.manifest,self.records)
        self.assertEqual('complete',result['primary']['status'])
        self.assertAlmostEqual(100,result['primary']['performance_index']['csharp'])
        self.assertAlmostEqual(50,result['primary']['performance_index']['java'])
        self.assertEqual(457,len(result['comparisons']))
        for view in result['sensitivities'].values():
            self.assertEqual('complete',view['status'])
            self.assertAlmostEqual(100,view['original_covered_weight'])
    def test_missing_metric_preserves_gap_in_full_index(self):
        for record in self.records:
            if record['identity']['case_id']=='C01':
                record['performance'].pop('cpu_per_work_unit',None)
        result=analyze_campaign(ROOT,self.manifest,self.records)
        self.assertIsNone(result['primary']['performance_index'])
        self.assertTrue(result['primary']['missing'])
    def test_duplicate_non_slo_pair_metric_rejects(self):
        self.records.append(deepcopy(next(record for record in self.records if record['identity']['case_id']=='C01')))
        with self.assertRaisesRegex(ValueError,'Duplicate measured'):
            analyze_campaign(ROOT,self.manifest,self.records)
    def test_http_slo_selects_actual_max_while_latency_uses_common_rate(self):
        item=deepcopy(next(record for record in self.records if record['identity']['case_id']=='H01' and record['identity']['language']=='csharp'))
        item['identity']['parameter_id']='get-capacity100'
        item['performance'].update(slo_throughput=3,p99_ms=999)
        self.records.append(item)
        result=analyze_campaign(ROOT,self.manifest,self.records)
        leaves=[leaf for leaf in result['comparisons'] if leaf['identity']['case_id']=='H01']
        throughput=next(leaf for leaf in leaves if leaf['metric']=='slo_throughput')
        latency=next(leaf for leaf in leaves if leaf['metric']=='common_rate_p99')
        self.assertGreater(throughput['comparison']['ratio'],2)
        self.assertAlmostEqual(2,latency['comparison']['ratio'])


if __name__=='__main__':
    unittest.main()
