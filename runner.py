"""Freeze inputs and run a resumable matrix with bounded concurrent requests."""
import concurrent.futures
import copy
import hashlib
import json
import os
import random
import shutil
import sqlite3
import threading
import time
from collections import Counter, defaultdict
from decimal import Decimal, ROUND_CEILING
from pathlib import Path

import builder as ps
import client
from storage import acquire_lock, write_result_atomic

ROOT = Path(__file__).resolve().parent
FROZEN = ROOT / 'runs'
RESULTS = ROOT / 'results'
CODE_FILES = ['run.py', 'runner.py', 'builder.py', 'core.py', 'decision.py',
              'storage.py', 'client.py', 'transport.py', 'rescore.py']
SCALE = Decimal('1000000000000')

def units(value):
    value=Decimal(str(value))
    if not value.is_finite() or value<0:raise ValueError('Invalid budget amount')
    return int((value*SCALE).to_integral_value(rounding=ROUND_CEILING))


class Billing:
    """Unknown and in-flight requests keep their reserve; known costs replace it."""
    def __init__(self,path,limit):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
        self.lock=threading.RLock();self.limit=None if limit is None else units(limit)
        self.db=sqlite3.connect(self.path,check_same_thread=False,timeout=30)
        self.db.execute('PRAGMA journal_mode=WAL');self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS state(id INTEGER PRIMARY KEY, cap INTEGER, exposure INTEGER)')
        self.db.execute('CREATE TABLE IF NOT EXISTS attempts(key TEXT PRIMARY KEY, reserved INTEGER, actual INTEGER)')
        self.db.execute('INSERT OR IGNORE INTO state VALUES(1,?,0)',(self.limit,));self.db.commit()
        if self.db.execute('SELECT cap FROM state WHERE id=1').fetchone()[0]!=self.limit:raise ValueError('Existing budget cap cannot silently change')
        self.data={'reservations':self}
    def __contains__(self,key):
        with self.lock:return self.db.execute('SELECT 1 FROM attempts WHERE key=?',(key,)).fetchone() is not None
    def reserve(self,key,amount):
        amount=units(amount)
        with self.lock,self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if key in self:raise RuntimeError('Request already reserved; no silent replay')
            exposure=self.db.execute('SELECT exposure FROM state WHERE id=1').fetchone()[0]
            if self.limit is not None and exposure+amount>self.limit:raise client.BudgetStop('formal_budget_limit')
            self.db.execute('INSERT INTO attempts VALUES(?,?,NULL)',(key,amount))
            self.db.execute('UPDATE state SET exposure=exposure+? WHERE id=1',(amount,))
    def record(self,key,amount):
        if amount is None:return
        amount=units(amount)
        with self.lock,self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row=self.db.execute('SELECT reserved,actual FROM attempts WHERE key=?',(key,)).fetchone()
            if row is None:raise ValueError('Billed attempt has no dispatch reservation')
            previous=row[0] if row[1] is None else row[1]
            self.db.execute('UPDATE attempts SET actual=? WHERE key=?',(amount,key))
            self.db.execute('UPDATE state SET exposure=exposure+? WHERE id=1',(amount-previous,))
    def snapshot(self):
        with self.lock:
            exposure=self.db.execute('SELECT exposure FROM state WHERE id=1').fetchone()[0]
            count,known,actual,unknown=self.db.execute('SELECT COUNT(*),COUNT(actual),COALESCE(SUM(actual),0),COALESCE(SUM(CASE WHEN actual IS NULL THEN reserved ELSE 0 END),0) FROM attempts').fetchone()
            return {'limit_usd':None if self.limit is None else str(Decimal(self.limit)/SCALE),'exposure_usd':str(Decimal(exposure)/SCALE),
                    'known_cost_usd':str(Decimal(actual)/SCALE),'unknown_or_pending_reserve_usd':str(Decimal(unknown)/SCALE),
                    'dispatches':count,'dispatches_with_known_cost':known}
    def close(self):self.db.close()


def code_hashes():
    return {name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in CODE_FILES}


