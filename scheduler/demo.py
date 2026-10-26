from __future__ import annotations

import time


def seed(engine):
    engine.settings.update(paused=True,demo=True)
    engine.store.put('settings',engine.settings)
    if engine.store.all('projects'):
        for server in engine.store.all('servers'):
            if server['host'].startswith('demo-node-'):
                server.update(connected=True,error='')
                engine.store.put('servers',server)
        upgrade(engine)
        return
    now=time.time()
    for i,(title,model,mem,count) in enumerate([('Atlas · 训练节点','NVIDIA A100-SXM4-80GB',81920,2),('Orion · 实验节点','NVIDIA GeForce RTX 4090',24576,2),('Lyra · 共享存储节点','NVIDIA GeForce RTX 3090',24576,1)]):
        s=engine.save_server({'name':title,'host':'demo-node-'+str(i+1),'username':'research','work_root':'/srv/experiments','data_root':'/mnt/shared/datasets','enabled':False})
        gpus=[]
        for index in range(count):
            busy=i==0 and index==0 or i==1 and index==1
            used=mem*.62 if busy else 640
            gpus.append({'index':index,'uuid':'DEMO-GPU-'+str(i)+'-'+str(index),'name':model,'memory_total_mb':mem,'memory_used_mb':int(used),'memory_free_mb':int(mem-used),'utilization_gpu':74 if busy else 0,'temperature_c':61 if busy else 32,'power_draw_w':238 if busy else 24,'is_idle':not busy,'processes':[]})
        s.update(connected=True,enabled=False,snapshot={'sampled_at':now,'cpu_percent':32.4+i*6,'memory':{'used_percent':41.2+i*3},'work_free_gb':420.5-i*80,'data_free_gb':2140,'gpus':gpus},benchmarks={g['uuid']:{'tensor_tflops':156.2 if i==0 else 78.1-i*12} for g in gpus})
        engine.store.put('servers',s)
    p=engine.create_project({'name':'视觉表征学习','description':'从基线训练到特征评估，将跨服务器实验放在同一个项目。'})
    engine.create_project({'name':'大模型推理优化','description':'对比不同量化配置与推理吞吐，维护可复现的实验代码。'})
    servers=engine.store.all('servers')
    first=engine.create_task(p['id'],{'name':'ResNet50_基线训练','command':'python train.py --data "$DATASET_0"','datasets':['imagenet'],'memory_mb':32000,'priority':10})
    first.update(status='running',server_id=servers[0]['id'],gpu_index=0,gpu_uuid='DEMO-GPU-0-0',attempt_id='a'*32,attempts=1,started_at=now-900)
    engine.store.put('tasks',first)
    second=engine.create_task(p['id'],{'name':'线性探测_评估','command':'python evaluate.py','depends_on':[first['id']],'memory_mb':12000,'priority':20})
    second.update(status='blocked',reason='等待前序训练完成并回传本机')
    engine.store.put('tasks',second)
    third=engine.create_task(p['id'],{'name':'数据增强_消融实验','command':'python ablation.py','memory_mb':16000,'priority':30,'level':'low_interference'})
    third.update(status='waiting',reason='等待符合规则的 GPU')
    engine.store.put('tasks',third)
    (engine.local_project(p['id'])/'code'/'train.py').write_text('# 演示代码，请上传自己的实验代码。\n',encoding='utf-8')
    engine.store.event('演示工作空间已加载；真实 SSH 与调度循环关闭')
    upgrade(engine)


