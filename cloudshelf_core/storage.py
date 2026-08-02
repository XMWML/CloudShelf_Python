import json
import os
import stat
import uuid


class ProfileStore:
    def __init__(self, path):
        self.path = path

    def load(self):
        try:
            with open(self.path, encoding='utf-8') as handle:
                profiles = json.load(handle)
        except (OSError, ValueError, TypeError):
            return []
        return [self.migrate(profile) for profile in profiles if isinstance(profile, dict)]

    def save(self, profiles):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        temporary = self.path + '.tmp'
        with open(temporary, 'w', encoding='utf-8') as handle:
            json.dump(profiles, handle, ensure_ascii=False, indent=2)
        os.replace(temporary, self.path)
        try:
            os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass

    @staticmethod
    def migrate(profile):
        result = dict(profile)
        result.setdefault('id', str(uuid.uuid4()))
        result.setdefault('name', result.get('host', 'CloudShelf connection'))
        result.setdefault('protocol', 'FTP')
        result.setdefault('port', {'FTP': 21, 'SFTP': 22, 'WebDAV': 443}.get(result['protocol'], 21))
        result.setdefault('username', '')
        result.setdefault('password', '')
        result.setdefault('sync_rules', result.pop('rules', []))
        result.setdefault('auth', 'password')
        result.setdefault('private_key', '')
        result.setdefault('host_key_policy', 'accept-new')
        result.setdefault('base_path', '/')
        result.setdefault('tls', result.get('protocol') == 'WebDAV')
        result.setdefault('sync_rules', [])
        return result
