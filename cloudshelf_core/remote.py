import base64
import os
import posixpath
import subprocess
import tempfile
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET

from .paths import join, norm


class RemoteClient:
    def __init__(self, profile):
        self.profile = profile
        self.p = profile
        self.password = profile.get('password', '')
        self.protocol = profile.get('protocol', 'FTP')
        self.scheme = {'FTP': 'ftp', 'SFTP': 'sftp', 'WebDAV': 'https' if profile.get('tls', True) else 'http'}[self.protocol]
        self.base = norm(profile.get('base_path', '/'))

    def url(self, path):
        server_path = join(self.base, path)
        return f'{self.scheme}://{self.profile["host"]}:{self.profile["port"]}{urllib.parse.quote(server_path)}'

    def _sftp_command(self, commands):
        args = ['sftp', '-q', '-P', str(self.profile['port']), '-o', 'StrictHostKeyChecking=' + ('yes' if self.profile.get('host_key_policy') == 'strict' else 'accept-new')]
        if self.profile.get('private_key'):
            args += ['-i', self.profile['private_key']]
        args += [f'{self.profile.get("username", "")}@{self.profile["host"]}']
        environment = os.environ.copy()
        temporary = []
        if self.profile.get('auth', 'password') == 'password' and self.password:
            secret = tempfile.NamedTemporaryFile(mode='w', delete=False)
            secret.write(self.password)
            secret.close()
            askpass = tempfile.NamedTemporaryFile(mode='w', delete=False)
            askpass.write('#!/bin/sh\ncat "$CLOUDSHELF_ASKPASS_FILE"\n')
            askpass.close()
            os.chmod(askpass.name, 0o700)
            temporary = [secret.name, askpass.name]
            environment.update({'CLOUDSHELF_ASKPASS_FILE': secret.name, 'SSH_ASKPASS': askpass.name, 'SSH_ASKPASS_REQUIRE': 'force', 'DISPLAY': 'cloudshelf:0'})
        try:
            return subprocess.check_output(args, input=('\n'.join(commands) + '\n').encode(), stderr=subprocess.STDOUT, timeout=120, env=environment).decode(errors='replace')
        finally:
            for path in temporary:
                try:
                    os.remove(path)
                except OSError:
                    pass

    @staticmethod
    def _quote(value):
        return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"') + '"'

    def list(self, path):
        if self.protocol == 'SFTP':
            output = self._sftp_command([f'ls -la {self._quote(join(self.base, path))}'])
            result = []
            for line in output.splitlines():
                parts = line.split(None, 8)
                if len(parts) >= 9 and parts[8] not in ('.', '..'):
                    name = parts[8]
                    result.append({'name': name, 'path': join(path, name), 'directory': parts[0].startswith('d'), 'size': int(parts[4]) if parts[4].isdigit() else None, 'modified': '-'})
            return result
        if self.protocol == 'FTP':
            return self._parse_unix_listing(self._curl(['--url', self.url(path), '--disable-epsv']), path)
        data = b'<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop><d:displayname/><d:resourcetype/><d:getcontentlength/><d:getlastmodified/></d:prop></d:propfind>'
        request = urllib.request.Request(self.url(path), method='PROPFIND' if self.protocol == 'WebDAV' else 'GET', headers={'Depth': '1', 'Content-Type': 'application/xml'}, data=data)
        credentials = base64.b64encode(f'{self.profile.get("username", "")}:{self.password}'.encode()).decode()
        request.add_header('Authorization', 'Basic ' + credentials)
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read()
        output = []
        for response in ET.fromstring(body).iter():
            if response.tag.split('}')[-1] != 'response':
                continue
            href = next((node.text for node in response.iter() if node.tag.split('}')[-1] == 'href'), '') or ''
            item_path = norm(urllib.parse.unquote(urllib.parse.urlparse(href).path))
            if item_path.rstrip('/') == join(self.base, path).rstrip('/'):
                continue
            name = posixpath.basename(item_path)
            is_directory = any(node.tag.split('}')[-1] == 'collection' for node in response.iter())
            size = next((node.text for node in response.iter() if node.tag.split('}')[-1] == 'getcontentlength'), None)
            modified = next((node.text for node in response.iter() if node.tag.split('}')[-1] == 'getlastmodified'), '-')
            output.append({'name': name, 'path': join(path, name), 'directory': is_directory, 'size': int(size) if size and size.isdigit() else None, 'modified': modified})
        return sorted(output, key=lambda item: (not item['directory'], item['name'].lower()))

    @staticmethod
    def _parse_unix_listing(output, parent):
        result = []
        for line in output.splitlines():
            parts = line.split(None, 8)
            if len(parts) >= 9 and parts[8] not in ('.', '..'):
                name = parts[8]
                result.append({'name': name, 'path': join(parent, name), 'directory': parts[0].startswith('d'), 'size': int(parts[4]) if parts[4].isdigit() else None, 'modified': ' '.join(parts[5:8])})
        return sorted(result, key=lambda item: (not item['directory'], item['name'].lower()))

    def _request(self, method, path, data=None, headers=None):
        request = urllib.request.Request(self.url(path), data=data, method=method, headers=headers or {})
        credentials = base64.b64encode(f'{self.profile.get("username", "")}:{self.password}'.encode()).decode()
        request.add_header('Authorization', 'Basic ' + credentials)
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.read()

    def _curl(self, arguments, output=None):
        config = tempfile.NamedTemporaryFile(mode='w', delete=False)
        config.write('url = "' + self.url('/').replace('"', '\\"') + '"\n')
        config.write('user = "' + (self.profile.get('username', '') + ':' + self.password).replace('"', '\\"') + '"\n')
        config.close()
        command = ['/usr/bin/curl', '--config', config.name, '--fail', '--silent', '--show-error'] + arguments
        if output:
            command += ['--output', output]
        try:
            return subprocess.check_output(command, stderr=subprocess.STDOUT, timeout=120).decode(errors='replace')
        finally:
            try:
                os.remove(config.name)
            except OSError:
                pass

    def mkdir(self, path):
        if self.protocol == 'SFTP':
            return self._sftp_command(['mkdir ' + self._quote(path)])
        if self.protocol == 'FTP':
            return self._curl(['--url', self.url(posixpath.dirname(path)), '--quote', 'MKD ' + join(self.base, path)])
        return self._request('MKCOL', path)

    def delete(self, path):
        if self.protocol == 'SFTP':
            return self._sftp_command(['rm -r ' + self._quote(path)])
        if self.protocol == 'FTP':
            try:
                children = self.list(path)
            except Exception:
                children = []
            for child in children:
                self.delete(child['path'])
            try:
                return self._curl(['--url', self.url(posixpath.dirname(path)), '--quote', 'DELE ' + join(self.base, path)])
            except subprocess.CalledProcessError:
                return self._curl(['--url', self.url(posixpath.dirname(path)), '--quote', 'RMD ' + join(self.base, path)])
        return self._request('DELETE', path)

    def rename(self, old, new):
        if self.protocol == 'SFTP':
            return self._sftp_command(['rename ' + self._quote(old) + ' ' + self._quote(new)])
        if self.protocol == 'FTP':
            return self._curl(['--url', self.url(posixpath.dirname(old)), '--quote', 'RNFR ' + join(self.base, old), '--quote', 'RNTO ' + join(self.base, new)])
        return self._request('MOVE', old, headers={'Destination': self.url(new), 'Overwrite': 'T'})

    def download(self, item, target):
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if self.protocol == 'SFTP':
            return self._sftp_command(['get -p ' + self._quote(item['path']) + ' ' + self._quote(target)])
        if self.protocol == 'FTP':
            self._curl(['--url', self.url(item['path'])], output=target)
            return
        with open(target, 'wb') as handle:
            handle.write(self._request('GET', item['path']))

    def upload(self, local, directory):
        target = join(directory, os.path.basename(local))
        if self.protocol == 'SFTP':
            return self._sftp_command(['put -p ' + self._quote(local) + ' ' + self._quote(target)])
        if self.protocol == 'FTP':
            return self._curl(['--url', self.url(target), '--upload-file', local])
        with open(local, 'rb') as handle:
            return self._request('PUT', target, handle.read())

    def copy(self, item, directory):
        target = join(directory, item['name'])
        if self.protocol == 'SFTP':
            if item.get('directory'):
                self.mkdir(target)
                for child in self.list(item['path']):
                    self.copy(child, target)
                return
            return self._sftp_command(['cp ' + self._quote(item['path']) + ' ' + self._quote(target)])
        if self.protocol == 'WebDAV':
            return self._request('COPY', item['path'], headers={'Destination': self.url(target), 'Overwrite': 'T'})
        temporary = os.path.join(tempfile.gettempdir(), 'cloudshelf-' + str(uuid.uuid4()))
        try:
            self.download(item, temporary)
            self.upload(temporary, directory)
        finally:
            try:
                os.remove(temporary)
            except OSError:
                pass

    def move(self, item, directory):
        target = join(directory, item['name'])
        if self.protocol == 'SFTP' and not item.get('directory'):
            return self.rename(item['path'], target)
        if self.protocol == 'WebDAV':
            return self.rename(item['path'], target)
        self.copy(item, directory)
        return self.delete(item['path'])