def upgrade(engine):
    """Upgrade only synthetic demonstration records; never reset user-created content."""
    from .files import manifest
    from .storage import uid
    now=time.time()
    servers=[s for s in engine.store.all('servers') if s['host'].startswith('demo-node-')]
    for i,s in enumerate(servers):
        snap=s['snapshot'];snap.update(cpu_count=64 if i==0 else 32,load_average=[4.2+i,3.8+i,3.4+i],hostname=s['host'],sampled_at=now)
        memory=snap['memory'];total=(256 if i==0 else 128)*1024**3
        memory.update(total_bytes=total,used_bytes=int(total*memory['used_percent']/100),available_bytes=int(total*(1-memory['used_percent']/100)))
        for key,total,free,path in [('work_disk',1024**4,snap['work_free_gb']*1024**3,s['work_root']),('data_disk',4*1024**4,snap['data_free_gb']*1024**3,s['data_root'])]:
            snap[key]={'total_bytes':total,'used_bytes':int(total-free),'free_bytes':int(free),'used_percent':round((total-free)/total*100,1),'path':path}
        for result in s.get('benchmarks',{}).values():
            result.update(measured_at=now-3600,fp32_tflops=round(result['tensor_tflops']*.23,1),matrix_size=4096,simulated=True)
        engine.store.put('servers',s)
    for scope in ('work','data'):
        configuration={s['id']:[s.get(k) for k in ('host','port','username',scope+'_root')] for s in engine.store.all('servers')}
        groups=[[servers[0]['id'],servers[2]['id']],[servers[1]['id']]] if scope=='work' and len(servers)>=3 else [[s['id'] for s in servers]]
        engine.store.put('storage',{'id':scope,'scope':scope,'checked_at':now,'groups':groups,'configuration':configuration,'states':{s['id']:{'status':'verified','root':s[scope+'_root']} for s in servers},'simulated':True})
    p=next((p for p in engine.store.all('projects') if p['name']=='视觉表征学习'),None)
    if not p:
        return
    existing=[t for t in engine.store.all('tasks') if t['project_id']==p['id']]
    for t in existing:
        if t['name']=='ResNet50_基线训练' and t['status']=='running':
            t['started_at']=now-900;engine.store.put('tasks',t)
    if any(t['name']=='数据准备与验证' for t in existing):
        return
    tasks=[t for t in engine.store.all('tasks') if t['project_id']==p['id']]
    if len(tasks)<3:
        return
    prep=engine.create_task(p['id'],{'name':'数据准备与验证','command':'python prepare.py','priority':1})
    attempt=uid();result=engine.local_project(p['id'])/'runs'/attempt/'result';result.mkdir(parents=True)
    (result/'dataset_stats.json').write_text('{"train": 1281167, "validation": 50000}',encoding='utf-8')
    (result/'validation.log').write_text('Validation passed. All dataset files verified.',encoding='utf-8')
    prep.update(status='success',attempt_id=attempt,started_at=now-2400,finished_at=now-1800,synced_at=now-1780,created_at=now-2500,exit_code=0,server_id=servers[2]['id'],gpu_index=0,input_manifest={},result_manifest=manifest(result))
    engine.store.put('tasks',prep)
    for i,t in enumerate(tasks[:3]):
        t.update(created_at=now-1700+i*5)
        if i in (0,2):t['depends_on']=[prep['id']]
        if i==0:t['started_at']=now-900
        engine.store.put('tasks',t)
    distill=engine.create_task(p['id'],{'name':'知识蒸馏_联合对比','command':'python distill.py','depends_on':[tasks[0]['id'],tasks[2]['id']]})
    distill.update(status='blocked',created_at=now-1690,reason='等待两个前序完成并回传本机');engine.store.put('tasks',distill)
    report=engine.create_task(p['id'],{'name':'汇总指标与实验报告','command':'python report.py','depends_on':[tasks[1]['id'],distill['id']]})
    report.update(created_at=now-1680);engine.store.put('tasks',report)
    engine.store.put('notes',{'id':uid(),'task_id':prep['id'],'project_id':p['id'],'body':'训练集和验证集已核验。共享数据目录可直接使用，无需重复上传。','created_at':now-1770,'updated_at':now-1770})
    engine.store.put('notes',{'id':uid(),'task_id':tasks[0]['id'],'project_id':p['id'],'body':'基线：batch size 256，学习率 0.1。完成后检查 checkpoint 与验证精度。','created_at':now-850,'updated_at':now-850})
