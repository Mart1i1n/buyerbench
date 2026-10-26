"""Offline descriptive analysis of final adopted decisions; never calls a model."""
import hashlib
import gzip
import html
import io
import json
import os
from collections import Counter
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd

from core import settle

ROOT = Path(__file__).resolve().parent
OUT = ROOT / 'results' / 'analysis'
MODELS = {
    'openai/gpt-5.6-sol': ('M01', 'GPT Sol'),
    'openai/gpt-5.6-terra': ('M02', 'GPT Terra'),
    'anthropic/claude-sonnet-5': ('M03', 'Claude Sonnet'),
    'google/gemini-3.8-flash': ('M04', 'Gemini Flash'),
    'deepseek/deepseek-v4.1-flash': ('M05', 'DeepSeek Flash'),
    'deepseek/deepseek-v4-pro': ('M06', 'DeepSeek Pro'),
    'qwen/qwen3-14b': ('M07', 'Qwen 14B'),
    'qwen/qwen3-32b': ('M08', 'Qwen 32B'),
    'meta-llama/llama-3.1-8b-instruct': ('M09', 'Llama 8B'),
    'anthropic/claude-opus-5': ('M10', 'Claude Opus'),
}


def key(r, clean=False):
    if clean:
        suffix = ('baseline' if not r['is_attack'] else r['attack_type'] +
                  ('_q' + str(round(float(r['price_factor']) * 100))
                   if r['price_basis'] == 'original_offer'
                   else '_p' + str(round((float(r['price_factor']) - 1) * 100))))
    else:
        suffix = r['condition_id']
    return r['model'], r['task_id'], suffix, r['repeat']


def records(frame):
    return json.loads(frame.to_json(orient='records', force_ascii=False, double_precision=15))


def metrics(g):
    pay = g[g.pay]
    return {
        'n': len(g), 'pay_n': int(g.pay.sum()), 'pay_pct': 100 * g.pay.mean(),
        'positive_loss_n': int(g.positive.sum()), 'positive_loss_pct': 100 * g.positive.mean(),
        'zero_loss_pay_n': int((g.pay & (g.relative_loss == 0)).sum()),
        'negative_loss_n': int((g.relative_loss < 0).sum()),
        'mean_relative_loss': g.relative_loss.mean(),
        'mean_relative_loss_pct': 100 * g.relative_loss.mean(),
        'relative_loss_sum': g.relative_loss.sum(),
        'loss_given_pay': pay.relative_loss.mean() if len(pay) else None,
        'mean_loss_usdc': g.loss_usdc.mean(), 'loss_usdc_sum': g.loss_usdc.sum(),
        'loss_ge_reference_n': int((g.relative_loss >= 1).sum()),
        'max_relative_loss': g.relative_loss.max(),
        'task_completed_pct': 100 * g.task_completed.mean(),
        'unauthorized_n': int(g.unauthorized.sum()),
    }


def aggregate(frame, columns):
    rows = []
    for k, g in frame.groupby(columns, dropna=False, sort=True):
        if len(columns) == 1:
            k = (k,) if not isinstance(k, tuple) else k
        rows.append(dict(zip(columns, k), **metrics(g)))
    return pd.DataFrame(rows)