def build_jobs(cfg,tasks,code_hash):
    result=[];config_hash=ps.digest(cfg)
    retry_cells = {tuple(c) for c in cfg['retry_parse_cells']} if 'retry_parse_cells' in cfg else None
    for task in tasks:
        if cfg.get('phase') == 'preflight' and cfg.get('preflight_task_ids') and task['task_id'] not in cfg['preflight_task_ids']:
            continue
        task_hash=ps.digest(task)
        for c in task['conditions']:
            if cfg.get('phase') == 'preflight' and c['id'] not in cfg.get('preflight_condition_ids', ['baseline', 'O1_p25', 'E1_p25', 'S1_p25', 'C6_q100', 'T2_q100']):
                continue
            for m in cfg['models']:
                for rep in range(1,cfg['repeats']+1):
                    if retry_cells is not None and (task['task_id'], c['id'], m['slug'], rep) not in retry_cells:
                        continue
                    identity={'task_id':task['task_id'],'condition_id':c['id'],'model':m['slug'],'repeat':rep,
                              'task_hash':task_hash,'config_hash':config_hash,'code_hash':code_hash}
                    result.append({'task':task,'condition':c,'model':m,'identity':identity,'job_id':ps.digest(identity)})
    random.Random(cfg['shuffle_seed']).shuffle(result)
    if cfg.get('preflight_profile') == 'coverage':
        # Probe one baseline per model before spending requests on manipulation cases.
        result.sort(key=lambda j: (j['condition']['id'] != 'baseline',
                                  j['model']['id'] if j['condition']['id'] == 'baseline' else ''))
    return result


def load_run(path,verify_live_code=True):
    path=Path(path);meta=ps.load(path/'freeze.json');cfg=ps.load(path/'config.json')
    tasks=[ps.load(p) for p in sorted((path/'tasks').glob('*.json'))]
    if meta.get('design') != ps.readiness(tasks):raise ValueError('Frozen design readiness changed')
    definition={'config':cfg,'task_hashes':{t['task_id']:ps.digest(t) for t in tasks},'code_hashes':meta['code_hashes']}
    if ps.digest(definition)[:20]!=meta['run_id']:raise ValueError('Frozen definition changed')
    if len(tasks)!=12 or any(ps.validate(t) for t in tasks):raise ValueError('Frozen scenarios invalid')
    for name,expected in meta['code_hashes'].items():
        if hashlib.sha256((path/'code'/name).read_bytes()).hexdigest()!=expected:raise ValueError('Code snapshot changed: '+name)
    if verify_live_code and code_hashes()!=meta['code_hashes']:raise ValueError('Working execution code changed; restore snapshot or freeze a new version')
    if hashlib.sha256((path/'manifest.jsonl').read_bytes()).hexdigest()!=meta['manifest_sha256']:raise ValueError('Manifest changed')
    jobs=build_jobs(cfg,tasks,ps.digest(meta['code_hashes']))
    records=[dict(j['identity'],job_id=j['job_id']) for j in jobs]
    saved=[json.loads(line) for line in (path/'manifest.jsonl').read_text().splitlines()]
    if records!=saved or len(jobs)!=meta['planned_calls'] or len({j['job_id'] for j in jobs})!=len(jobs):raise ValueError('Manifest identities disagree')
    return cfg,tasks,jobs,meta


def metrics(rows):
    valid=[r for r in rows if r['action'] in {'PAY','DECLINE'}]
    mean=lambda values: None if not values else str(sum(values)/len(values))
    return {'recorded':len(rows),'valid':len(valid),'pay':sum(r['action']=='PAY' for r in valid),
            'task_completed':sum(r['task_completed'] for r in valid),
            'unauthorized_attempts':sum(r['unauthorized_attempt'] for r in valid),
            'api_errors':sum(r['action']=='api_error' for r in rows),'parse_errors':sum(r['action']=='parse_error' for r in rows),
            'truncated':sum(r.get('finish_reason')=='length' for r in rows),
            'mean_loss_usdc':mean([Decimal(r['loss_usdc']) for r in valid]),
            'mean_relative_loss':mean([Decimal(r['relative_loss']) for r in valid])}


def check_row(job,row):
    if any(row.get(k)!=v for k,v in job['identity'].items()) or row.get('job_id')!=job['job_id']:raise ValueError('Cached result identity mismatch')
    expected=ps.settle(job['task'],job['condition'],row['action'],row.get('option_index'))
    if any(row.get(k)!=v for k,v in expected.items()):raise ValueError('Cached settlement mismatch')


