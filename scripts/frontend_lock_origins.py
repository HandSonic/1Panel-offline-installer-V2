"""Validate pinned npm registry origins without runtime or YAML dependencies."""
from pathlib import Path
import re


def validate_lock_origins(lock):
    """Bundled entries inherit only an explicitly declared, integrity-pinned archive."""
    packages=lock['packages']
    bundled_owners = {}
    package_name=r'(?:@[A-Za-z0-9._~-]+/)?[A-Za-z0-9._~-]+'
    def tarball(item):
        url=item.get('resolved','')
        if not isinstance(url,str) or not re.fullmatch(r'https://registry\.npmjs\.org/[^?#\s]+/-/[^/?#\s]+\.tgz',url):
            raise ValueError('Dependency must use the official npm registry tarball')
        if not isinstance(item.get('integrity'),str) or not re.fullmatch(r'sha512-[A-Za-z0-9+/]{86}==',item['integrity']):
            raise ValueError('Dependency requires pinned SHA-512 integrity')
    for path,item in packages.items():
        if path=='':continue
        if not isinstance(item,dict) or item.get('link') or not re.fullmatch('node_modules/'+package_name+'(?:/node_modules/'+package_name+')*',path) or '..' in Path(path).parts:
            raise ValueError('Unsupported frontend dependency source')
        if item.get('inBundle') is not True:
            tarball(item)
            continue
        # Bundled contents are shipped inside an ancestor tarball, not fetched by
        # a missing child URL. Require the complete explicit bundle ancestry.
        child=path
        while True:
            if '/node_modules/' not in child:
                raise ValueError('Bundled dependency has no pinned containing package')
            parent,direct=child.rsplit('/node_modules/',1)
            owner=packages.get(parent)
            if not isinstance(owner,dict) or owner.get('link'):
                raise ValueError('Bundled dependency parent is absent or linked')
            if owner.get('inBundle') is True:
                child=parent
                continue
            declared=owner.get('bundleDependencies',owner.get('bundledDependencies'))
            if not isinstance(declared,list) or direct not in declared:
                raise ValueError('Containing tarball does not declare this bundled dependency')
            tarball(owner)
            bundled_owners[path] = parent
            break
        # If npm retains a separate origin too, do not ignore an invalid one.
        if 'resolved' in item or 'integrity' in item:tarball(item)
    return bundled_owners
