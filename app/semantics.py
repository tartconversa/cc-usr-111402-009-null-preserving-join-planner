"""受限谓词的三值语义。None表示unknown，WHERE仅保留True。"""
def evaluate(row,expression):
    if expression is None:return True
    op=expression['op']
    if op in ('and','or'):
        values=[evaluate(row,e) for e in expression['args']]
        if op=='and':
            if any(x is False for x in values):return False
            return None if any(x is None for x in values) else True
        if any(x is True for x in values):return True
        return None if any(x is None for x in values) else False
    if op=='not':
        v=evaluate(row,expression['arg'])
        return None if v is None else not v
    value=row.get(expression['field'])
    if op=='is_null':return value is None
    other=expression['value']
    if value is None or other is None:return None
    if type(value) is not type(other):raise ValueError('谓词值类型不一致')
    if op=='eq':return value==other
    if op=='lt':return value<other
    if op=='le':return value<=other
    if op=='gt':return value>other
    if op=='ge':return value>=other
    raise ValueError('未知谓词')

def where(row,expression):return evaluate(row,expression) is True
