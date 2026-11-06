"""Codex 的结构化输出要求每个对象的 required 列出全部字段，否则整次调用直接被拒。"""
import json
from pathlib import Path
import unittest

SCHEMAS=Path(__file__).resolve().parent.parent/'schemas'


def missing(node,path=''):
    found=[]
    if isinstance(node,dict):
        if node.get('type')=='object' and 'properties' in node:
            gap=set(node['properties'])-set(node.get('required',[]))
            if gap:found.append((path or '/',sorted(gap)))
            if node.get('additionalProperties') is not False:found.append((path or '/','additionalProperties'))
        for key,value in node.items():found+=missing(value,path+'/'+key)
    elif isinstance(node,list):
        for i,value in enumerate(node):found+=missing(value,f'{path}[{i}]')
    return found


class SchemaStrictTest(unittest.TestCase):
    def test_every_object_lists_all_fields_as_required(self):
        for file in sorted(SCHEMAS.glob('*.json')):
            with self.subTest(schema=file.name):
                self.assertEqual(missing(json.loads(file.read_text())),[])


if __name__=='__main__':unittest.main()
