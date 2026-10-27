"""Named-framework HTTP and lifecycle cases with owned database correctness gates."""

from contextlib import ExitStack,contextmanager
import http.client
import json
from pathlib import Path
import time

from controller.lifecycle import first_response, http_load, idle_load, owned_build, service_process,packaged_service
from controller.execution import paired_schedule
from reporting.report import canonical_hash


def http_key(row):
    endpoint = 'H02/get-1024' if row['case_id'].startswith('P') else row['case_id'] + '/' + row['parameter_id'].rsplit('-', 1)[0]
    return canonical_hash({'endpoint': endpoint, **{key: row.get(key) for key in ('engine','access','profile','dataset','workers')}})


def database_http(row):
    return row['case_id'] in ('H04','H05','H06') or (row['case_id'] == 'P01' and row['parameter_id'] == 'H04')


def http_contract(data, row):
    from controller.suite import lookup_oracle
    from validation.database_oracle import Oracle, new_order
    data = Path(data)
    case = row['case_id']
    if database_http(row):
        oracle = Oracle(data)
        if case == 'H04' or case == 'P01':
            path, value, method, parameter = '/customer/1', oracle.execute('D01', {'customer_id':1}), 'GET', None
        elif case == 'H05' and row['parameter_id'].startswith('get'):
            path, value, method, parameter = '/order/1', oracle.execute('D03', {'order_id':1}), 'GET', None
        else:
            parameter = new_order(2000000000)
            operation = 'H06' if case == 'H06' else 'D06'
            value = oracle.execute(operation, parameter)
            path, method = ('/checkout' if case == 'H06' else '/order'), 'POST'
        expected = json.dumps(value, ensure_ascii=False, separators=(',',':')).encode()
        return path, expected, method, json.dumps(parameter).encode() if parameter else None, lambda payload: json.loads(payload) == value
    if case == 'H01':
        return '/plaintext', b'0123456789abcdef0123456789abcdef', 'GET', None, None
    if case == 'H03':
        oracle_row = {**row, 'case_id':'C01', 'parameter_id':'transform'}
        return '/compute', lookup_oracle(data, oracle_row)['checksum'].encode(), 'GET', None, None
    if case in ('H02','P01','P02','P04'):
        size = 65536 if '65536' in row['parameter_id'] else 1024
        expected = (data / f'document-{size}.json').read_bytes()
        method = 'POST' if row['parameter_id'].startswith('post') else 'GET'
        return f'/json?size={size}', expected, method, expected if method == 'POST' else None, lambda payload: json.loads(payload) == json.loads(expected)
    raise ValueError('Unknown HTTP contract')


def control(server, reset=False,metadata=False):
    connection = http.client.HTTPConnection('127.0.0.1', server['ready']['port'], timeout=60)
    try:
        connection.request('POST' if reset else 'GET', '/__benchmark/metadata' if metadata else '/__benchmark/reset' if reset else '/__benchmark/state')
        response = connection.getresponse()
        value = json.loads(response.read())
        if response.status != 200:
            raise ValueError('Owned database state/reset control failed')
        return value
    finally:
        connection.close()


def drain_server(server,timeout=300):
    connection=http.client.HTTPConnection('127.0.0.1',server['ready']['port'],timeout=timeout)
    try:
        connection.request('POST','/__benchmark/quiesce')
        response=connection.getresponse()
        value=json.loads(response.read())
        if response.status!=200 or value!={'active':0}:
            raise ValueError('HTTP server could not close admissions and drain active operations')
    finally:
        connection.close()


def committed_orders(server,ids):
    result=set()
    for offset in range(0,len(ids),500):
        chunk=ids[offset:offset+500]
        connection=http.client.HTTPConnection('127.0.0.1',server['ready']['port'],timeout=60)
        try:
            connection.request('POST','/__benchmark/committed',body=json.dumps(chunk).encode(),headers={'Content-Type':'application/json'})
            response=connection.getresponse()
            values=json.loads(response.read())
            if response.status!=200 or not isinstance(values,list) or any(type(value) is not int or value not in chunk for value in values) or len(values)!=len(set(values)):
                raise ValueError('Committed-ID audit returned an invalid ledger')
            result.update(values)
        finally:
            connection.close()
    return result


