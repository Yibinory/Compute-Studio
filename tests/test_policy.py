import copy
import time

import pytest

from scheduler.policy import choose,dependencies_ready


def server(key='s',idle=True,util=0,free=20000,speed=100):
    gpu={'index':0,'uuid':'g','name':'A100','memory_total_mb':24000,'memory_free_mb':free,'memory_used_mb':24000-free,'is_idle':idle,'utilization_gpu':util}
    return {'id':key,'enabled':True,'connected':True,'snapshot':{'sampled_at':time.time(),'work_free_gb':100,'gpus':[gpu]},'benchmarks':{'g':{'tensor_tflops':speed}}}


@pytest.mark.parametrize('level,idle,util,expected',[('idle_only',False,0,False),('idle_only',True,0,True),('low_interference',False,29,True),('low_interference',False,30,False),('emergency',False,99,True)])
def test_execution_levels(level,idle,util,expected):
    result=choose({'level':level,'memory_mb':1000,'max_utilization':30},[server(idle=idle,util=util)],[],{})
    assert bool(result)==expected


@pytest.mark.parametrize('change',[{'blocked_gpus':['g']},{'enabled':False},{'paused':True},{'connected':False}])
def test_server_and_gpu_guards(change):
    s=server();s.update(change)
    assert choose({'level':'emergency'},[s],[],{}) is None


def test_stale_and_disk_guards():
    s=server();s['snapshot']['sampled_at']=time.time()-31
    assert choose({},[s],[],{}) is None
    s['snapshot']['sampled_at']=time.time();s['snapshot']['work_free_gb']=4
    assert choose({},[s],[],{}) is None


def test_peak_and_reservation_prevent_overcommit():
    s=server(idle=False)
    s['history']={'g':[{'memory_used_mb':22000,'utilization_gpu':20}]}
    task={'level':'low_interference','memory_mb':3000}
    assert choose(task,[s],[],{}) is None
    s.pop('history')
    active={'id':'t','status':'running','server_id':'s','gpu_uuid':'g','memory_mb':18000}
    assert choose(task,[s],[active],{}) is None


def test_idle_before_speed_then_fastest():
    task={'level':'emergency'}
    assert choose(task,[server('fast-busy',idle=False,speed=1000),server('idle',speed=10)],[],{})[0]['id']=='idle'
    assert choose(task,[server('slow',speed=10),server('fast',speed=100)],[],{})[0]['id']=='fast'


def test_affinity_model_and_memory():
    assert choose({'server_ids':['other']},[server()],[],{}) is None
    assert choose({'gpu_name':'4090'},[server()],[],{}) is None
    assert choose({'memory_mb':20001},[server()],[],{}) is None


def test_dependency_requires_local_sync():
    t={'depends_on':['a','b']}
    deps=[{'id':'a','status':'success','synced_at':1},{'id':'b','status':'success'}]
    assert not dependencies_ready(t,deps)
    deps[1]['synced_at']=2
    assert dependencies_ready(t,deps)
    deps[0]['status']='failed'
    assert not dependencies_ready(t,deps)


def test_duplicate_ssh_aliases_share_gpu_reservations():
    active={'id':'t','status':'running','server_id':'alias-A','gpu_uuid':'g','memory_mb':18000}
    assert choose({'level':'emergency','memory_mb':4000},[server('alias-B')],[active],{}) is None