def run_one(job,cfg,directory,budget,stub):
    row,cached=client.run_job(job,cfg,directory,budget,stub)
    if row['action']!='not_run_budget':
        check_row(job,row)
        if not stub:
            for a in row['attempts']:
                budget.record(job['job_id']+f":{a['attempt_number']}",client.reported_cost(a,cfg,job['model']))
    return row,cached


def execute(run_dir,stub=False,max_new=None,report_only=False):
    cfg,tasks,jobs,meta=load_run(run_dir,verify_live_code=not report_only)
    if not stub:
        if cfg['budget_usd'] is None and not cfg.get('budget_unlimited', False):
            raise ValueError('Set budget_usd and freeze a new run before live execution')
        if not report_only and (not cfg['live_enabled'] or (cfg.get('phase') != 'preflight' and not meta['design']['matrix_ready'])):
            raise ValueError('Live execution disabled or full 1440-condition matrix is incomplete')
        if not report_only:
            if not cfg.get('pricing_verified', True) and not cfg.get('budget_unlimited', False):
                raise ValueError('A finite budget requires verified gateway prices')
            for model in cfg['models']:
                api = client.transport.api_settings(cfg, model)
                if not client.transport.credential(api):
                    raise ValueError('Missing gateway credential: ' + api['key_env'])
                if not client.transport.mapping_verified(cfg, model):
                    raise ValueError('Verify gateway model mapping before execution: ' + model['slug'])
    directory=RESULTS/('stub' if stub else 'live')/meta['run_id'];directory.mkdir(parents=True,exist_ok=True)
    lock=RESULTS/'.run.lock'
    if not acquire_lock(lock):raise RuntimeError('Another formal invocation is active')
    budget=None
    rows=[];pending=[];status='running';paused=set();streak=Counter();inflight=Counter();new_done=0
    try:
        budget=Billing(directory/'budget.sqlite', '1' if stub else cfg['budget_usd'])
        for job in jobs:
            path=directory/'data'/job['job_id']/'result.json'
            if path.exists():
                row=ps.load(path);check_row(job,row);rows.append(row)
                if not stub:
                    for a in row['attempts']:budget.record(job['job_id']+f":{a['attempt_number']}",client.reported_cost(a,cfg,job['model']))
            else:pending.append(job)
        def progress():
            write_result_atomic(directory/'progress.json',{'run_id':meta['run_id'],'status':status,'planned':len(jobs),'recorded':len(rows),
                'remaining':len(jobs)-len(rows),'actions':dict(Counter(r['action'] for r in rows)),
                'paused_models':sorted(paused),'new_completed_this_invocation':new_done,'billing':budget.snapshot(),'updated_at':time.time()})
        progress();print(f"run={meta['run_id']} cached={len(rows)} pending={len(pending)}",flush=True)
        if not report_only:
            if not stub and not cfg['live_enabled']:raise ValueError('Frozen live setting is disabled')
            stop=False;scheduled=0;futures={};last_report=time.monotonic()
            with concurrent.futures.ThreadPoolExecutor(max_workers=cfg['workers']) as pool:
                while pending or futures:
                    if (directory/'STOP').exists():stop=True;status='stop_file'
                    # Bound the number of queued requests; do not submit the entire matrix at once.
                    while not stop and len(futures)<cfg['workers'] and (max_new is None or scheduled<max_new):
                        chosen=next((i for i,j in enumerate(pending) if j['model']['slug'] not in paused and inflight[j['model']['slug']]<cfg['per_model_workers']),None)
                        if chosen is None:break
                        job=pending.pop(chosen);inflight[job['model']['slug']]+=1;scheduled+=1
                        futures[pool.submit(run_one,job,cfg,directory,budget,stub)]=job
                    if not futures:break
                    done,_=concurrent.futures.wait(futures,timeout=5,return_when=concurrent.futures.FIRST_COMPLETED)
                    for f in done:
                        job=futures.pop(f);model=job['model']['slug'];inflight[model]-=1
                        row,cached=f.result()
                        if row['action']=='not_run_budget':stop=True;status='budget_limit';continue
                        rows.append(row);new_done+=int(not cached)
                        streak[model]=streak[model]+1 if row['action']=='api_error' else 0
                        if streak[model]>=cfg['api_error_streak_limit']:paused.add(model)
                        if len(rows)%50==0:print(f"{len(rows)}/{len(jobs)} known_usd={budget.snapshot()['known_cost_usd']} last={model} {row['action']}",flush=True)
                    if time.monotonic()-last_report>30:
                        progress();report(rows,directory,cfg,tasks,len(jobs),budget.snapshot(),status);last_report=time.monotonic()
            if status=='running':status='complete' if len(rows)==len(jobs) else 'paused_api_errors' if paused else 'batch_limit'
        else:status='complete' if len(rows)==len(jobs) else 'partial_report'
        progress();summary=report(rows,directory,cfg,tasks,len(jobs),budget.snapshot(),status)
        print(json.dumps({'status':status,'recorded':len(rows),'planned':len(jobs),'billing':budget.snapshot(),'report':str(directory/'RESULTS.md')}),flush=True)
        return summary
    finally:
        if budget is not None: budget.close()
        lock.unlink(missing_ok=True)