def http_plan(data, row, count, *, phase='measurement'):
    """Prepare finite request bodies and expected outputs before the measured arrivals."""
    from validation.database_oracle import Oracle, new_order
    oracle = Oracle(data)
    if not database_http(row):
        return None, None
    path=Path(data)/f'http-trace-{phase}.json'
    if not path.is_file():
        path=Path(data)/f'trace-{phase}.json'
    trace = json.loads(path.read_text(encoding='utf-8'))
    mutation=row['case_id']=='H06' or (row['case_id']=='H05' and row['parameter_id'].startswith('post'))
    if count > len(trace) and mutation:
        raise ValueError('Finite HTTP trace exhausted; enlarge and freeze inputs in pilot, never wrap')
    if not mutation and count>len(trace):
        trace=[trace[index%len(trace)] for index in range(count)]
    plans = []
    for index, item in enumerate(trace[:count]):
        if row['case_id'] == 'H04':
            path, case, parameter, method = f"/customer/{item['customer_id']}", 'D01', {'customer_id':item['customer_id']}, 'GET'
        elif row['case_id'] == 'H05' and row['parameter_id'].startswith('get'):
            key = index % len(oracle.tables['orders']) + 1
            path, case, parameter, method = f'/order/{key}', 'D03', {'order_id':key}, 'GET'
        else:
            path, case, method = ('/checkout','H06','POST') if row['case_id'] == 'H06' else ('/order','D06','POST')
            parameter = new_order(item['insert_id'], customer_id=item['customer_id'])
        # Canonical inputs have already passed rollback/constraint tests. Avoid
        # cloning the entire database for each known-valid untimed oracle step.
        value = oracle._execute(case, parameter)
        if case == 'H06':
            # Completion order of a customer's independent concurrent purchases
            # may differ. Exact final balances/version counts are checked below.
            def validator(payload, key=value['id'], total=value['total_cents']):
                observed = json.loads(payload)
                return set(observed) == {'id','total_cents','version'} and observed['id'] == key and observed['total_cents'] == total and type(observed['version']) is int and observed['version'] > 0
        else:
            def validator(payload, wanted=value):
                return json.loads(payload) == wanted
        plans.append({'path':path, 'method':method, 'body':json.dumps(parameter).encode() if method == 'POST' else None,
                      'status':201 if method == 'POST' else 200, 'validator':validator})
    return plans, oracle


def rate_for_row(row, pilot):
    rates = (pilot or {}).get('http', {}).get(http_key(row))
    if not rates:
        raise ValueError('HTTP load grid must be frozen from a common paired pilot')
    point = row['parameter_id'].rsplit('-',1)[-1]
    if point == 'common70':
        return .7 * min(rates['csharp_sustainable'], rates['java_sustainable'])
    if point.startswith('capacity'):
        return rates['common_capacity'] * int(point[len('capacity'):]) / 100
    return rates['common_capacity'] * .7


