import json
import os
import tempfile
import unittest

from cloudshelf_core.paths import join, norm
from cloudshelf_core.storage import ProfileStore
from cloudshelf_core.sync import SyncEngine
from cloudshelf_core.remote import RemoteClient


class FakeClient:
    def __init__(self):
        self.items = {}
        self.uploads = []
        self.deletes = []

    def list(self, folder):
        prefix = norm(folder).rstrip('/') + '/'
        result = {}
        for path, item in self.items.items():
            if not path.startswith(prefix):
                continue
            rest = path[len(prefix):]
            name = rest.split('/', 1)[0]
            child = join(folder, name)
            if child in result:
                continue
            directory = '/' in rest or item.get('directory', False)
            result[child] = {'name': name, 'path': child, 'directory': directory, 'size': None if directory else item['size'], 'modified': '-'}
        return list(result.values())

    def mkdir(self, path):
        self.items.setdefault(norm(path), {'directory': True})

    def upload(self, local, directory):
        target = join(directory, os.path.basename(local))
        self.items[target] = {'directory': False, 'size': os.path.getsize(local)}
        self.uploads.append(target)

    def download(self, item, target):
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, 'wb') as handle:
            handle.write(b'remote')

    def delete(self, path):
        self.deletes.append(path)
        self.items.pop(path, None)


class CoreTests(unittest.TestCase):
    def test_paths(self):
        self.assertEqual(norm('a/../b'), '/b')
        self.assertEqual(join('/root', '../file'), '/file')

    def test_remote_profile_compatibility(self):
        client = RemoteClient({'host': 'example.com', 'port': 22, 'protocol': 'SFTP'})
        self.assertIs(client.p, client.profile)
        self.assertTrue(client.url('/').startswith('sftp://example.com:22/'))
        ftps = RemoteClient({'host': 'ftps://files.example.com:990/backups', 'port': 21, 'protocol': 'FTP'})
        self.assertEqual(ftps.p['host'], 'files.example.com')
        self.assertEqual(ftps.p['base_path'], '/backups')
        self.assertTrue(ftps.url('/').startswith('ftps://files.example.com:990/'))

    def test_profile_migration_and_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'connections.json')
            with open(path, 'w', encoding='utf-8') as handle:
                json.dump([{'id': '1', 'name': 'old', 'rules': []}], handle)
            profile = ProfileStore(path).load()[0]
            self.assertIn('sync_rules', profile)
            ProfileStore(path).save([profile])
            with open(path, encoding='utf-8') as handle:
                self.assertEqual(json.load(handle)[0]['id'], '1')

    def test_sync_upload_and_download(self):
        with tempfile.TemporaryDirectory() as directory:
            local = os.path.join(directory, 'local'); os.makedirs(local)
            with open(os.path.join(local, 'hello.txt'), 'wb') as handle: handle.write(b'hello')
            client = FakeClient()
            rule = {'local': local, 'remote': '/sync', 'upload': True, 'download': True, 'conflict': 'keep_local'}
            report = SyncEngine(client, rule).run()
            self.assertEqual(report['uploaded'], 1)
            self.assertIn('/sync/hello.txt', client.uploads)

    def test_deletion_requires_previous_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            local = os.path.join(directory, 'local'); os.makedirs(local)
            client = FakeClient(); client.items['/sync/old.txt'] = {'directory': False, 'size': 3}
            rule = {'local': local, 'remote': '/sync', 'upload': False, 'delete_remote': True, 'snapshot': {'local': ['old.txt'], 'remote': ['old.txt']}}
            SyncEngine(client, rule).run()
            self.assertEqual(client.deletes, ['/sync/old.txt'])


if __name__ == '__main__':
    unittest.main()
