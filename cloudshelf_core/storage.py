import json
import os
import stat
import uuid

try:
    import keyring
except ImportError:
    keyring = None


class CredentialStore:
    service = 'CloudShelf'

    @classmethod
    def available(cls):
        return keyring is not None

    @classmethod
    def save(cls, profile_id, password):
        if keyring is not None and password:
            keyring.set_password(cls.service, str(profile_id), password)

    @classmethod
    def load(cls, profile_id):
        if keyring is None:
            return None
        try:
            return keyring.get_password(cls.service, str(profile_id))
        except Exception:
            return None

    @classmethod
    def delete(cls, profile_id):
        if keyring is not None:
            try:
                keyring.delete_password(cls.service, str(profile_id))
            except Exception:
                pass


class ProfileStore:
    def __init__(self, path):
        self.path = path

    def load(self):
        try:
            with open(self.path, encoding='utf-8') as handle:
                profiles = json.load(handle)
        except (OSError, ValueError, TypeError):
            return []
        result = []
        for profile in profiles:
            if not isinstance(profile, dict):
                continue
            profile = self.migrate(profile)
            stored = CredentialStore.load(profile['id'])
            if stored:
                profile['password'] = stored
            result.append(profile)
        return result

    def save(self, profiles):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        temporary = self.path + '.tmp'
        persisted = []
        for profile in profiles:
            entry = dict(profile)
            password = entry.get('password', '')
            if CredentialStore.available() and password:
                CredentialStore.save(entry.get('id'), password)
                entry.pop('password', None)
            persisted.append(entry)
        with open(temporary, 'w', encoding='utf-8') as handle:
            json.dump(persisted, handle, ensure_ascii=False, indent=2)
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