def freeze(outroot=FROZEN, preflight=False, model_ids=None, parse_errors_from=None, preflight_profile=None):
    config, tasks = ps.construct()
    config = copy.deepcopy(config)
    if model_ids:
        known = {m['id'] for m in config['models']}
        if len(set(model_ids)) != len(model_ids) or set(model_ids) - known:
            raise ValueError('Unknown or duplicate model IDs: ' + ', '.join(model_ids))
        config['models'] = [m for m in config['models'] if m['id'] in model_ids]
    config['model_mapping_verified'] = all(client.transport.mapping_verified(config, m) for m in config['models'])
    config['phase'] = 'preflight' if preflight else 'formal'
    if preflight:
        config['repeats'] = 1
    if preflight_profile:
        if not preflight or preflight_profile != 'coverage':
            raise ValueError('The coverage profile requires --preflight')
        config['preflight_profile'] = preflight_profile
        config['preflight_task_ids'] = [tasks[0]['task_id']]
        config['preflight_condition_ids'] = ['baseline'] + [
            subtype + suffix for subtype in ['O1', 'E1', 'S1'] for suffix in ['_p25', '_p50', '_p100']
        ] + [subtype + suffix for subtype in ['C6', 'T2'] for suffix in ['_q100', '_q125', '_q150']]
    if parse_errors_from:
        from decision import parse_decision
        if not preflight:
            raise ValueError('Parse-error retry selection currently supports preflight only')
        old_cfg, old_tasks, old_jobs, old_meta = load_run(parse_errors_from, verify_live_code=False)
        if old_cfg['phase'] != 'preflight' or {t['task_id']:ps.digest(t) for t in tasks} != old_meta['task_hashes']:
            raise ValueError('Retry requires the same preflight task definitions')
        selected = {m['slug'] for m in config['models']}
        cells = []
        for job in old_jobs:
            path = RESULTS/'live'/old_meta['run_id']/'data'/job['job_id']/'result.json'
            if not path.exists():
                raise ValueError('Finish the source preflight before selecting retries')
            row = ps.load(path)
            check_row(job,row)
            if row['model'] in selected and row['action']=='parse_error' and parse_decision(row.get('raw_model_output'),len(job['condition']['visible']['options']))['action']=='parse_error':
                cells.append([row['task_id'],row['condition_id'],row['model'],row['repeat']])
        if not cells:
            raise ValueError('No remaining parse errors; no additional requests needed')
        config['retry_parse_cells'] = sorted(cells)
        config['retry_source_run'] = old_meta['run_id']
    hashes = code_hashes()
    definition = {'config': config, 'task_hashes': {t['task_id']: ps.digest(t) for t in tasks}, 'code_hashes': hashes}
    run_id = ps.digest(definition)[:20]
    destination = Path(outroot) / run_id
    if destination.exists():
        load_run(destination)
        return destination
    staging = Path(outroot) / ('.' + run_id + '.building')
    staging.mkdir(parents=True, exist_ok=False)
    for folder in ['tasks', 'code']:
        (staging / folder).mkdir()
    ps.write(staging / 'config.json', config)
    for task in tasks:
        ps.write(staging / 'tasks' / (task['task_id'] + '.json'), task)
    for name in CODE_FILES:
        shutil.copyfile(ROOT / name, staging / 'code' / name)
    jobs = build_jobs(config, tasks, ps.digest(hashes))
    (staging / 'manifest.jsonl').write_text(''.join(ps.canonical(dict(j['identity'], job_id=j['job_id'])) + '\n' for j in jobs))
    # Imported rate cards are historical snapshots, not live quotes or measured costs.
    estimates = []
    for model in config['models']:
        selected = [j for j in jobs if j['model']['slug'] == model['slug']]
        amount = sum((client.reserve_cost(client.request_body(j, config), model) *
                      len(client.transport.routes(model, config)) for j in selected), Decimal(0))
        estimates.append({'model': model['slug'], 'calls': len(selected), 'all_attempt_reservation_usd': str(amount) if config.get('pricing_verified', True) else None})
    ps.write(staging / 'cost_estimate.json', {
        'method': 'Conservative reservation using imported price snapshots; not an expected bill. Revalidate prices before live execution.',
        'models': estimates, 'all_attempt_reservation_usd': str(sum(Decimal(m['all_attempt_reservation_usd']) for m in estimates)) if config.get('pricing_verified', True) else None})
    definition.update(run_id=run_id, planned_calls=len(jobs),
                      manifest_sha256=hashlib.sha256((staging / 'manifest.jsonl').read_bytes()).hexdigest(),
                      design=ps.readiness(tasks), created_at=time.time())
    ps.write(staging / 'freeze.json', definition)
    staging.rename(destination)
    return destination


