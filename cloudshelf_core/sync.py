import email.utils
import os
import posixpath
import shutil
import time

from .paths import join, norm


class SyncEngine:
    """Protocol-neutral synchronization engine.

    The client only needs list, upload, download, mkdir and delete methods.
    A rule keeps a compact snapshot so deletion propagation is opt-in and
    cannot delete items that were not observed by a previous successful run.
    """

    def __init__(self, client, rule, progress=None):
        self.client = client
        self.rule = rule
        self.progress = progress or (lambda _: None)

    def local_inventory(self):
        root = os.path.abspath(self.rule['local'])
        output = {}
        for base, directories, files in os.walk(root):
            relative_base = os.path.relpath(base, root)
            if relative_base != '.':
                output[relative_base] = {'directory': True}
            for name in files:
                path = os.path.join(base, name)
                output[os.path.relpath(path, root)] = {
                    'directory': False,
                    'mtime': os.path.getmtime(path),
                    'path': path,
                    'size': os.path.getsize(path),
                }
        return output

    def remote_inventory(self, folder):
        output = {}

        def visit(path):
            for item in self.client.list(path):
                relative = posixpath.relpath(item['path'], folder)
                output[relative] = {
                    'directory': item['directory'],
                    'item': item,
                    'size': item.get('size'),
                }
                if item['directory']:
                    visit(item['path'])

        visit(folder)
        return output

    @staticmethod
    def remote_mtime(item):
        try:
            return email.utils.parsedate_to_datetime(item.get('modified', '')).timestamp()
        except (AttributeError, TypeError, ValueError, OverflowError):
            return 0

    def run(self):
        root = os.path.abspath(self.rule['local'])
        os.makedirs(root, exist_ok=True)
        remote_root = norm(self.rule['remote'])
        local = self.local_inventory()
        remote = self.remote_inventory(remote_root)
        report = {'uploaded': 0, 'downloaded': 0, 'deleted_local': 0, 'deleted_remote': 0}
        snapshot = self.rule.get('snapshot') or {'local': [], 'remote': []}

        if self.rule.get('delete_remote'):
            for relative in set(snapshot['local']) - set(local):
                if relative in remote:
                    self.client.delete(remote[relative]['item']['path'])
                    report['deleted_remote'] += 1

        if self.rule.get('delete_local'):
            for relative in set(snapshot['remote']) - set(remote):
                path = os.path.join(root, relative)
                if os.path.exists(path):
                    shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)
                    report['deleted_local'] += 1

        upload = self.rule.get('upload', True)
        download = self.rule.get('download', False)
        conflict = self.rule.get('conflict', 'keep_newest')
        if conflict == 'keep_remote':
            upload = False
        if conflict == 'keep_local':
            download = False

        if upload:
            for relative, local_item in local.items():
                if local_item['directory']:
                    continue
                remote_item = remote.get(relative)
                remote_time = self.remote_mtime(remote_item['item']) if remote_item else 0
                changed = (
                    remote_item is None
                    or remote_item['size'] != local_item['size']
                    or (conflict == 'keep_newest' and local_item['mtime'] > remote_time)
                )
                if not changed:
                    continue
                parent = posixpath.dirname(relative)
                current = remote_root
                for part in filter(None, parent.split('/')):
                    current = join(current, part)
                    try:
                        self.client.mkdir(current)
                    except Exception:
                        pass
                self.client.upload(local_item['path'], join(remote_root, parent))
                report['uploaded'] += 1
                self.progress(f'上传 {relative}')

        if download:
            for relative, remote_item in remote.items():
                if remote_item['directory']:
                    continue
                path = os.path.join(root, relative)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                local_time = os.path.getmtime(path) if os.path.exists(path) else 0
                changed = (
                    not os.path.exists(path)
                    or os.path.getsize(path) != remote_item['size']
                    or (conflict == 'keep_newest' and self.remote_mtime(remote_item['item']) > local_time)
                )
                if changed:
                    self.client.download(remote_item['item'], path)
                    report['downloaded'] += 1
                    self.progress(f'下载 {relative}')

        self.rule['snapshot'] = {
            'local': list(self.local_inventory()),
            'remote': list(self.remote_inventory(remote_root)),
        }
        self.rule['last_sync'] = time.time()
        return report
