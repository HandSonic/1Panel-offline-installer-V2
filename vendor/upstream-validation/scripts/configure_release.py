#!/usr/bin/env python3
"""Set embedded community release flags; stable/beta/dev track upstream modes."""
import pathlib,re,sys
from resolve_inputs import resolve

def configure(root, version):
    mode=resolve(version)['mode']
    for component in ('core','agent'):
        file=root/component/'cmd/server/conf/app.yaml'
        text=file.read_text()
        values={'mode':mode,'level':'info','is_demo':'false','is_offline':'false','is_fxplay':'false','is_enterprise':'false'}
        # Upstream embeds the release version in Core only. Agent's reviewed
        # app.yaml has no version field; preserve that component's schema.
        if component == 'core': values['version']=version
        for key,value in values.items():
            text,count=re.subn(r'(?m)^(  '+key+r':) .+$',lambda m:m[1]+' '+value,text)
            if count != 1: raise ValueError(f'{file}: expected exactly one {key}, found {count}')
        file.write_text(text)
if __name__=='__main__': configure(pathlib.Path(sys.argv[1]),sys.argv[2])