def render_report(result, tables):
    os.environ.setdefault('MPLCONFIGDIR', '/tmp/x402-analysis-matplotlib')
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    plt.rcParams.update({'font.family': 'DejaVu Sans', 'font.size': 10, 'svg.fonttype': 'none'})

    def svg(fig):
        buf = io.StringIO()
        fig.savefig(buf, format='svg', bbox_inches='tight')
        plt.close(fig)
        return buf.getvalue()[buf.getvalue().index('<svg'):]

    names = {
        'model_id': 'Model id', 'model': 'Model', 'task_id': 'Task id', 'product': 'Product', 'band': 'Band',
        'reference_usdc': 'Reference usdc', 'category': 'Category', 'subtype': 'Subtype', 'subtype_name': 'Subtype name',
        'rule': 'Rule', 'level': 'Level', 'factor': 'Factor', 'repeat': 'Repeat',
        'n': 'N', 'pay_n': 'Pay n', 'pay_pct': 'Pay pct', 'positive_loss_n': 'Positive loss n',
        'positive_loss_pct': 'Positive loss pct', 'zero_loss_pay_n': 'Zero loss pay n',
        'mean_relative_loss_pct': 'Mean relative loss pct', 'mean_relative_loss': 'Mean relative loss',
        'loss_given_pay': 'Loss given pay', 'relative_loss_share_pct': 'Relative loss share pct',
        'mean_loss_usdc': 'Mean loss usdc', 'loss_usdc_sum': 'Loss usdc sum',
        'relative_loss_sum': 'Relative loss sum', 'loss_ge_reference_n': 'Loss ge reference n',
        'max_relative_loss': 'Max relative loss', 'task_completed_pct': 'Task completed pct',
        'unauthorized_n': 'Unauthorized n', 'conditions': 'Conditions',
        'mixed_decision_n': 'Mixed decision n', 'mixed_decision_pct': 'Mixed decision pct',
        'mixed_choice_pct': 'Mixed choice pct', 'pay_count': 'Pay count',
        'option_id': 'Option id', 'option_position': 'Option position',
        'matched_conditions': 'Matched conditions', 'increased_pay_n': 'Increased pay n',
        'decreased_pay_n': 'Decreased pay n', 'nonmonotone_n': 'Nonmonotone n',
        'mean_pay_change_pp': 'Mean pay change pp', 'mean_loss_change': 'Mean loss change',
        'has_fair_option': 'Has fair option', 'execution_status': 'Execution status',
        'paid_usdc': 'Paid usdc', 'value_usdc': 'Value usdc', 'relative_loss': 'Relative loss',
    }
    main_cols = ['n', 'pay_pct', 'positive_loss_pct', 'mean_relative_loss_pct', 'loss_given_pay']

    def table(name, columns, title=None, frame=None):
        data = (tables[name] if frame is None else frame)[columns].rename(columns=names)
        markup = data.to_html(index=False, border=0, escape=True, na_rep='—', float_format=lambda x: f'{x:.2f}')
        return '<div class="tablebox"><div class="tablebar"><strong>' + html.escape(title or name) + '</strong><input placeholder="Filter this table..." oninput="filterTable(this)"></div><div class="scroll">' + markup + '</div></div>'

    def detail(label, body):
        return '<details><summary>' + label + '</summary>' + body + '</details>'

    fig, ax = plt.subplots(figsize=(8.4, 4.5))
    m = tables['models']
    ax.scatter(m.pay_pct, m.mean_relative_loss_pct, s=65, c='#216c84')
    for _, row in m.iterrows():
        offset = {'M03': (6, 8), 'M10': (-25, -14), 'M04': (7, -12), 'M05': (7, -12), 'M06': (-36, 8), 'M01': (6, 7), 'M02': (-32, -13)}.get(row.model_id, (6, 4))
        ax.annotate(row.model_id, (row.pay_pct, row.mean_relative_loss_pct), xytext=offset, textcoords='offset points', fontsize=9)
    ax.set(xlabel='Payment rate (%)', ylabel='Mean relative loss (%)', title='Model comparison · manipulation conditions', xlim=(0, 105), ylim=(0, 135))
    ax.grid(alpha=.2)
    scatter = svg(fig)
    fig, ax = plt.subplots(figsize=(8.4, 4.8))
    heat = tables['model_category'].pivot(index='model_id', columns='category', values='pay_pct').reindex(columns=list('OESCT'))
    im = ax.imshow(heat, cmap='YlGnBu', vmin=0, vmax=100, aspect='auto')
    ax.set_xticks(range(5), heat.columns)
    ax.set_yticks(range(10), [f'{mid}  {m.set_index("model_id").loc[mid,"model"]}' for mid in heat.index])
    for i in range(10):
        for j in range(5):
            value = heat.iloc[i, j]
            ax.text(j, i, f'{value:.1f}', ha='center', va='center', color='white' if value > 65 else '#18333e', fontsize=9)
    ax.set_title('Payment rate by model and category (%) · n=864 per cell')
    fig.colorbar(im, ax=ax, fraction=.025, pad=.03)
    heatmap = svg(fig)
    fig, axes = plt.subplots(1, 2, figsize=(10, 3.7))
    for rule, color in [('A', '#216c84'), ('B', '#c06a34')]:
        g = tables['rule_price'].query('rule == @rule')
        axes[0].plot(g.level, g.pay_pct, 'o-', color=color, label=f'Rule {rule}')
        axes[1].plot(g.level, g.mean_relative_loss_pct, 'o-', color=color, label=f'Rule {rule}')
    for ax, ylabel in zip(axes, ['Payment rate (%)', 'Mean relative loss (%)']):
        ax.set(xlabel='Price level (within each rule)', ylabel=ylabel, xticks=[1, 2, 3])
        ax.grid(alpha=.2); ax.legend()
    prices = svg(fig)
    fig, ax = plt.subplots(figsize=(9, 3.8))
    x = np.arange(len(m)); width = .38
    without = tables['no_T2_models'].set_index('model_id').loc[m.model_id]
    ax.bar(x - width / 2, m.mean_relative_loss_pct, width, label='All 40 subtypes', color='#216c84')
    ax.bar(x + width / 2, without.mean_relative_loss_pct, width, label='39 subtypes, T2 excluded (sensitivity)', color='#c9a667')
    ax.set(xticks=x, xticklabels=m.model_id, ylabel='Mean relative loss (%)', title='How much does T2 change each model’s mean?')
    ax.legend(fontsize=8); ax.grid(axis='y', alpha=.2)
    tail = svg(fig)

    overall = result['overall_attack']
    decomp = result['loss_decomposition']
    t2 = tables['subtypes'].query("subtype == 'T2'").iloc[0]
    zero_pay_pct = 100 * decomp['zero_loss_pay_n'] / overall['pay_n']
    fair_success = 100 * decomp['fair_option_selected_n'] / decomp['fair_option_available_n']
    band_order = ['sub-dollar', 'single-digit', 'tens', 'hundreds']
    bands = tables['bands'].set_index('band').loc[band_order].reset_index()
    choices = tables['choices'].groupby(['subtype', 'level', 'option_id'], as_index=False)['n'].sum()
    total_choices = tables['choices'].groupby(['subtype', 'option_id'], as_index=False)['n'].sum()
    for frame in (choices, total_choices):
        group = ['subtype', 'level'] if 'level' in frame else ['subtype']
        frame['choice_pct'] = frame['n'] / frame.groupby(group)['n'].transform('sum') * 100
    names['choice_pct'] = 'Share of all subtype decisions (%)'
    body = ['<header><h1>x402 experimental results</h1><p>Final adopted decisions, including recovery and offline format repairs. These are controlled decision-based scores, not observed on-chain losses.</p></header>']
    body.append('<section><h2>Metrics</h2><p>Payment rate measures authorization. Positive-loss rate measures harmful decisions. Mean relative loss includes declines as zero and divides loss by each task reference price. Mean relative loss equals payment rate times mean relative loss conditional on payment.</p></section>')
    body.append('<section><h2>Model comparison</h2>' + scatter + heatmap + tail + '</section>')
    body.append('<section><h2>Price conditions</h2><p>Rule A quotes 1.25/1.50/2.00 times reference value. Rule B scales the original attack offer by 1.00/1.25/1.50. The rules cover different subtypes and are not interchangeable markup levels.</p>' + prices + '</section>')
    for name, frame in tables.items():
        body.append('<section><h2>' + html.escape(name.replace('_', ' ').title()) + '</h2>' + table(name, list(frame.columns)) + '</section>')
    body.append('<section><h2>Selected cases</h2><p>Model explanations are observations, not evidence of internal causal mechanisms.</p>')
    for c in result['cases']:
        heading = f"{c['model']} / task {c['task_id']} / {c['condition_id']} / repeat {c['repeat']}"
        body.append('<article><h3>' + html.escape(heading) + '</h3><p>' + html.escape(c['case_kind']) + '</p><pre>' + html.escape(c['raw_model_output']) + '</pre></article>')
    body.append('</section><section><h2>Limitations</h2><ul>' + ''.join('<li>' + html.escape(x) + '</li>' for x in result['limitations']) + '</ul></section>')
    body.append('<footer>Scores were rechecked against task outcomes. Source file hashes and complete tables are in analysis_summary.json. Release metadata has been sanitized; model outputs and decisions are unchanged.</footer>')
    css = '''body{margin:0;background:#f3f5f7;color:#18313e;font:15px/1.7 system-ui,-apple-system,sans-serif}header,section,footer{max-width:1120px;margin:auto;padding:30px}header{padding-top:45px}h1{font-size:34px;line-height:1.3}h2{font-size:25px;margin-top:0}h3{margin-top:30px}.eyebrow{font-size:12px;letter-spacing:2px;color:#527081}.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:16px}.cards div{background:white;border:1px solid #dbe3e8;border-radius:10px;padding:20px}.cards b{display:block;font-size:30px;color:#216c84}nav{position:sticky;top:0;background:#18313e;z-index:5;display:flex;justify-content:center;flex-wrap:wrap;padding:10px;gap:16px}nav a{color:white;text-decoration:none;font-size:13px}section{background:white;border-bottom:10px solid #f3f5f7;scroll-margin-top:65px}p{max-width:1000px}svg{width:100%;height:auto;max-height:650px;margin:16px 0}table{border-collapse:collapse;width:100%;font-size:13px;white-space:nowrap}th,td{padding:9px 11px;text-align:right;border-bottom:1px solid #e4ebef}th{background:#edf3f6;cursor:pointer}th:first-child,td:first-child{text-align:left}tbody tr:hover{background:#f0f8fa}.scroll{overflow:auto;max-height:560px}.tablebox{border:1px solid #dbe3e8;border-radius:8px;margin:20px 0;overflow:hidden}.tablebar{display:flex;justify-content:space-between;gap:12px;align-items:center;padding:12px;background:#fafcfd}input{border:1px solid #bacbd4;border-radius:5px;padding:7px;width:230px}details{margin:16px 0;border:1px solid #dbe3e8;border-radius:8px;padding:12px}summary{cursor:pointer;font-weight:600}.case{border-top:1px solid #dbe3e8;padding:10px 0}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f3f6f8;padding:15px;font-size:13px}footer{font-size:12px;color:#607582}@media(max-width:700px){.cards{grid-template-columns:repeat(2,1fr)}header,section,footer{padding:20px}h1{font-size:26px}.tablebar{display:block}input{margin-top:6px}}'''
    js = '''function filterTable(input){let q=input.value.toLowerCase();input.closest('.tablebox').querySelectorAll('tbody tr').forEach(r=>r.hidden=!r.textContent.toLowerCase().includes(q));}document.querySelectorAll('table').forEach(t=>{t.querySelectorAll('th').forEach((h,i)=>{h.onclick=()=>{let dir=h.dataset.dir==='asc'?-1:1;h.dataset.dir=dir===1?'asc':'desc';let b=t.tBodies[0];[...b.rows].sort((a,c)=>{let x=a.cells[i].textContent,y=c.cells[i].textContent;let nx=Number(x),ny=Number(y);return dir*(x.trim()!==''&&y.trim()!==''&&Number.isFinite(nx)&&Number.isFinite(ny)?nx-ny:new Intl.Collator('en',{numeric:true}).compare(x,y))}).forEach(r=>b.appendChild(r));}})});'''
    document = '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>x402 experimental results</title><style>' + css + '</style></head><body>' + ''.join(body) + '<script>' + js + '</script></body></html>'
    (OUT / 'analysis_report.html').write_text(document)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    catalog = {t['id']: t for t in json.loads((ROOT / 'data/tasks.json').read_text())['tasks']}
    tasks = {p.stem: json.loads(p.read_text()) for p in (ROOT / 'generated/tasks').glob('*.json')}
    conditions = {tid: {c['id']: c for c in t['conditions']} for tid, t in tasks.items()}
    recipes = {r['subtype']: r for r in json.loads((ROOT / 'data/attack_recipes.json').read_text())}
    clean_path = ROOT / 'results/final_results_clean.jsonl'
    full_path = ROOT / 'results/all_final_results.jsonl'
    clean = {}
    ch = hashlib.sha256()
    with clean_path.open('rb') as f:
        for line in f:
            ch.update(line)
            r = json.loads(line, parse_float=Decimal)
            k = key(r, True)
            assert k not in clean
            clean[k] = r
    rows, seen, cases = [], set(), []
    fh = hashlib.sha256()
    full_stream = full_path.open('rb') if full_path.exists() else gzip.open(str(full_path) + '.gz', 'rb')
    with full_stream as f:
        for line in f:
            fh.update(line)
            r = json.loads(line)
            k = key(r)
            assert k not in seen
            seen.add(k)
            c = clean[k]
            assert r['action'] == c['decision'] and Decimal(r['relative_loss']) == Decimal(c['relative_loss'])
            task, cond = tasks[r['task_id']], conditions[r['task_id']][r['condition_id']]
            assert r['condition'] == ('attack' if c['is_attack'] else 'baseline')
            assert c['product'] == task['title'] and c['task_price_band'] == task['price_band']
            assert c['attack_type'] == cond['subtype'] and c['price_level'] == cond.get('price_condition_index')
            expected_factor = cond.get('quote_scaling_factor') or cond.get('price_multiplier') or '1'
            assert Decimal(c['price_factor']) == Decimal(expected_factor)
            score = settle(task, cond, r['action'], r.get('option_index'))
            for field, value in score.items():
                if field in {'paid_usdc', 'value_usdc', 'loss_usdc', 'relative_loss'}:
                    assert Decimal(r[field]) == Decimal(value), (k, field)
                else:
                    assert r[field] == value, (k, field)
            paid = r['action'] == 'PAY'
            idx = r['option_index'] if paid else None
            eligible = [i for i, outcome in enumerate(cond['outcomes'])
                        if outcome['task_completed'] and outcome['authorization_matches'] and
                        Decimal(settle(task, cond, 'PAY', i)['relative_loss']) <= 0]
            option = cond['visible']['options'][idx] if paid else None
            row = {
                'model_id': MODELS[r['model']][0], 'model': MODELS[r['model']][1],
                'task_id': r['task_id'], 'product': c['product'], 'band': c['task_price_band'],
                'reference_usdc': float(catalog[r['task_id']]['review_price']),
                'condition_id': r['condition_id'], 'attack': c['is_attack'],
                'subtype': r['subtype'] or 'baseline', 'category': r['subtype'][0] if r['subtype'] else 'baseline',
                'subtype_name': recipes[r['subtype']]['title'] if r['subtype'] else 'Normal baseline',
                'operation': recipes[r['subtype']]['operation'] if r['subtype'] else 'baseline',
                'rule': r['price_rule'] or 'baseline', 'level': r['price_condition_index'] or 0,
                'factor': float(c['price_factor']), 'repeat': r['repeat'], 'pay': paid,
                'relative_loss': float(r['relative_loss']), 'loss_usdc': float(r['loss_usdc']),
                'paid_usdc': float(r['paid_usdc']), 'value_usdc': float(r['value_usdc']),
                'positive': Decimal(r['relative_loss']) > 0,
                'task_completed': r['task_completed'], 'unauthorized': r['unauthorized_attempt'],
                'execution_status': r['execution_status'],
                'option_id': option['option_id'] if paid else 'DECLINE',
                'option_position': idx + 1 if paid else 0,
                'has_fair_option': bool(eligible), 'selected_fair_option': paid and idx in eligible,
                'source': r['consolidation']['ledger_entry']['source'],
                'gateway': r['gateway'], 'provider': r['provider'] or 'not_reported',
            }
            rows.append(row)
            case_kind = None
            if r['model'] == 'qwen/qwen3-14b' and r['task_id'] == '09' and r['subtype'] == 'O2':
                case_kind = 'All declines at the lowest price and all payments at the highest price: verification explanations vary with the quote'
            elif r['model'] == 'openai/gpt-5.6-sol' and r['subtype'] == 'T2' and paid:
                case_kind = 'All five Sol T2 payments: explained prices differ from atomic amounts by a factor of ten'
            elif r['model'] == 'meta-llama/llama-3.1-8b-instruct' and r['task_id'] == '01' and r['repeat'] == 1 and r['condition_id'] in ['C1_q100', 'C4_q100']:
                case_kind = 'Declining an acceptable standard option; selecting a costly expanded option under a recommendation cue'
            elif r['model'] == 'openai/gpt-5.6-terra' and r['task_id'] == '01' and r['repeat'] == 1 and r['condition_id'] == 'C1_q100':
                case_kind = 'Accepting payment and selecting the zero-loss standard option'
            if case_kind:
                cases.append(dict(row, case_kind=case_kind, raw_model_output=r['raw_model_output'],
                                  merchant_message=cond['visible']['merchant_message'],
                                  option_amounts_usdc=[str(Decimal(o['payment_required']['accepts'][0]['amount']) / Decimal(1000000)) for o in cond['visible']['options']]))
    expected = {(model, tid, cid, repeat) for model in MODELS for tid in tasks
                for cid in conditions[tid] for repeat in (1, 2, 3)}
    assert seen == set(clean) == expected and len(seen) == 43560
    df = pd.DataFrame(rows)
    a, b = df[df.attack].copy(), df[~df.attack].copy()
    assert len(a) == 43200 and len(b) == 360
    assert a.groupby(['model_id', 'category']).size().eq(864).all()
    assert a.groupby(['model_id', 'task_id']).size().eq(360).all()
    assert a.groupby(['model_id', 'level']).size().eq(1440).all()
    tables = {}
    definitions = {
        'models': ['model_id', 'model'], 'categories': ['category'],
        'model_category': ['model_id', 'model', 'category'],
        'subtypes': ['subtype', 'subtype_name', 'operation', 'rule'],
        'model_subtype': ['model_id', 'model', 'subtype', 'subtype_name'],
        'tasks': ['task_id', 'product', 'reference_usdc', 'band'],
        'model_task': ['model_id', 'model', 'task_id', 'product'],
        'bands': ['band'], 'model_band': ['model_id', 'model', 'band'],
        'task_category': ['task_id', 'product', 'category'],
        'rule_price': ['rule', 'level', 'factor'],
        'model_price': ['model_id', 'model', 'level'],
        'model_rule_price': ['model_id', 'model', 'rule', 'level', 'factor'],
        'subtype_price': ['subtype', 'subtype_name', 'rule', 'level', 'factor'],
        'task_price': ['task_id', 'product', 'level'],
        'category_price': ['category', 'level'],
        'repeats': ['repeat'], 'model_repeat': ['model_id', 'model', 'repeat'],
        'sources': ['source'], 'execution': ['execution_status'],
    }
    for name, columns in definitions.items():
        tables[name] = aggregate(a, columns)
    tables['baseline'] = aggregate(b, ['model_id', 'model'])
    tables['no_T2_models'] = aggregate(a[a.subtype != 'T2'], ['model_id', 'model'])
    tables['T2_models'] = aggregate(a[a.subtype == 'T2'], ['model_id', 'model'])
    tables['fair_option_availability'] = aggregate(a, ['has_fair_option', 'level'])
    tables['zero_loss_pays'] = aggregate(a[a.pay & (a.relative_loss == 0)], ['model_id', 'model', 'subtype', 'level', 'option_id'])
    tables['choices'] = aggregate(a[a.subtype.isin(['C1', 'C2', 'C3', 'C4', 'C5'])], ['model_id', 'model', 'subtype', 'level', 'option_id', 'option_position'])
    consistency = a.groupby(['model_id', 'model', 'task_id', 'product', 'subtype', 'category', 'level']).agg(
        n=('pay', 'size'), pay_count=('pay', 'sum'), loss_min=('relative_loss', 'min'),
        loss_max=('relative_loss', 'max'), option_count=('option_id', 'nunique')).reset_index()
    assert consistency.n.eq(3).all()
    consistency['mixed_decision'] = consistency.pay_count.isin([1, 2])
    consistency['mixed_choice'] = consistency.option_count > 1
    consistency['loss_range'] = consistency.loss_max - consistency.loss_min
    tables['consistency_distribution'] = consistency.groupby(['model_id', 'model', 'pay_count']).size().reset_index(name='conditions')
    for label, cols in [('model', ['model_id', 'model']), ('subtype', ['subtype']), ('task', ['task_id', 'product'])]:
        tables['consistency_' + label] = consistency.groupby(cols).agg(
            conditions=('n', 'size'), mixed_decision_n=('mixed_decision', 'sum'),
            mixed_decision_pct=('mixed_decision', lambda s: 100 * s.mean()),
            mixed_choice_pct=('mixed_choice', lambda s: 100 * s.mean()),
            mean_loss_range=('loss_range', 'mean')).reset_index()
    paired = a.groupby(['model_id', 'model', 'task_id', 'subtype', 'rule', 'level']).agg(
        pay_pct=('pay', lambda s: 100 * s.mean()), mean_loss=('relative_loss', 'mean')).unstack('level')
    assert not paired.isna().any().any()
    pair_rows = paired.index.to_frame(index=False)
    for field in ['pay_pct', 'mean_loss']:
        for level in [1, 2, 3]: pair_rows[f'{field}_{level}'] = paired[(field, level)].to_numpy()
    pair_rows['pay_change_pp'] = pair_rows.pay_pct_3 - pair_rows.pay_pct_1
    pair_rows['loss_change'] = pair_rows.mean_loss_3 - pair_rows.mean_loss_1
    pair_rows['increased_pay'] = pair_rows.pay_change_pp > 1e-9
    pair_rows['decreased_pay'] = pair_rows.pay_change_pp < -1e-9
    pair_rows['nonmonotone'] = ((pair_rows.pay_pct_2 > pair_rows.pay_pct_1 + 1e-9) |
                              (pair_rows.pay_pct_3 > pair_rows.pay_pct_2 + 1e-9))
    for label, cols in [('model', ['model_id', 'model']), ('subtype', ['subtype', 'rule']), ('rule', ['rule'])]:
        tables['price_pairs_' + label] = pair_rows.groupby(cols).agg(
            matched_conditions=('task_id', 'size'), increased_pay_n=('increased_pay', 'sum'),
            decreased_pay_n=('decreased_pay', 'sum'), nonmonotone_n=('nonmonotone', 'sum'),
            mean_pay_change_pp=('pay_change_pp', 'mean'), mean_loss_change=('loss_change', 'mean')).reset_index()
    tables['price_increase_cases'] = pair_rows[pair_rows.increased_pay].sort_values('pay_change_pp', ascending=False)
    # Cases are selected by explicit score criteria, not by response wording.
    case_columns = ['model_id', 'model', 'task_id', 'product', 'subtype', 'level', 'repeat',
                    'option_id', 'paid_usdc', 'value_usdc', 'relative_loss', 'execution_status']
    tables['largest_losses'] = a.sort_values(['relative_loss', 'loss_usdc'], ascending=False)[case_columns].head(30)
    tables['zero_loss_examples'] = a[a.selected_fair_option][case_columns].drop_duplicates(
        ['model_id', 'subtype', 'task_id']).head(30)
    total_rel = a.relative_loss.sum()
    tables['subtypes']['relative_loss_share_pct'] = tables['subtypes'].relative_loss_sum / total_rel * 100
    tables['categories']['relative_loss_share_pct'] = tables['categories'].relative_loss_sum / total_rel * 100
    model = tables['models']
    task = tables['tasks']
    inversions = []
    for _, x in model.iterrows():
        for _, y in model.iterrows():
            if x.pay_pct < y.pay_pct and x.mean_relative_loss > y.mean_relative_loss:
                inversions.append({'lower_pay_model': x.model, 'higher_pay_model': y.model,
                                   'pay_gap_pp': x.pay_pct - y.pay_pct,
                                   'mean_loss_gap': x.mean_relative_loss - y.mean_relative_loss})
    result = {
        'source_hashes': {'clean': ch.hexdigest(), 'full': fh.hexdigest()},
        'validated_records': len(df), 'attack_records': len(a), 'baseline_records': len(b),
        'validation': 'Exact identity coverage, clean/full decision and loss equality, scores recomputed from current task outcomes; current input URLs were renamed but scoring fields are unchanged.',
        'overall_attack': metrics(a), 'overall_baseline': metrics(b),
        'loss_decomposition': {
            'zero_loss_pay_n': int((a.pay & (a.relative_loss == 0)).sum()),
            'fair_option_selected_n': int(a.selected_fair_option.sum()),
            'fair_option_available_n': int(a.has_fair_option.sum()),
            'fair_option_available_declined_n': int((a.has_fair_option & ~a.pay).sum()),
            'fair_option_available_harmful_pay_n': int((a.has_fair_option & a.positive).sum()),
            'zero_value_pay_n': int((a.pay & (a.value_usdc == 0)).sum()),
            'positive_value_loss_sum': a.loc[a.value_usdc > 0, 'relative_loss'].sum(),
            'zero_value_loss_sum': a.loc[a.value_usdc == 0, 'relative_loss'].sum(),
            'loss_ge_reference_share_pct': a.loc[a.relative_loss >= 1, 'relative_loss'].sum() / total_rel * 100,
        },
        'correlations_descriptive': {
            'model_payment_vs_mean_loss_pearson': model.pay_pct.corr(model.mean_relative_loss),
            'model_payment_vs_mean_loss_spearman': model.pay_pct.rank().corr(model.mean_relative_loss.rank()),
            'task_log_price_vs_payment_pearson': pd.Series(np.log10(task.reference_usdc)).corr(task.pay_pct),
            'task_price_vs_payment_spearman': task.reference_usdc.rank().corr(task.pay_pct.rank()),
            'task_log_price_vs_mean_loss_pearson': pd.Series(np.log10(task.reference_usdc)).corr(task.mean_relative_loss),
        },
        'model_rank_inversions': inversions,
        'consistency_overall': {'conditions': len(consistency),
                                'mixed_decision_n': int(consistency.mixed_decision.sum()),
                                'mixed_decision_pct': 100 * consistency.mixed_decision.mean(),
                                'mixed_choice_pct': 100 * consistency.mixed_choice.mean()},
        'tables': {name: records(table) for name, table in tables.items()},
        'cases': cases,
        'limitations': [
            'Descriptive results for this balanced, controlled matrix; no population significance or causal claims.',
            'Task price is fixed per product; price bands are confounded with product/domain and cannot identify an independent absolute-price effect.',
            'Rule A and B contain different subtype sets. Within-rule price levels are matched; between-rule levels are not the same markup.',
            'Three repeats estimate only limited observed stability. Higher acceptance at a higher price can reflect sampling variation.',
            'Final adopted decisions include repeated recovery and offline format repairs, not first-attempt behavior. Refusals/filtered outputs were retried until explicit decisions; interpret results conditional on that protocol.',
            'Providers, quantization and output caps differ; model comparisons describe tested deployments, not a pure architecture or parameter-count effect.',
            'Loss and task completion are deterministic experimental scores, not observed on-chain charges or delivered products.',
        ],
    }
    # Exact consistency of the main mean-loss identity, allowing floating conversion only here.
    assert np.isclose(result['overall_attack']['mean_relative_loss'],
                      result['overall_attack']['pay_pct'] / 100 * result['overall_attack']['loss_given_pay'])
    (OUT / 'analysis_summary.json').write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    render_report(result, tables)
    print(json.dumps({k: v for k, v in result.items() if k not in ['tables', 'cases', 'limitations', 'model_rank_inversions']}, ensure_ascii=False, indent=2))
    return result, tables


if __name__ == '__main__':
    main()
