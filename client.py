"""Single-request execution shared by offline checks and formal runs."""
import time
import json
from decimal import Decimal
import builder as ps
import transport
from decision import parse_decision
from storage import write_result_atomic

def request_body(job,cfg):
    m=job['model']
    api = transport.api_settings(cfg, m)
    body={'model':m.get('api_model') or m['slug'],'max_tokens':m['max_output_tokens'],'messages':[
        {'role':'system','content':cfg['system_prompt']},
        {'role':'user','content':ps.build_prompt(job['task'],job['condition'])+cfg['protocol']}]}
    if m['temperature'] is not None:body['temperature']=m['temperature']
    if api['kind'] == 'openrouter' and m['reasoning'] is not None:body['reasoning']=m['reasoning']
    # Gateway-specific reasoning parameters must be explicitly verified, not copied from OpenRouter.
    body.update(m.get('api_parameters', {}))
    return body


def reported_cost(record, cfg, model=None):
    if transport.api_settings(cfg, model).get('usage_cost_is_usd', False):
        return (record.get('usage') or {}).get('cost')
    return None


def reserve_cost(body,model):
    # Conservative byte-based token bound + chat overhead, maximum listed tier, and margin.
    price=model['pricing'];tiers=[price]+price.get('overrides',[])
    input_rate=max(Decimal(str(p.get('prompt',0))) for p in tiers)
    output_rate=max(Decimal(str(p.get('completion',0))) for p in tiers)
    request_fee=Decimal(str(price.get('request',0)))
    nbytes=len(json.dumps(body,ensure_ascii=False).encode())+512
    return (Decimal(nbytes)*input_rate+Decimal(model['max_output_tokens'])*output_rate+request_fee)*2


class BudgetStop(RuntimeError): pass


def run_job(job,cfg,directory,budget,stub=False):
    api = transport.api_settings(cfg, job['model'])
    folder=directory/'data'/job['job_id'];path=folder/'result.json'
    if path.exists():return ps.load(path),True
    folder.mkdir(parents=True,exist_ok=True)
    body=request_body(job,cfg);write_result_atomic(folder/'request.json',body)
    prior=[]
    if not stub:
        for i in range(1,len(transport.routes(job['model'],cfg))+1):
            result=folder/f'attempt_{i}.json';started=folder/f'attempt_{i}.started.json'
            if result.exists():prior.append(ps.load(result))
            elif started.exists() or job['job_id']+f':{i}' in budget.data['reservations']:
                record={'attempt_number':i,'error':'interrupted_remote_outcome_unknown','retryable':False,'response_received':False}
                write_result_atomic(result,record);prior.append(record);break
            else:break
    def start(i,provider):
        reserve = reserve_cost(body,job['model']) if cfg.get('pricing_verified', True) else Decimal(0)
        key=job['job_id']+f':{i}';budget.reserve(key,reserve)
        write_result_atomic(folder/f'attempt_{i}.started.json',{'request_hash':ps.digest(body),'requested_provider':provider,'time':time.time()})
    def finish(record):
        write_result_atomic(folder/f"attempt_{record['attempt_number']}.json",record)
        budget.record(job['job_id']+f":{record['attempt_number']}",reported_cost(record,cfg,job['model']))
    try:
        if stub:
            response={'text':'Reason: offline fixture.\nACTION: PAY\nOPTION: 1','provider':'stub','returned_model':job['model']['slug'],'error':None,'finish_reason':'stop'};attempts=[]
        else:response,attempts=transport.call(body,job['model'],cfg,finish,prior,start)
    except BudgetStop:
        return {'job_id':job['job_id'],**job['identity'],'action':'not_run_budget'},False
    decision={'action':'api_error','option_index':None,'error_detail':response.get('error')}
    if not response.get('error'):
        returned=response.get('returned_model')
        accepted = job['model'].get('api_accepted_returned_models') or job['model']['accepted_returned_models']
        if stub: accepted = [job['model']['slug']]
        if not returned or returned not in accepted:
            decision['error_detail']='missing_or_unexpected_returned_model:'+str(returned)
        elif not stub and api['kind'] == 'openrouter' and response.get('provider')!=job['model']['provider_name']:
            decision['error_detail']='unexpected_returned_provider:'+str(response.get('provider'))
        else:decision=parse_decision(response.get('text'),len(job['condition']['visible']['options']))
    row={**job['identity'],'job_id':job['job_id'],'condition':job['condition']['condition'],
         'subtype':job['condition'].get('subtype'),'price_multiplier':job['condition'].get('price_multiplier'),
         'price_rule':job['condition'].get('price_rule'), 'price_condition_index':job['condition'].get('price_condition_index'),
         'quote_scaling_factor':job['condition'].get('quote_scaling_factor'),
         'option_price_ratios':job['condition'].get('option_price_ratios'),
         'markup_ratio':job['condition'].get('markup_ratio'),'model_config':job['model'],
         'request_hash':ps.digest(body),'raw_model_output':response.get('text'),'returned_model':response.get('returned_model'),
         'provider':response.get('provider'), 'gateway':api['name'],
         'api_base_url':api['base_url'], 'requested_model':body['model'],
         'credential_env':api['key_env'],
         'provider_identity_verified':not stub and api['kind'] == 'openrouter' and not decision.get('error_detail'),
         'attempts':attempts,'finish_reason':response.get('finish_reason'),
         'usage':response.get('usage'),'mode':'stub' if stub else 'live','timestamp':time.time(),**decision,
         **ps.settle(job['task'],job['condition'],decision['action'],decision.get('option_index'))}
    write_result_atomic(path,row)
    return row,False
