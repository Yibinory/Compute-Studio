"""Cooperative cancellation and phase-local transfer estimates."""
import time


class OperationCanceled(Exception):
    pass


def measured(previous, value):
    now=time.time()
    key=lambda p:(p.get('phase'),p.get('server'),p.get('total'),p.get('group_index'))
    same=key(previous)==key(value) and value.get('bytes',0)>=previous.get('bytes',0)
    elapsed=now-previous.get('updated_at',now)
    delta=value.get('bytes',0)-previous.get('bytes',0)
    speed=previous.get('bytes_per_second',0) if same else 0
    if same and elapsed>0 and delta>0:
        sample=delta/elapsed
        speed=.7*speed+.3*sample if speed else sample
    return {**value,'updated_at':now,'bytes_per_second':speed,
            'eta_seconds':max(0,(value['total']-value.get('bytes',0))/speed) if speed>0 and value.get('total',0)>0 else None}
