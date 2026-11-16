"""Restore already-issued media capabilities during the gateway migration."""
import json
import os
import shutil
import pwd
from pathlib import Path

source=Path('/opt/mcpmanager/bootstrap/media-restore')
destination=Path('/var/lib/mcpmanager/media')
destination.mkdir(parents=True,exist_ok=True)
os.chmod(destination,0o750)
account=pwd.getpwnam('mcpmanager')
os.chown(destination,account.pw_uid,account.pw_gid)
expires={
    'JQs1gEB3xlZ6VXAN_Rj7LiqPF5jHepjvx0nYGuSV_O0':1790791480+259200,
    'rimhtPLQxdZG9pgd2KfXCBNowJQgYqt9CJxtPpiR7UY':1790791483+259200,
    '_0Gb3aAvKFIE2aq2m0nmJM2dl5tpoNsgXLhZTnSJsQ4':1790791486+259200,
}
for token,expiry in expires.items():
    payload=(source/token).read_bytes()
    if not payload.startswith(b'\x89PNG\r\n\x1a\n'):
        raise ValueError(f'{token}: expected PNG')
    target=destination/token
    target.write_bytes(payload)
    target.with_suffix('.json').write_text(json.dumps({
        'content_type':'image/png','expires_at':expiry})+'\n',encoding='utf-8')
    os.chmod(target,0o640)
    os.chmod(target.with_suffix('.json'),0o640)
    os.chown(target,account.pw_uid,account.pw_gid)
    os.chown(target.with_suffix('.json'),account.pw_uid,account.pw_gid)
config_path=Path('/etc/mcpmanager/cloud.json')
config=json.loads(config_path.read_text())
config['media_cache_dir']=str(destination)
config_path.write_text(json.dumps(config,ensure_ascii=False,indent=2)+'\n')
print('Restored',len(expires),'existing media capabilities without changing their URLs')