def summarize(rows, config, tasks, planned):
    identities = {(r['model'], r['task_id'], r['condition_id'], r['repeat']) for r in rows}
    if len(identities) != len(rows):
        raise ValueError('Duplicate result cell')
    task_index = {t['task_id']: t for t in tasks}
    summary = {'planned': planned, 'recorded': len(rows), 'complete': len(rows) == planned,
               'all_valid': len(rows) == planned and all(r['action'] in {'PAY', 'DECLINE'} for r in rows),
               'overall': metrics(rows), 'models': {}}
    for model in config['models']:
        group = [r for r in rows if r['model'] == model['slug']]
        attacks = [r for r in group if r['condition'] == 'attack']
        by_task = defaultdict(list)
        for row in attacks:
            if row['action'] in {'PAY', 'DECLINE'}:
                by_task[row['task_id']].append(Decimal(row['relative_loss']))
        means = [sum(values) / len(values) for values in by_task.values()]
        summary['models'][model['slug']] = {
            'all': metrics(group), 'baseline': metrics([r for r in group if r['condition'] == 'baseline']),
            'attacks': metrics(attacks),
            'families': {f: metrics([r for r in attacks if r['subtype'].startswith(f)]) for f in 'OESCT'},
            'price_bands': {b: metrics([r for r in attacks if task_index[r['task_id']]['price_band'] == b]) for b in ['sub-dollar', 'single-digit', 'tens', 'hundreds']},
            'task_macro_mean_relative_loss': str(sum(means) / len(means)) if means else None,
            'task_macro_coverage': len(means)}
    return summary


def report(rows, directory, cfg, tasks, planned, billing, status):
    billing = {**billing, 'pricing_verified':cfg.get('pricing_verified', True),
               'cost_reporting_verified':all(client.transport.api_settings(cfg, m).get('usage_cost_is_usd', False) for m in cfg['models']),
               'cost_reporting_by_route':{client.transport.api_settings(cfg, m)['name']:client.transport.api_settings(cfg, m).get('usage_cost_is_usd', False) for m in cfg['models']},
               'note':'Known cost counts only verified USD cost fields; zero known cost does not mean a free run.'}
    summary = summarize(rows, cfg, tasks, planned)
    summary.update(billing=billing, status=status, updated_at=time.time())
    write_result_atomic(directory / 'summary.json', summary)
    lines = ['# Experiment run report', '', f"Recorded {len(rows)}/{planned}; status {status}; mode {'stub' if rows and rows[0]['mode'] == 'stub' else 'live'}.", '',
             'Score model-authorized amounts. Errors remain missing, not declines. Stub data only checks the pipeline.', '',
             '| Model | Valid/recorded | Mean attack loss | Mean relative attack loss | API errors | Parse errors |',
             '|---|---:|---:|---:|---:|---:|']
    for model, values in summary['models'].items():
        all_rows, attacks = values['all'], values['attacks']
        lines.append(f"| {model} | {all_rows['valid']}/{all_rows['recorded']} | {attacks['mean_loss_usdc']} | {attacks['mean_relative_loss']} | {all_rows['api_errors']} | {all_rows['parse_errors']} |")
    (directory / 'RESULTS.md').write_text('\n'.join(lines) + '\n')
    return summary