def run_http_rows(root, data, config, rows, stage, campaign_id, fingerprint, directory, evidence, *, pilot=None, resume=False):
    from controller.benchmarkctl import atomic_json, utc_now
    from controller.suite import suite_command, worker_environment, process_limits
    from validation.database_oracle import Oracle
    root, data = Path(root), Path(data)
    records, failures = [], []
    cache={'stack':ExitStack(),'key':None,'server':None}
    @contextmanager
    def server_for(command,environment,limits,row):
        reusable = stage=='smoke' and row['case_id'].startswith('H') and (not database_http(row) or row['engine']=='sqlite')
        if not reusable:
            with service_process(command,root,config['process_timeout_seconds'],environment=environment,limits=limits) as server:
                yield server
            return
        key=tuple(command)
        if cache['key']!=key:
            cache['stack'].close()
            cache['stack']=ExitStack()
            cache['server']=cache['stack'].enter_context(service_process(command,root,config['process_timeout_seconds'],environment=environment,limits=limits))
            cache['key']=key
        yield cache['server']
    if stage=='smoke':
        rows=sorted(rows,key=lambda row:(row['language'],str(row['engine']),str(row['access']),row['profile'],row['workers'] or 1,row['case_id'],row['parameter_id']))
    generator_proof = None
    if stage == 'full' and any(row['case_id'].startswith('H') for row in rows):
        from controller.generator import calibrate_generator
        maximum = max(rate_for_row(row,pilot) for row in rows if row['case_id'].startswith('H'))
        generator_proof = calibrate_generator(data,maximum,fingerprint,seconds=config.get('generator_calibration_seconds',10))
        atomic_json(evidence / 'generator-capacity.json',generator_proof)
    if stage == 'full':
        schedule = paired_schedule(rows, config['seed'])
        atomic_json(evidence / 'http-paired-schedule.json', schedule)
        rows = [row for pair in schedule for row in pair['rows']]
    for row in rows:
        checkpoint = directory / 'records' / (row['case_hash'] + '.json')
        if checkpoint.is_file() and resume:
            result = json.loads(checkpoint.read_text(encoding='utf-8'))
            unsigned = {key:value for key,value in result.items() if key != 'result_hash'}
            if result.get('fingerprint') != fingerprint or result.get('result_hash') != canonical_hash(unsigned):
                cache['stack'].close()
                raise ValueError('HTTP checkpoint provenance/content changed')
            records.append(result)
            continue
        owned = ExitStack()
        try:
            command = suite_command(root, data, row, server=True)
            deployment=None
            if row['case_id']=='P04':
                command,deployment=owned.enter_context(packaged_service(root,data,row))
            environment = worker_environment(root)
            if database_http(row) and row['engine'] != 'sqlite':
                from controller.infrastructure import DatabaseEnvironment
                infrastructure = owned.enter_context(DatabaseEnvironment(root, config, row['engine']))
                database_name = infrastructure.create_database()
                environment.update(infrastructure.worker_environment(database_name, row['language']))
                if row['case_id']=='P01':
                    from controller.database import DatabaseWorker,database_command
                    initializer=database_command(root,data,row)
                    with DatabaseWorker(initializer,root,max(600,config['process_timeout_seconds']),environment=environment) as worker:
                        if worker.ready['state']!=Oracle(data).state():
                            raise ValueError('Cold startup database dependency was not canonically prepared')
                        atomic_json(evidence/(row['case_hash']+'.startup-database.json'),{'command':initializer,'ready':worker.ready,'stdout':worker.stdout,'stderr':worker.stderr})
                    environment['BENCH_DB_INITIALIZE']='0'
            if row['case_id'] in ('P03','L05'):
                variant = 'package' if row['case_id'] == 'P03' else row['parameter_id']
                build = owned_build(root, row['language'], variant)
                result = {'status':'validated' if stage == 'smoke' else 'measured', 'build':build,
                          'performance':None if stage == 'smoke' else {'build_seconds':build['elapsed_ns']/1e9, 'artifact_bytes':build['artifact_bytes']}}
            else:
                path, expected, method, body, validator = http_contract(data, row)
                initial = Oracle(data).state() if database_http(row) else None
                with server_for(command,environment,process_limits(config,row) if stage=='full' else None,row) as server:
                    if initial and row['case_id']!='P01' and control(server,True) != initial:
                        raise ValueError('HTTP initial database state differs from independent canonical oracle')
                    correct = first_response(server,path,expected,method,body,validator)
                    if row['case_id']=='P01' and initial:
                        correct['database_initialization_boundary']='SQLite canonical import inside fresh process, included' if row['engine']=='sqlite' else 'Canonical network database prepared by helper before fresh application launch'
                    if initial and row['case_id']=='P01' and control(server)!=initial:
                        raise ValueError('Cold startup returned correct output but dependency state differs')
                    metadata=control(server,metadata=True) if initial and stage=='full' else None
                    diagnostic=None
                    if initial and row['engine']=='sqlite' and stage=='full':
                        from controller.admission import sqlite_diagnostics
                        diagnostic=sqlite_diagnostics(metadata,pilot)
                    if initial and row['engine']!='sqlite' and stage=='full':
                        from controller.admission import network_diagnostics
                        diagnostic=network_diagnostics(root,row,pilot,metadata,infrastructure.profile['image'])
                    if row['case_id'] == 'P02':
                        timeline = idle_load(server,expected,10 if stage == 'smoke' else rate_for_row(row,pilot),smoke=stage=='smoke')
                        result = {'status':'validated' if stage=='smoke' else 'measured','timeline':timeline,
                                  'performance':None if stage=='smoke' else {'idle_rss_bytes':timeline['idle_rss_bytes']}}
                    elif row['case_id'] in ('P01','P04'):
                        result = {'status':'validated' if stage=='smoke' else 'measured','startup':correct,
                                  'performance':None if stage=='smoke' else {'first_correct_seconds':correct['launch_to_first_correct_ns']/1e9}}
                    elif stage == 'smoke':
                        result = {'status':'validated','http_check':correct,'performance':None}
                        if initial:
                            plan, oracle = http_plan(data,row,1)
                            control(server,True)
                            item = plan[0]
                            first_response(server,item['path'],b'',item['method'],item['body'],item['validator'])
                            if control(server) != oracle.state():
                                raise ValueError('HTTP committed final state differs from independent oracle')
                            result['final_state'] = oracle.state()
                    else:
                        def load(rate,duration,phase='measurement'):
                            plans,oracle = http_plan(data,row,int(rate*duration),phase=phase)
                            if initial:
                                if control(server,True) != initial:
                                    raise ValueError('HTTP warm factory reset differs from canonical snapshot')
                            else:
                                control(server,True)
                            raw_path=evidence/(row['case_hash']+'.load-'+phase+'-'+str(rate)+'.json')
                            measured = http_load(server,path,expected,rate,duration,method=method,body=body,validator=validator,
                                                 expected_status=201 if method=='POST' else 200,requests=plans,
                                                 max_connections=1 if row['engine']=='sqlite' else 256,
                                                 drain_callback=lambda:drain_server(server,config.get('http_drain_timeout_seconds',300)),
                                                 archive_callback=lambda result:atomic_json(raw_path,result))
                            atomic_json(raw_path,measured)
                            if oracle is not None:
                                if not measured['server_drained']:
                                    measured['oracle_state_matches']=False
                                    return measured
                                mutation=method=='POST'
                                if mutation and measured['failures']:
                                    parameters=[json.loads(plan['body']) for plan in plans]
                                    committed=committed_orders(server,[item['order_id'] for item in parameters])
                                    oracle=Oracle(data)
                                    operation='H06' if row['case_id']=='H06' else 'D06'
                                    for parameter in parameters:
                                        if parameter['order_id'] in committed:
                                            oracle._execute(operation,parameter)
                                    measured['committed_order_ids']=sorted(committed)
                                    measured['commit_boundary']='Server active operations drained to zero; actual committed scheduled IDs independently replayed'
                                actual = control(server)
                                measured['final_state'] = actual
                                measured['oracle_final_state'] = oracle.state()
                                measured['oracle_state_matches'] = actual == oracle.state()
                                atomic_json(raw_path,measured)
                            return measured
                        if stage == 'pilot':
                            rates = config.get('http_pilot_rates',[10,50,100,500,1000])
                            loads=[]
                            for rate in rates:
                                loads.append(load(rate,config.get('http_pilot_seconds',10)))
                                if not loads[-1]['server_drained']:
                                    break
                            result = {'status':'pilot','pilot_loads':loads,'http_pilot_key':http_key(row),'performance':None}
                        else:
                            rate = rate_for_row(row,pilot)
                            warmup = load(rate,config['warmup_seconds'],'warmup')
                            if not warmup['server_drained']:
                                raise ValueError('Warmup server could not quiesce; measured phase was not started')
                            measured = load(rate,config['measurement_seconds'])
                            from controller.generator import verify_generator
                            measured['generator_capacity_calibrated'] = verify_generator(generator_proof,fingerprint,rate)
                            result = {'status':'measured','warmup':warmup,'http_load':measured,
                                      'performance':{'slo_throughput':measured['slo']['successful_requests_per_second'],
                                                     'p99_ms':measured['slo']['successful_response_p99_ms'],
                                                     'cpu_seconds':measured['resources']['cpu_seconds'],
                                                     'peak_rss_bytes':measured['resources']['peak_rss_bytes']},
                                      'gates':{'oracle_state':measured.get('oracle_state_matches',True),
                                               'generator_capacity':measured['generator_capacity_calibrated'],
                                               'measurement_duration':measured['arrival_window_seconds']>=config['measurement_seconds'],
                                               'warmup_duration':warmup['arrival_window_seconds']>=config['warmup_seconds'],
                                               'collector':not measured['resources']['errors'],
                                               'server_drained':measured['server_drained'],
                                               'resource_limits':server['budget'] is not None and server['budget'].evidence['enforced'],
                                               'native_provider_parity':not database_http(row) or bool(diagnostic and diagnostic['passed'])}}
                            result['gates']['frozen_native_receipt']=not database_http(row) or row['engine']!='sqlite' or bool(diagnostic and diagnostic['passed'])
                    result['resource_budget'] = server['budget'].evidence if server['budget'] else None
                    result['metadata']=metadata
                    result['network_diagnostics']=diagnostic
                    atomic_json(evidence/(row['case_hash']+'.server.json'),{'command':command,'ready':server['ready'],'stdout':server['stdout'],'stderr':server['stderr']})
            if database_http(row) and row['engine'] != 'sqlite':
                infrastructure.drop_database(database_name)
                atomic_json(evidence/(row['case_hash']+'.infrastructure.json'),{'engine':row['engine'],'owner':infrastructure.owner,'image':infrastructure.profile['image'],'commands':infrastructure.logs})
            result.update({'contract_version':'suite-1','campaign_id':campaign_id,'stage':stage,'identity':row,'fingerprint':fingerprint,
                           'command':command,'utc_completed':utc_now(),'admitted':False,
                           'reason':'Smoke/pilot or diagnostics; calibrated generator, complete resource and paired gates required for ranking'})
            if stage == 'full':
                if row['case_id']=='P01':
                    result['gates'] = {'first_response_correct':True,'resource_limits':bool((result.get('resource_budget') or {}).get('enforced'))}
                elif row['case_id']=='P02':
                    result['gates'] = {'full_idle_timeline':result['timeline']['idle_seconds']==30,
                                       'full_load_timeline':result['timeline']['load_seconds']==60,
                                       'resource_limits':bool((result.get('resource_budget') or {}).get('enforced'))}
                elif row['case_id']=='P04':
                    result['deployment'] = deployment
                    result['performance']['runtime_inclusive_bytes'] = result['deployment'].get('runtime_inclusive_bytes')
                    result['gates'] = {'first_response_correct':True,'packaged_artifact_verified':bool(result['deployment'].get('artifacts')),
                                       'runtime_size_measured':bool(result['performance']['runtime_inclusive_bytes']),
                                       'resource_limits':bool((result.get('resource_budget') or {}).get('enforced'))}
                result['admitted'] = bool(result.get('gates')) and all(result['gates'].values())
            result['result_hash'] = canonical_hash(result)
            atomic_json(checkpoint,result)
            records.append(result)
        except Exception as error:
            failure = {'identity':row,'status':'blocked_or_invalid','reason':str(error),'admitted':False}
            atomic_json(directory/'failures'/(row['case_hash']+'.json'),failure)
            failures.append(failure)
        finally:
            owned.close()
    cache['stack'].close()
    return records,failures
