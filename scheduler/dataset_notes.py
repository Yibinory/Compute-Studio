"""Local dataset annotations, independent of transfer and manifest records."""
import time

from werkzeug.exceptions import Conflict, NotFound

MAX_CHARACTERS = 1_000_000


def read(engine, dataset_id):
    try:
        engine.store.get_summary('datasets', dataset_id)
    except ValueError:
        raise NotFound('数据集不存在')
    try:
        return engine.store.get('dataset_notes', dataset_id)
    except ValueError:
        return {'id': dataset_id, 'text': '', 'revision': 0, 'updated_at': None}


def compact(note):
    text = note['text']
    return {'id': note['id'], 'preview': text[:240], 'characters': len(text),
            'revision': note['revision'], 'updated_at': note['updated_at']}


def save(engine, dataset_id, data):
    if not isinstance(data, dict):
        raise ValueError('请提供备注内容和版本')
    text, revision = data.get('text'), data.get('revision')
    if not isinstance(text, str) or len(text) > MAX_CHARACTERS or '\x00' in text:
        raise ValueError('备注须为不超过 100 万字符的文本，且不能包含空字符')
    if type(revision) is not int or revision < 0:
        raise ValueError('备注版本无效，请重新打开编辑窗口')
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    if not text.strip():
        text = ''
    # No engine/SSH locks: notes remain editable during long transfers. The
    # store lock also serializes dataset deletion and concurrent note editors.
    with engine.store.lock:
        previous = read(engine, dataset_id)
        if text == previous['text']:
            return previous  # Retry after a lost save response is harmless.
        if revision != previous['revision']:
            raise Conflict('备注已在另一个页面更新。本页内容已保留，请复制后重新打开备注再合并。')
        value = {'id': dataset_id, 'text': text, 'revision': revision + 1,
                 'updated_at': time.time()}
        engine.store.put('dataset_notes', value)
        return value
