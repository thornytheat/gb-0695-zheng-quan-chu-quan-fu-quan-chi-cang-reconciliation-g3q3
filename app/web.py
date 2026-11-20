from flask import Blueprint, render_template, request
from app.config import TITLE, MODULES, SHORT, SAMPLE
from app.pipeline import calculate
from app.store import latest, save, get_run
from app import withdrawal_store
bp=Blueprint('web',__name__)
@bp.route('/')
@bp.route('/work/<int:stage>')
def work(stage=1):
    if stage<1 or stage>len(MODULES):
        from flask import abort
        abort(404)
    run=get_run(request.args['run_id']) if request.args.get('run_id') else latest()
    if run is None: run=save(calculate())
    withdrawal=None;current=None
    if stage<=7:
        current=run['stages'][str(stage)]
    else:
        withdrawal=withdrawal_store.latest_for_run(run['manifest']['run_id'])
        totals=(withdrawal or {}).get('totals',{})
        current={'name':'撤销冲减','rows':[{k:v for k,v in row.items() if k!='凭证明细'} for row in (withdrawal or {}).get('rows',[])],
                 'metrics':[['应付合计',totals.get('应付_CNY','0.00'),'CNY'],['已付转应收',totals.get('转应收_CNY','0.00'),'CNY'],
                            ['在途止付',totals.get('在途_CNY','0.00'),'CNY'],['未付终止',totals.get('终止支付_CNY','0.00'),'CNY'],
                            ['本批冲减',totals.get('冲减_CNY','0.00'),'CNY'],['结批状态',(withdrawal or {}).get('status','未建立'),'']]}
        if withdrawal: current['metrics'].append(['可结批','是' if withdrawal['can_close'] else '否',''])
    return render_template('m'+str(stage)+'.html', title=TITLE, modules=MODULES, short=SHORT, run=run, current=current, stage=stage, sample=SAMPLE.read_text(encoding='utf-8-sig'), withdrawal=withdrawal)
