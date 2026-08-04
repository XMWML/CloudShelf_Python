#!/usr/bin/env python3
import concurrent.futures, json, logging, os, posixpath, shutil, subprocess, tempfile, threading, uuid, urllib.parse, urllib.request, urllib.error, time, email.utils
from datetime import datetime
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, simpledialog
from cloudshelf_core.paths import fmt_size as core_fmt_size, join as core_join, norm as core_norm
from cloudshelf_core.storage import CredentialStore, ProfileStore
from cloudshelf_core.sync import SyncEngine as CoreSyncEngine
from cloudshelf_core.remote import RemoteClient as CoreRemoteClient
from cloudshelf_core.settings import normalized_settings

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    TkBase = TkinterDnD.Tk
except ImportError:
    DND_FILES = None
    TkBase = tk.Tk

APP_DIR = os.path.join(os.path.expanduser('~'), '.cloudshelf')
PROFILE_FILE = os.path.join(APP_DIR, 'connections.json')
SETTINGS_FILE = os.path.join(APP_DIR, 'settings.json')
LOG_FILE = os.path.join(APP_DIR, 'cloudshelf.log')
PREVIEW_DIR = os.path.join(APP_DIR, 'preview')

class TaskCancelled(Exception):
    pass


class TaskControl:
    def __init__(self):
        self.cancelled=False; self.paused=False; self._resume=threading.Event(); self._resume.set()
    def checkpoint(self):
        if self.cancelled: raise TaskCancelled()
        self._resume.wait()
        if self.cancelled: raise TaskCancelled()
    def toggle_pause(self):
        self.paused=not self.paused
        self._resume.set() if not self.paused else self._resume.clear()

def norm(p):
    p = '/' + (p or '').strip().strip('/')
    return posixpath.normpath(p).replace('//', '/')
def join(a, b): return norm(posixpath.join(norm(a), b))
def fmt_size(n):
    if n is None: return '-'
    for unit in ('B','KB','MB','GB','TB'):
        if n < 1024: return f'{n:.0f} {unit}' if unit == 'B' else f'{n:.1f} {unit}'
        n /= 1024
    return f'{n:.1f} PB'

class LegacyRemoteClient:
    def __init__(self, p):
        self.p = p
        self.password = p.get('password', '')
        self.scheme = {'FTP':'ftp', 'SFTP':'sftp', 'WebDAV':'https' if p.get('tls', True) else 'http'}[p['protocol']]
        self.base = norm(p.get('base_path', '/'))
    def url(self, path):
        server_path = join(self.base, path)
        return f'{self.scheme}://{self.p["host"]}:{self.p["port"]}{urllib.parse.quote(server_path)}'
    def auth(self): return (self.p.get('username',''), self.password)
    def list(self, path):
        if self.p['protocol'] == 'SFTP':
            cmd = ['sftp', '-q', '-P', str(self.p['port']), '-o', 'StrictHostKeyChecking=' + ('yes' if self.p.get('host_key_policy')=='strict' else 'accept-new')]
            if self.p.get('private_key'): cmd += ['-i', self.p['private_key']]
            cmd += [f'{self.p.get("username","")}@{self.p["host"]}']
            out = subprocess.check_output(cmd, input=f'ls -la {join(self.base,path)}\n'.encode(), stderr=subprocess.STDOUT, timeout=30).decode(errors='replace')
            result=[]
            for line in out.splitlines():
                parts=line.split(None,8)
                if len(parts)>=9 and parts[8] not in ('.','..'):
                    name=parts[8]; result.append({'name':name,'path':join(path,name),'directory':parts[0].startswith('d'),'size':int(parts[4]) if parts[4].isdigit() else None,'modified':'-'})
            return result
        req=urllib.request.Request(self.url(path), method='PROPFIND' if self.p['protocol']=='WebDAV' else 'GET', headers={'Depth':'1','Content-Type':'application/xml'}, data=(b'<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop><d:displayname/><d:resourcetype/><d:getcontentlength/><d:getlastmodified/></d:prop></d:propfind>' if self.p['protocol']=='WebDAV' else None))
        auth=self.auth(); req.add_header('Authorization','Basic '+__import__('base64').b64encode(f'{auth[0]}:{auth[1]}'.encode()).decode())
        with urllib.request.urlopen(req, timeout=30) as r: data=r.read()
        if self.p['protocol']=='FTP':
            lines=data.decode(errors='replace').splitlines(); out=[]
            for line in lines:
                parts=line.split(None,8)
                if len(parts)>=9 and parts[8] not in ('.','..'):
                    name=parts[8]; out.append({'name':name,'path':join(path,name),'directory':parts[0].startswith('d'),'size':int(parts[4]) if parts[4].isdigit() else None,'modified':' '.join(parts[5:8])})
            return out
        import xml.etree.ElementTree as ET
        root=ET.fromstring(data); out=[]
        for response in root.iter():
            if response.tag.split('}')[-1] != 'response': continue
            href=next((x.text for x in response.iter() if x.tag.split('}')[-1]=='href'), '') or ''
            item_path=urllib.parse.unquote(urllib.parse.urlparse(href).path); item_path=norm(item_path)
            if item_path.rstrip('/') == join(self.base,path).rstrip('/'): continue
            name=posixpath.basename(item_path); isdir=any(x.tag.split('}')[-1]=='collection' for x in response.iter())
            size=next((x.text for x in response.iter() if x.tag.split('}')[-1]=='getcontentlength'), None)
            out.append({'name':name,'path':join(path,name),'directory':isdir,'size':int(size) if size and size.isdigit() else None,'modified':next((x.text for x in response.iter() if x.tag.split('}')[-1]=='getlastmodified'), '-')})
        return sorted(out,key=lambda x:(not x['directory'],x['name'].lower()))
    def sftp_exec(self, commands):
        cmd=['sftp','-q','-P',str(self.p['port']),'-o','StrictHostKeyChecking='+('yes' if self.p.get('host_key_policy')=='strict' else 'accept-new')]
        if self.p.get('private_key'): cmd += ['-i',self.p['private_key']]
        cmd += [f'{self.p.get("username","")}@{self.p["host"]}']; env=os.environ.copy(); secret=None; askpass=None
        if self.p.get('auth','password')=='password' and self.password:
            secret=tempfile.NamedTemporaryFile(mode='w',delete=False); secret.write(self.password); secret.close(); os.chmod(secret.name,0o600)
            askpass=tempfile.NamedTemporaryFile(mode='w',delete=False); askpass.write('#!/bin/sh\ncat "$CLOUDSHELF_ASKPASS_FILE"\n'); askpass.close(); os.chmod(askpass.name,0o700)
            env.update({'CLOUDSHELF_ASKPASS_FILE':secret.name,'SSH_ASKPASS':askpass.name,'SSH_ASKPASS_REQUIRE':'force','DISPLAY':'cloudshelf:0'})
        try: return subprocess.check_output(cmd,input=('\n'.join(commands)+'\n').encode(),stderr=subprocess.STDOUT,timeout=120,env=env).decode(errors='replace')
        finally:
            for f in (secret,askpass):
                if f:
                    try:os.remove(f.name)
                    except OSError:pass
    def request(self, method, path, data=None, headers=None):
        req=urllib.request.Request(self.url(path), data=data, method=method, headers=headers or {})
        import base64; req.add_header('Authorization','Basic '+base64.b64encode(f'{self.p.get("username","")}:{self.password}'.encode()).decode())
        with urllib.request.urlopen(req,timeout=30) as r: return r.read()
    def mkdir(self, path):
        if self.p['protocol']=='SFTP': return self.sftp_exec(['mkdir '+self.quote(path)])
        return self.request('MKCOL',path)
    def delete(self, path):
        if self.p['protocol']=='SFTP': return self.sftp_exec(['rm -r '+self.quote(path)])
        return self.request('DELETE',path)
    def rename(self, old, new):
        if self.p['protocol']=='SFTP': return self.sftp_exec(['rename '+self.quote(old)+' '+self.quote(new)])
        return self.request('MOVE',old,headers={'Destination':self.url(new),'Overwrite':'T'})
    def download(self, item, target):
        os.makedirs(os.path.dirname(target),exist_ok=True)
        if self.p['protocol']=='SFTP': return self.sftp_exec(['get -p '+self.quote(item['path'])+' '+self.quote(target)])
        open(target,'wb').write(self.request('GET',item['path']))
    def upload(self, local, directory):
        if self.p['protocol']=='SFTP': return self.sftp_exec(['put -p '+self.quote(local)+' '+self.quote(join(directory,os.path.basename(local)))])
        with open(local,'rb') as f: return self.request('PUT',join(directory,os.path.basename(local)),f.read())
    def quote(self,value): return '"'+str(value).replace('\\','\\\\').replace('"','\\"')+'"'
    def copy(self, item, directory):
        target=join(directory,item['name'])
        if self.p['protocol']=='SFTP':
            self.sftp_exec(['cp '+self.quote(item['path'])+' '+self.quote(target)]); return
        if self.p['protocol']=='WebDAV':
            self.request('COPY',item['path'],headers={'Destination':self.url(target),'Overwrite':'T'}); return
        if item['directory']:
            self.mkdir(target)
            for child in self.list(item['path']): self.copy(child,target)
        else:
            tmp=os.path.join(APP_DIR,'copy-'+str(uuid.uuid4()))
            try: self.download(item,tmp); self.upload(tmp,directory)
            finally:
                if os.path.exists(tmp): os.remove(tmp)
    def move(self, item, directory):
        target=join(directory,item['name'])
        if self.p['protocol']=='SFTP':
            self.sftp_exec(['rename '+self.quote(item['path'])+' '+self.quote(target)]); return
        if self.p['protocol']=='WebDAV':
            self.request('MOVE',item['path'],headers={'Destination':self.url(target),'Overwrite':'T'}); return
        if item['directory']:
            self.copy(item,directory); self.delete(item['path'])
        else: self.rename(item['path'],target)

class LegacySyncEngine:
    def __init__(self, client, rule, progress=None):
        self.client,self.rule,self.progress=client,rule,progress or (lambda _:None)
    def local_inventory(self):
        root=os.path.abspath(self.rule['local']); out={}
        for base,dirs,files in os.walk(root):
            rel=os.path.relpath(base,root)
            if rel!='.': out[rel]={'directory':True}
            for name in files:
                p=os.path.join(base,name); out[os.path.relpath(p,root)]={'directory':False,'size':os.path.getsize(p),'mtime':os.path.getmtime(p),'path':p}
        return out
    def remote_inventory(self, folder):
        out={}
        def visit(path):
            for item in self.client.list(path):
                rel=posixpath.relpath(item['path'],folder); out[rel]={'item':item,'directory':item['directory'],'size':item.get('size')}
                if item['directory']: visit(item['path'])
        visit(folder); return out
    def run(self):
        root=os.path.abspath(self.rule['local']); os.makedirs(root,exist_ok=True); remote_root=norm(self.rule['remote'])
        local=self.local_inventory(); remote=self.remote_inventory(remote_root); report={'uploaded':0,'downloaded':0,'deleted_local':0,'deleted_remote':0}
        previous=self.rule.get('snapshot',{'local':[],'remote':[]})
        if self.rule.get('delete_remote'):
            for rel in set(previous['local'])-set(local):
                if rel in remote: self.client.delete(remote[rel]['item']['path']); report['deleted_remote']+=1
        if self.rule.get('delete_local'):
            for rel in set(previous['remote'])-set(remote):
                p=os.path.join(root,rel)
                if os.path.exists(p): shutil.rmtree(p) if os.path.isdir(p) else os.remove(p); report['deleted_local']+=1
        upload=self.rule.get('upload',True); download=self.rule.get('download',False)
        if self.rule.get('conflict')=='keep_remote': upload=False
        if self.rule.get('conflict')=='keep_local': download=False
        if upload:
            for rel,data in local.items():
                if data['directory']: continue
                remote_item=remote.get(rel); remote_time=0
                if remote_item:
                    try: remote_time=email.utils.parsedate_to_datetime(remote_item['item'].get('modified','')).timestamp()
                    except (TypeError,ValueError,OverflowError): remote_time=0
                if rel not in remote or remote_item['size']!=data['size'] or (self.rule.get('conflict')=='keep_newest' and data.get('mtime',0)>remote_time):
                    parent=posixpath.dirname(rel); current=remote_root
                    for part in [x for x in parent.split('/') if x]:
                        current=join(current,part)
                        try:self.client.mkdir(current)
                        except Exception:pass
                    self.client.upload(data['path'],join(remote_root,parent)); report['uploaded']+=1; self.progress('上传 '+rel)
        if download:
            for rel,data in remote.items():
                if data['directory']: continue
                p=os.path.join(root,rel); os.makedirs(os.path.dirname(p),exist_ok=True); local_time=os.path.getmtime(p) if os.path.exists(p) else 0; remote_time=0
                try: remote_time=email.utils.parsedate_to_datetime(data['item'].get('modified','')).timestamp()
                except (TypeError,ValueError,OverflowError): pass
                newer_remote=self.rule.get('conflict')=='keep_newest' and remote_time>local_time
                if not os.path.exists(p) or os.path.getsize(p)!=data['size'] or newer_remote:
                    self.client.download(data['item'],p); report['downloaded']+=1; self.progress('下载 '+rel)
        self.rule['snapshot']={'local':list(self.local_inventory()),'remote':list(self.remote_inventory(remote_root))}; self.rule['last_sync']=time.time(); return report

norm = core_norm
join = core_join
fmt_size = core_fmt_size
SyncEngine = CoreSyncEngine
RemoteClient = CoreRemoteClient


class App(TkBase):
    def __init__(self):
        super().__init__(); self.title('CloudShelf'); self.geometry('1240x760'); self.minsize(980,620)
        self.profiles=[]; self.session=None; self.path='/'; self.history=['/']; self.history_index=0; self.items=[]; self.transfers=[]; self.jobs={}; self.retry_jobs={}; self.progress_stats={}; self.active_sync_rules=set(); self.clipboard=[]; self.clipboard_mode='copy'; self.checked_items=set(); self.connection_states={}; self.local_fingerprints={}; self.sync_scan_busy=False; self.activity_count=0; self.load_profiles(); self.load_settings(); os.makedirs(PREVIEW_DIR,exist_ok=True); logging.basicConfig(filename=LOG_FILE,level=logging.INFO,format='%(asctime)s %(levelname)s'); self.executor=concurrent.futures.ThreadPoolExecutor(max_workers=self.settings['max_workers'])
        self.language=self.settings['language']; self.protocol('WM_DELETE_WINDOW',self.close_app); self.build_ui(); self.localize_widgets(); self.refresh_profiles(); self.after(100,self.auto_connect); self.after(5000,self.auto_sync)
    def load_profiles(self):
        self.profiles=ProfileStore(PROFILE_FILE).load()
    def save_profiles(self):
        ProfileStore(PROFILE_FILE).save(self.profiles)
    def load_settings(self):
        try:
            with open(SETTINGS_FILE, encoding='utf-8') as handle:
                self.settings=normalized_settings(json.load(handle))
        except (OSError,ValueError,TypeError): self.settings=normalized_settings({})
        self.auto_sync_enabled=self.settings['automatic_sync']; self.max_workers=self.settings['max_workers']
    def save_settings(self):
        os.makedirs(APP_DIR,exist_ok=True)
        self.settings['automatic_sync']=self.auto_sync_enabled; self.settings['max_workers']=self.max_workers
        temporary=SETTINGS_FILE + '.tmp'
        with open(temporary,'w',encoding='utf-8') as handle: json.dump(self.settings,handle,ensure_ascii=False,indent=2)
        os.replace(temporary,SETTINGS_FILE)
    def translate(self, value):
        if self.language != 'en': return value
        return {
            '新建连接':'New Connection', '编辑连接':'Edit Connection', '删除连接':'Delete Connection', '连接/断开':'Connect/Disconnect',
            '同步管理':'Sync Manager', '设置':'Settings', '自动同步：开/关':'Auto Sync: On/Off', '后退':'Back', '前进':'Forward',
            '上级目录':'Up', '刷新':'Refresh', '新建文件夹':'New Folder', '上传':'Upload', '下载':'Download', '复制到':'Copy To',
            '移动到':'Move To', '重命名':'Rename', '删除':'Delete', '连接':'Connections', '协议':'Protocol', '状态':'Status',
            '名称':'Name', '选择':'Select', '大小':'Size', '类型':'Type', '修改时间':'Modified', '文本预览':'Text Preview',
            '传输任务':'Transfers', '清除已结束':'Clear Finished', '全部开始':'Start All', '全部停止':'Pause All', '重试失败':'Retry Failed',
            '暂停/继续':'Pause/Resume', '取消任务':'Cancel Task', '项目':'Item', '文件夹':'Folder', '文件':'File'
        }.get(value,value)
    def localize_widgets(self, parent=None):
        parent=parent or self
        for child in parent.winfo_children():
            try:
                text=child.cget('text')
                if text: child.configure(text=self.translate(text))
            except tk.TclError: pass
            self.localize_widgets(child)
    def settings_dialog(self):
        w=tk.Toplevel(self); w.title('Settings'); w.transient(self); w.grab_set(); tabs=ttk.Notebook(w); tabs.pack(fill='both',expand=True,padx=10,pady=10)
        preview=ttk.Frame(tabs,padding=8); transfer=ttk.Frame(tabs,padding=8); sync=ttk.Frame(tabs,padding=8); tabs.add(preview,text='Preview'); tabs.add(transfer,text='Transfers'); tabs.add(sync,text='Sync')
        enabled=tk.BooleanVar(value=self.settings['preview_enabled']); extensions=tk.StringVar(value=self.settings['preview_extensions']); maximum=tk.IntVar(value=self.settings['preview_max_bytes']//1024); language=tk.StringVar(value=self.settings['language'])
        ttk.Checkbutton(preview,text='Enable text preview',variable=enabled).grid(row=0,column=0,columnspan=2,sticky='w',pady=4)
        ttk.Label(preview,text='Text extensions').grid(row=1,column=0,sticky='w',pady=4); ttk.Entry(preview,textvariable=extensions,width=46).grid(row=1,column=1,sticky='ew',pady=4)
        ttk.Label(preview,text='Maximum preview size (KB)').grid(row=2,column=0,sticky='w',pady=4); ttk.Spinbox(preview,from_=1,to=102400,textvariable=maximum,width=10).grid(row=2,column=1,sticky='w',pady=4)
        ttk.Label(preview,text='Language').grid(row=3,column=0,sticky='w',pady=4); ttk.Combobox(preview,textvariable=language,values=('system','zh','en'),state='readonly',width=12).grid(row=3,column=1,sticky='w',pady=4)
        workers=tk.IntVar(value=self.max_workers); download_dir=tk.StringVar(value=self.settings['download_directory']); ask_destination=tk.BooleanVar(value=self.settings['ask_download_destination'])
        automatic_connect=tk.StringVar(value=self.settings['automatic_connect_profile_id'])
        ttk.Label(transfer,text='Concurrent tasks').grid(row=0,column=0,sticky='w',pady=4); ttk.Spinbox(transfer,from_=1,to=8,textvariable=workers,width=8).grid(row=0,column=1,sticky='w',pady=4)
        ttk.Checkbutton(transfer,text='Ask for a destination on every download',variable=ask_destination).grid(row=1,column=0,columnspan=2,sticky='w',pady=4)
        ttk.Label(transfer,text='Default download directory').grid(row=2,column=0,sticky='w',pady=4); ttk.Entry(transfer,textvariable=download_dir,width=42).grid(row=2,column=1,sticky='ew',pady=4)
        ttk.Label(transfer,text='Connect on launch').grid(row=3,column=0,sticky='w',pady=4)
        choices={'Do not connect automatically': ''}
        choices.update({f"{profile['name']} ({profile['protocol']})": str(profile['id']) for profile in self.profiles})
        automatic_connect_label=tk.StringVar(value=next((name for name,profile_id in choices.items() if profile_id == automatic_connect.get()), 'Do not connect automatically'))
        ttk.Combobox(transfer,textvariable=automatic_connect_label,values=list(choices),state='readonly',width=42).grid(row=3,column=1,sticky='ew',pady=4)
        ttk.Label(sync,text='Manage rules for the selected connection.').pack(anchor='w',pady=4); ttk.Button(sync,text='Open Sync Manager',command=self.sync_manager).pack(anchor='w')
        def save():
            try:
                self.max_workers=max(1,min(8,workers.get())); self.settings.update({'preview_enabled':enabled.get(),'preview_extensions':extensions.get(),'preview_max_bytes':maximum.get()*1024,'language':language.get(),'download_directory':download_dir.get().strip(),'ask_download_destination':ask_destination.get(),'automatic_connect_profile_id':choices[automatic_connect_label.get()]}); self.language=language.get()
            except tk.TclError: messagebox.showerror('Invalid settings','Task count and preview size must be numbers.',parent=w); return
            self.save_settings(); w.destroy()
        ttk.Button(w,text='Save',command=save).pack(anchor='e',padx=10,pady=(0,10))
    def build_ui(self):
        bar=ttk.Frame(self,padding=6); bar.pack(fill='x')
        for text,cmd in [('新建连接',self.add_profile),('编辑连接',self.edit_profile),('删除连接',self.delete_profile),('连接/断开',self.toggle_connection),('同步管理',self.sync_manager),('设置',self.settings_dialog),('自动同步：开/关',self.toggle_auto_sync),('后退',self.go_back),('前进',self.go_forward),('上级目录',self.go_up),('刷新',self.refresh),('新建文件夹',self.mkdir),('上传',self.upload),('下载',self.download),('复制到',self.copy),('移动到',self.move),('重命名',self.rename),('删除',self.delete)]: ttk.Button(bar,text=text,command=cmd).pack(side='left',padx=2)
        for sequence,command in [('<Control-a>',self.toggle_select_all),('<Command-a>',self.toggle_select_all),('<Control-c>',self.copy_selection),('<Command-c>',self.copy_selection),('<Control-x>',self.cut_selection),('<Command-x>',self.cut_selection),('<Control-v>',self.paste_selection),('<Command-v>',self.paste_selection),('<Control-i>',self.properties),('<Command-i>',self.properties),('<Control-r>',self.refresh),('<Command-r>',self.refresh)]: self.bind_all(sequence,lambda _,command=command:(command(),'break')[1])
        pan=ttk.PanedWindow(self,orient='horizontal'); pan.pack(fill='both',expand=True,padx=6,pady=4)
        left=ttk.Frame(pan,width=270); right=ttk.Frame(pan); pan.add(left,weight=1); pan.add(right,weight=4)
        ttk.Label(left,text='连接').pack(anchor='w'); self.conn=ttk.Treeview(left,columns=('protocol','state'),show='tree headings',selectmode='browse'); self.conn.heading('#0',text='连接'); self.conn.heading('protocol',text='协议'); self.conn.heading('state',text='状态'); self.conn.column('protocol',width=70); self.conn.column('state',width=70); self.conn.pack(fill='both',expand=True); self.conn.bind('<<TreeviewSelect>>',self.select_profile); self.conn.bind('<Button-3>',self.connection_menu)
        head=ttk.Frame(right); head.pack(fill='x'); self.status=tk.StringVar(value='未选择连接'); ttk.Label(head,textvariable=self.status).pack(side='left'); self.activity=tk.StringVar(value=''); ttk.Label(head,textvariable=self.activity,foreground='#666666').pack(side='left',padx=8); self.activity_bar=ttk.Progressbar(head,mode='indeterminate',length=72); self.path_var=tk.StringVar(value='/'); ttk.Label(head,textvariable=self.path_var).pack(side='right')
        self.files=ttk.Treeview(right,columns=('checked','size','type','modified'),show='tree headings',selectmode='extended'); self.files.heading('#0',text='名称'); self.files.heading('checked',text='选择'); self.files.heading('size',text='大小'); self.files.heading('type',text='类型'); self.files.heading('modified',text='修改时间'); self.files.column('#0',width=390); self.files.column('checked',width=52,anchor='center'); self.files.column('size',width=100); self.files.column('type',width=100); self.files.pack(fill='both',expand=True); self.files.bind('<Double-1>',self.open_item); self.files.bind('<Button-3>',self.context_menu); self.files.bind('<Button-1>',self.file_click); self.files.bind('<<TreeviewSelect>>',self.preview_selected)
        if DND_FILES:
            self.files.drop_target_register(DND_FILES); self.files.dnd_bind('<<Drop>>',self.drop_upload)
        self.preview_frame=ttk.LabelFrame(right,text='文本预览'); self.preview_text=tk.Text(self.preview_frame,height=9,wrap='word',state='disabled'); self.preview_text.pack(fill='both',expand=True,padx=4,pady=4); self.preview_frame.pack(fill='x',pady=(5,0))
        transfer_head=ttk.Frame(self); transfer_head.pack(fill='x',padx=8); ttk.Label(transfer_head,text='传输任务').pack(side='left'); ttk.Button(transfer_head,text='清除已结束',command=self.clear_finished_transfers).pack(side='right',padx=3); ttk.Button(transfer_head,text='全部开始',command=self.start_all_transfers).pack(side='right',padx=3); ttk.Button(transfer_head,text='全部停止',command=self.pause_all_transfers).pack(side='right',padx=3); ttk.Button(transfer_head,text='重试失败',command=self.retry_failed_transfers).pack(side='right',padx=3); ttk.Button(transfer_head,text='暂停/继续',command=self.pause_task).pack(side='right',padx=3); ttk.Button(transfer_head,text='取消任务',command=self.cancel_task).pack(side='right'); self.transfer=ttk.Treeview(self,columns=('state','progress'),show='tree headings',height=6); self.transfer.heading('#0',text='项目'); self.transfer.heading('state',text='状态'); self.transfer.heading('progress',text='进度'); self.transfer.pack(fill='x',padx=6,pady=(0,6))
    def refresh_profiles(self):
        self.conn.delete(*self.conn.get_children()); [self.conn.insert('', 'end', iid=str(p['id']), text=p['name'], values=(p['protocol'],self.connection_states.get(str(p['id']),'未连接'))) for p in self.profiles]
    def auto_connect(self):
        identifier=self.settings.get('automatic_connect_profile_id','')
        if identifier and any(str(profile['id']) == identifier for profile in self.profiles):
            self.conn.selection_set(identifier); self.select_profile()
    def set_activity(self, text=None):
        if text:
            self.activity_count += 1; self.activity.set(text)
            if not self.activity_bar.winfo_manager(): self.activity_bar.pack(side='left')
            self.activity_bar.start(10)
        else:
            self.activity_count=max(0,self.activity_count-1)
            if not self.activity_count:
                self.activity.set(''); self.activity_bar.stop(); self.activity_bar.pack_forget()
    def select_profile(self,_=None):
        sel=self.conn.selection();
        if not sel:return
        profile=next(p for p in self.profiles if str(p['id'])==sel[0]); self.connection_states[sel[0]]='连接中'; self.conn.item(sel[0],values=(profile['protocol'],'连接中'))
        self.session=RemoteClient(profile); self.path='/'; self.history=['/']; self.history_index=0; self.status.set(f'{self.session.p["name"]}  |  {self.session.p["protocol"]}  |  已连接'); self.connection_states[sel[0]]='已连接'; self.conn.item(sel[0],values=(profile['protocol'],'已连接')); self.refresh()
    def toggle_connection(self):
        selected=self.conn.selection()
        if not selected: return
        identifier=selected[0]
        if self.session and str(self.session.p.get('id')) == identifier:
            self.session=None; self.items=[]; self.checked_items.clear(); self.files.delete(*self.files.get_children()); self.status.set('已断开连接'); self.connection_states[identifier]='未连接'; profile=next(p for p in self.profiles if str(p['id'])==identifier); self.conn.item(identifier,values=(profile['protocol'],'未连接')); return
        self.select_profile()
    def toggle_auto_sync(self):
        self.auto_sync_enabled=not self.auto_sync_enabled; self.save_settings(); messagebox.showinfo('自动同步', '已启用' if self.auto_sync_enabled else '已停用', parent=self)
    def connection_menu(self,event):
        iid=self.conn.identify_row(event.y)
        if iid:self.conn.selection_set(iid)
        menu=tk.Menu(self,tearoff=0); menu.add_command(label='连接/断开',command=self.toggle_connection); menu.add_command(label='编辑',command=self.edit_profile); menu.add_command(label='删除',command=self.delete_profile); menu.post(event.x_root,event.y_root)
    def worker(self, fn, ok=None, iid=None, activity=None):
        if activity: self.set_activity(activity)
        def run():
            try: result=fn(); self.after(0,lambda:(ok(result) if ok else None, self.set_activity() if activity else None))
            except TaskCancelled: self.after(0,lambda:(self.update_transfer(iid,'已取消','-') if iid else None, self.set_activity() if activity else None))
            except Exception as e: logging.exception('Background task failed'); self.after(0,lambda:(self.update_transfer(iid,'失败','-') if iid else None, messagebox.showerror('操作失败',str(e)), self.set_activity() if activity else None))
        self.executor.submit(run)
    def close_app(self):
        self.executor.shutdown(wait=False,cancel_futures=True); self.destroy()
    def refresh(self):
        if not self.session:return
        self.worker(lambda:self.session.list(self.path),self.show_items,activity='正在加载文件夹…')
    def show_items(self,items):
        self.items=items; self.checked_items.clear(); self.files.delete(*self.files.get_children());
        if self.path!='/': self.files.insert('',0,iid='__up__',text='..',values=('', '-', '上级目录','-'))
        for i,x in enumerate(items): self.files.insert('', 'end', iid=str(i), text=('📁 ' if x['directory'] else '📄 ')+x['name'],values=('☐',fmt_size(x['size']), '文件夹' if x['directory'] else '文件',x['modified']))
        self.path_var.set(self.path)
    def open_item(self,_=None):
        sel=self.files.selection();
        if not sel:return
        if sel[0]=='__up__': self.go_up(); return
        x=self.items[int(sel[0])]
        if x['directory']: self.open_path(x['path'])
    def open_path(self,path):
        if self.history_index < len(self.history)-1:self.history=self.history[:self.history_index+1]
        self.history.append(norm(path)); self.history_index+=1; self.path=norm(path); self.refresh()
    def go_back(self):
        if self.history_index>0:self.history_index-=1; self.path=self.history[self.history_index]; self.refresh()
    def go_forward(self):
        if self.history_index+1<len(self.history):self.history_index+=1; self.path=self.history[self.history_index]; self.refresh()
    def go_up(self):
        if self.session and self.path!='/': self.open_path(posixpath.dirname(self.path) or '/')
    def file_click(self,event):
        identifier=self.files.identify_row(event.y)
        if identifier and identifier != '__up__' and self.files.identify_column(event.x) == '#1':
            if identifier in self.checked_items: self.checked_items.remove(identifier)
            else: self.checked_items.add(identifier)
            values=list(self.files.item(identifier,'values')); values[0]='☑' if identifier in self.checked_items else '☐'; self.files.item(identifier,values=values)
            return 'break'
    def toggle_select_all(self):
        identifiers={str(index) for index in range(len(self.items))}
        self.checked_items=set() if self.checked_items == identifiers else identifiers
        for identifier in identifiers:
            values=list(self.files.item(identifier,'values')); values[0]='☑' if identifier in self.checked_items else '☐'; self.files.item(identifier,values=values)
    def selected(self):
        identifiers=self.checked_items or {item for item in self.files.selection() if item!='__up__'}
        return [self.items[int(identifier)] for identifier in identifiers]
    def mkdir(self):
        if not self.session:return
        name=simpledialog.askstring('新建文件夹','文件夹名称：',parent=self); 
        if name:self.worker(lambda:self.session.mkdir(join(self.path,name)),lambda _:self.refresh())
    def rename(self):
        xs=self.selected();
        if len(xs)!=1:return
        name=simpledialog.askstring('重命名','新名称：',initialvalue=xs[0]['name'],parent=self)
        if name:self.worker(lambda:self.session.rename(xs[0]['path'],join(self.path,name)),lambda _:self.refresh())
    def delete(self):
        xs=self.selected();
        if xs and messagebox.askyesno('确认删除',f'确定删除 {len(xs)} 个项目？',parent=self): self.worker(lambda:[self.session.delete(x['path']) for x in xs],lambda _:self.refresh())
    def copy_selection(self): self.clipboard=self.selected(); self.clipboard_mode='copy'
    def cut_selection(self): self.clipboard=self.selected(); self.clipboard_mode='move'
    def paste_selection(self):
        for item in self.clipboard:
            fn=self.session.copy if self.clipboard_mode=='copy' else self.session.move
            self.worker(lambda item=item,fn=fn:fn(item,self.path),lambda _:self.refresh())
        self.clipboard=[]
    def properties(self):
        xs=self.selected()
        if len(xs)==1:
            x=xs[0]
            if not x['directory']:
                messagebox.showinfo('属性',f'名称：{x["name"]}\n类型：文件\n远端路径：{x["path"]}\n大小：{fmt_size(x.get("size"))}\n修改时间：{x.get("modified","-")}',parent=self)
                return
            messagebox.showinfo('属性',f'名称：{x["name"]}\n类型：文件夹\n远端路径：{x["path"]}\n大小：正在计算…\n修改时间：{x.get("modified","-")}',parent=self)
            self.worker(lambda:self.remote_folder_size(x['path']),lambda size:messagebox.showinfo('属性',f'名称：{x["name"]}\n类型：文件夹\n远端路径：{x["path"]}\n大小：{fmt_size(size)}\n修改时间：{x.get("modified","-")}',parent=self))
    def remote_folder_size(self,path):
        total=0
        for item in self.session.list(path):
            total += self.remote_folder_size(item['path']) if item['directory'] else (item.get('size') or 0)
        return total
    def upload(self):
        if not self.session:return
        paths=filedialog.askopenfilenames(parent=self,title='选择要上传的文件')
        for p in paths:
            iid=self.add_transfer(os.path.basename(p),'上传中','0%'); self.jobs[iid]=TaskControl()
            self.retry_jobs[iid]=lambda new_iid,event,p=p:self.upload_path(p,new_iid,event)
            self.worker(lambda p=p,iid=iid:self.upload_path(p,iid,self.jobs[iid]),lambda _,iid=iid:self.update_transfer(iid,'上传完成','100%'),iid)
        folder=filedialog.askdirectory(parent=self,title='或选择要上传的文件夹')
        if folder:
            iid=self.add_transfer(os.path.basename(folder),'上传中','0%'); self.jobs[iid]=TaskControl(); self.worker(lambda iid=iid:self.upload_path(folder,iid,self.jobs[iid]),lambda _,iid=iid:self.update_transfer(iid,'文件夹上传完成','100%'),iid)
            self.retry_jobs[iid]=lambda new_iid,event,folder=folder:self.upload_path(folder,new_iid,event)
    def drop_upload(self,event):
        if not self.session:return
        for path in self.tk.splitlist(event.data):
            iid=self.add_transfer(os.path.basename(path),'上传中','0%'); self.jobs[iid]=TaskControl()
            self.retry_jobs[iid]=lambda new_iid,event,path=path:self.upload_path(path,new_iid,event)
            self.worker(lambda path=path,iid=iid:self.upload_path(path,iid,self.jobs[iid]),lambda _,iid=iid:self.update_transfer(iid,'上传完成','100%'),iid)
    def upload_path(self,path,iid=None,cancel=None):
        if cancel: cancel.checkpoint()
        if not os.path.isdir(path):
            result=self.session.upload(path,self.path,progress=(lambda done,total:self.after(0,lambda:self.report_progress(iid,'上传中',done,total)) if iid and total else None))
            if iid:self.after(0,lambda:self.update_transfer(iid,'上传中','100%'))
            return result
        target=join(self.path,os.path.basename(path))
        try:self.session.mkdir(target)
        except Exception:pass
        for root,dirs,files in os.walk(path):
            rel=os.path.relpath(root,path); remote=target if rel=='.' else join(target,rel)
            for name in dirs:
                try:self.session.mkdir(join(remote,name))
                except Exception:pass
            for name in files:
                if cancel: cancel.checkpoint()
                file_path=os.path.join(root,name); self.session.upload(file_path,remote,progress=(lambda done,total:self.after(0,lambda:self.report_progress(iid,'上传中',done,total)) if iid and total else None))
                if iid:self.after(0,lambda name=name:self.update_transfer(iid,'上传中',name))
    def download(self):
        xs=self.selected();
        if not self.session or not xs:return
        configured=self.settings.get('download_directory','')
        dest=filedialog.askdirectory(parent=self,title='选择下载目录',initialdir=configured or None) if self.settings.get('ask_download_destination',True) or not configured else configured
        if dest:
            for x in xs:
                iid=self.add_transfer(x['name'],'下载中','0%'); self.jobs[iid]=TaskControl(); self.retry_jobs[iid]=lambda new_iid,event,x=x,dest=dest:self.download_path(x,dest,new_iid,event); self.worker(lambda x=x,iid=iid:self.download_path(x,dest,iid,self.jobs[iid]),lambda _,iid=iid:self.update_transfer(iid,'下载完成','100%'),iid)
    def download_path(self,item,dest,iid=None,cancel=None):
        if cancel: cancel.checkpoint()
        target=os.path.join(dest,item['name'])
        if item['directory']:
            os.makedirs(target,exist_ok=True)
            for child in self.session.list(item['path']):self.download_path(child,target,iid,cancel)
        else:
            self.session.download(item,target,progress=(lambda done,total: self.after(0,lambda:self.report_progress(iid,'下载中',done,total)) if iid else None))
            if iid:self.after(0,lambda:self.update_transfer(iid,'下载中','文件完成'))
    def destination(self,title): return simpledialog.askstring(title,'目标远端目录：',initialvalue=self.path,parent=self)
    def copy(self):
        xs=self.selected(); dest=self.destination('复制到') if xs else None
        if dest:
            for x in xs:self.worker(lambda x=x:self.session.copy(x,norm(dest)),lambda _,name=x['name']:self.add_transfer(name,'复制完成'))
    def move(self):
        xs=self.selected(); dest=self.destination('移动到') if xs else None
        if dest:
            for x in xs:self.worker(lambda x=x:self.session.move(x,norm(dest)),lambda _,name=x['name']:self.add_transfer(name,'移动完成'))
    def add_transfer(self,title,state,progress='-'):
        iid=str(uuid.uuid4()); self.transfer.insert('',0,iid=iid,text=title,values=(state,progress)); return iid
    def update_transfer(self,iid,state,progress):
        if self.transfer.exists(iid): self.transfer.item(iid,values=(state,progress))
    def report_progress(self,iid,state,done,total):
        now=time.monotonic(); previous=self.progress_stats.get(iid); rate=0
        if previous:
            elapsed=now-previous[0]
            if elapsed>0: rate=max(0,done-previous[1])/elapsed
        self.progress_stats[iid]=(now,done)
        percent=f'{done/total:.0%}' if total else fmt_size(done)
        self.update_transfer(iid,state,f'{percent} · {fmt_size(rate)}/s' if rate else percent)
    def cancel_task(self):
        for iid in self.transfer.selection():
            task=self.jobs.get(iid)
            if task and task.paused:
                task.cancelled=True; task._resume.set(); self.update_transfer(iid,'取消中','-')
    def pause_task(self):
        for iid in self.transfer.selection():
            task=self.jobs.get(iid)
            if task:
                task.toggle_pause(); self.update_transfer(iid,'已暂停' if task.paused else '继续中','-')
    def retry_task(self):
        for old_iid in self.transfer.selection():
            starter=self.retry_jobs.get(old_iid)
            if not starter: continue
            title=self.transfer.item(old_iid,'text'); new_iid=self.add_transfer(title,'重试中','0%'); self.jobs[new_iid]=TaskControl(); self.retry_jobs[new_iid]=starter
            self.worker(lambda new_iid=new_iid:self.retry_jobs[new_iid](new_iid,self.jobs[new_iid]),lambda _,new_iid=new_iid:self.update_transfer(new_iid,'完成','100%'),new_iid)
    def pause_all_transfers(self):
        self.transfer.selection_set(self.transfer.get_children()); self.pause_task()
    def start_all_transfers(self):
        for identifier, task in self.jobs.items():
            if task.paused: task.toggle_pause(); self.update_transfer(identifier,'继续中','-')
    def retry_failed_transfers(self):
        failed=[identifier for identifier in self.transfer.get_children() if self.transfer.item(identifier,'values')[0] == '失败']
        self.transfer.selection_set(failed); self.retry_task()
    def clear_finished_transfers(self):
        for identifier in self.transfer.get_children():
            if self.transfer.item(identifier,'values')[0] in ('完成','上传完成','下载完成','文件夹上传完成','已取消','失败'):
                self.transfer.delete(identifier); self.jobs.pop(identifier,None); self.retry_jobs.pop(identifier,None); self.progress_stats.pop(identifier,None)
    def preview_selected(self,_=None):
        if not self.settings.get('preview_enabled',True): return
        selected=self.selected()
        if len(selected) != 1 or selected[0]['directory']:
            self.set_preview('选择一个文本文件以预览。'); return
        item=selected[0]
        suffix=os.path.splitext(item['name'])[1].lower().lstrip('.')
        allowed={part.strip().lstrip('.').lower() for part in self.settings.get('preview_extensions','').split(',') if part.strip()}
        if suffix not in allowed:
            self.set_preview('此文件类型未启用文本预览。'); return
        if item.get('size') is not None and item['size'] > self.settings['preview_max_bytes']:
            self.set_preview('文件超过预览大小限制。'); return
        target=os.path.join(PREVIEW_DIR,str(uuid.uuid4())+'-'+os.path.basename(item['name']))
        self.set_preview('正在下载预览…')
        self.worker(lambda:self.session.download(item,target),lambda _:self.show_preview_file(target),activity='正在下载预览…')
    def show_preview_file(self,path):
        try:
            with open(path,'rb') as handle: data=handle.read(self.settings['preview_max_bytes']+1)
            self.set_preview(data.decode('utf-8',errors='replace'))
        except OSError as error: self.set_preview('无法加载预览：'+str(error))
        finally:
            try: os.remove(path)
            except OSError: pass
    def set_preview(self,text):
        self.preview_text.configure(state='normal'); self.preview_text.delete('1.0','end'); self.preview_text.insert('1.0',text); self.preview_text.configure(state='disabled')
    def context_menu(self,e):
        iid=self.files.identify_row(e.y)
        if iid and iid!='__up__': self.files.selection_set(iid)
        m=tk.Menu(self,tearoff=0); m.add_command(label='打开',command=self.open_item); m.add_command(label='下载',command=self.download); m.add_command(label='复制',command=self.copy_selection); m.add_command(label='剪切',command=self.cut_selection); m.add_command(label='粘贴',command=self.paste_selection); m.add_command(label='复制到',command=self.copy); m.add_command(label='移动到',command=self.move); m.add_command(label='重命名',command=self.rename); m.add_command(label='属性',command=self.properties); m.add_command(label='删除',command=self.delete); m.post(e.x_root,e.y_root)
    def add_profile(self): self.profile_dialog()
    def delete_profile(self):
        sel=self.conn.selection()
        if not sel:return
        p=next(p for p in self.profiles if str(p['id'])==sel[0])
        if messagebox.askyesno('删除连接',f'确定删除连接“{p["name"]}”？',parent=self):
            CredentialStore.delete(p['id']); self.profiles.remove(p); self.session=None
            if self.settings.get('automatic_connect_profile_id') == str(p['id']): self.settings['automatic_connect_profile_id']=''; self.save_settings()
            self.save_profiles(); self.refresh_profiles(); self.files.delete(*self.files.get_children()); self.status.set('未选择连接')
    def edit_profile(self):
        sel=self.conn.selection();
        if sel:self.profile_dialog(next(p for p in self.profiles if str(p['id'])==sel[0]))
    def sync_manager(self):
        if not self.session:return
        w=tk.Toplevel(self); w.title('同步管理'); w.geometry('820x400'); w.transient(self)
        tree=ttk.Treeview(w,columns=('remote','mode','state','last'),show='headings');
        for c,t in [('remote','远端目录'),('mode','同步操作'),('state','状态'),('last','上次同步')]: tree.heading(c,text=t)
        tree.pack(fill='both',expand=True,padx=8,pady=8)
        rules=self.session.p.setdefault('sync_rules',[])
        def reload():
            tree.delete(*tree.get_children())
            for i,r in enumerate(rules): tree.insert('', 'end',iid=str(i),values=(r.get('remote','/'),('上传' if r.get('upload',True) else '')+(' 下载' if r.get('download') else ''), '已启用' if r.get('enabled',True) else '已停用', time.strftime('%Y-%m-%d %H:%M',time.localtime(r['last_sync'])) if r.get('last_sync') else '从未'))
        def edit():
            sel=tree.selection(); old=rules[int(sel[0])] if sel else None; self.rule_dialog(rules,old,reload)
        def run():
            sel=tree.selection();
            if sel:self.run_rule(rules[int(sel[0])],reload)
        def toggle():
            sel=tree.selection();
            if sel: rules[int(sel[0])]['enabled']=not rules[int(sel[0])].get('enabled',True); self.save_profiles(); reload()
        def remove():
            sel=tree.selection();
            if sel: rules.pop(int(sel[0])); self.save_profiles(); reload()
        b=ttk.Frame(w); b.pack(fill='x',padx=8,pady=8)
        for text,cmd in [('添加规则',edit),('编辑',edit),('启用/停用',toggle),('立即执行',run),('删除',remove)]: ttk.Button(b,text=text,command=cmd).pack(side='left',padx=3)
        reload()
    def rule_dialog(self,rules,old,reload):
        w=tk.Toplevel(self); w.title('编辑同步规则' if old else '添加同步规则'); vars={}
        fields=[('本地文件夹','local'),('远端文件夹','remote')]
        for row,(label,key) in enumerate(fields):
            ttk.Label(w,text=label).grid(row=row,column=0,padx=8,pady=6)
            v=tk.StringVar(value=(old or {}).get(key,'/' if key=='remote' else '')); vars[key]=v
            frame=ttk.Frame(w); frame.grid(row=row,column=1,padx=8,pady=6,sticky='ew'); ttk.Entry(frame,textvariable=v,width=38).pack(side='left',fill='x',expand=True)
            if key=='local': ttk.Button(frame,text='浏览…',command=lambda v=v:self.choose_local_folder(v)).pack(side='left',padx=4)
            else: ttk.Button(frame,text='选择…',command=lambda v=v:self.choose_remote_folder(v)).pack(side='left',padx=4)
        checks=[]
        for row,(label,key,default) in enumerate([('本地新增/修改上传','upload',True),('远端新增/修改下载','download',False),('本地删除同步到远端','delete_remote',False),('远端删除同步到本地','delete_local',False),('检测本地变化自动同步','watch',False)],2):
            v=tk.BooleanVar(value=(old or {}).get(key,default)); checks.append((key,v)); ttk.Checkbutton(w,text=label,variable=v).grid(row=row,column=1,sticky='w',padx=8,pady=3)
        ttk.Label(w,text='冲突策略').grid(row=7,column=0,padx=8,pady=6); conflict=tk.StringVar(value=(old or {}).get('conflict','keep_newest')); ttk.Combobox(w,textvariable=conflict,values=('keep_newest','keep_local','keep_remote','duplicate'),state='readonly').grid(row=7,column=1,sticky='w',padx=8)
        ttk.Label(w,text='同步间隔(分钟)').grid(row=8,column=0,padx=8,pady=6); interval=tk.IntVar(value=(old or {}).get('interval',15)); ttk.Spinbox(w,from_=1,to=1440,textvariable=interval,width=8).grid(row=8,column=1,sticky='w',padx=8)
        def save():
            r=old or {'id':str(uuid.uuid4()),'enabled':True}; r.update({k:v.get() for k,v in vars.items()}); r.update({k:v.get() for k,v in checks}); r['conflict']=conflict.get(); r['interval']=max(1,interval.get());
            if not old: rules.append(r)
            self.save_profiles(); w.destroy(); reload()
        ttk.Button(w,text='保存',command=save).grid(row=9,column=1,sticky='e',padx=8,pady=10)
    def choose_local_folder(self,variable):
        path=filedialog.askdirectory(parent=self,title='选择本地同步文件夹')
        if path: variable.set(path)
    def choose_remote_folder(self,variable):
        if not self.session:return
        w=tk.Toplevel(self); w.title('选择远端文件夹'); w.geometry('520x400'); tree=ttk.Treeview(w,columns=('path',),show='tree headings'); tree.heading('#0',text='名称'); tree.heading('path',text='路径'); tree.pack(fill='both',expand=True,padx=8,pady=8)
        current=norm(variable.get() or '/'); tree.insert('', 'end',iid='__root__',text='/',values=('/',)); tree.selection_set('__root__')
        def load(parent,path):
            self.worker(lambda:self.session.list(path),lambda items: [tree.insert(parent,'end',text='📁 '+x['name'],values=(x['path'],)) for x in items if x['directory']])
        load('__root__',current)
        def choose():
            selected=tree.selection()
            if selected:
                variable.set(tree.item(selected[0],'values')[0]); w.destroy()
        tree.bind('<Double-1>',lambda _: load(tree.selection()[0],tree.item(tree.selection()[0],'values')[0]) if tree.selection() else None)
        ttk.Button(w,text='选择',command=choose).pack(anchor='e',padx=8,pady=8)
    def run_rule(self,rule,reload=lambda:None):
        rule_id=rule.get('id')
        if not rule.get('enabled',True) or rule_id in self.active_sync_rules: return
        self.active_sync_rules.add(rule_id)
        def job():
            try:
                report=SyncEngine(self.session,rule,lambda s: self.after(0,lambda:self.add_transfer('同步',s))).run(); self.save_profiles(); return report
            finally:
                self.after(0,lambda:self.active_sync_rules.discard(rule_id))
        self.worker(job,lambda report:(self.add_transfer('同步',f"完成：上传 {report['uploaded']}，下载 {report['downloaded']}，删除 {report['deleted_remote']+report['deleted_local']}"),reload()))
    def auto_sync(self):
        if self.session and self.auto_sync_enabled and not self.sync_scan_busy:
            rules=list(self.session.p.get('sync_rules',[])); self.sync_scan_busy=True
            def scan():
                due=[]
                for rule in rules:
                    changed=False
                    if rule.get('watch'):
                        fingerprint=self.local_fingerprint(rule.get('local','')); previous=self.local_fingerprints.get(rule.get('id')); self.local_fingerprints[rule.get('id')]=fingerprint; changed=previous is not None and previous != fingerprint
                    scheduled=rule.get('last_sync',0)+rule.get('interval',15)*60<=time.time()
                    if changed or scheduled: due.append(rule)
                self.after(0,lambda:self.finish_auto_scan(due))
            threading.Thread(target=scan,daemon=True).start()
        self.after(5000,self.auto_sync)
    def finish_auto_scan(self,rules):
        self.sync_scan_busy=False
        for rule in rules:self.run_rule(rule)
    @staticmethod
    def local_fingerprint(folder):
        if not folder or not os.path.isdir(folder): return None
        count=0; total=0; newest=0
        for root,dirs,files in os.walk(folder):
            count += len(dirs)+len(files)
            for name in files:
                try:
                    info=os.stat(os.path.join(root,name)); total += info.st_size; newest=max(newest,info.st_mtime)
                except OSError: pass
        return count,total,newest
    def profile_dialog(self,old=None):
        w=tk.Toplevel(self); w.title('编辑连接' if old else '新建连接'); w.transient(self); w.grab_set(); vars={}
        fields=[('名称','name'),('协议','protocol'),('服务器地址','host'),('端口','port'),('用户名','username'),('密码','password'),('远端根目录','base_path'),('认证方式','auth'),('私钥路径','private_key'),('主机密钥策略','host_key_policy')]
        for r,(label,key) in enumerate(fields):
            ttk.Label(w,text=label).grid(row=r,column=0,sticky='w',padx=10,pady=5)
            v=tk.StringVar(value=str((old or {}).get(key, {'protocol':'FTP','port':'21','base_path':'/','auth':'password','host_key_policy':'accept-new','private_key':''}.get(key,''))))
            vars[key]=v
            if key == 'protocol': ent=ttk.Combobox(w,textvariable=v,values=('FTP','SFTP','WebDAV'),state='readonly')
            elif key == 'auth': ent=ttk.Combobox(w,textvariable=v,values=('password','ssh_agent','private_key'),state='readonly')
            elif key == 'host_key_policy': ent=ttk.Combobox(w,textvariable=v,values=('accept-new','strict'),state='readonly')
            else: ent=ttk.Entry(w,textvariable=v,show='*' if key=='password' else '')
            if key=='private_key':
                frame=ttk.Frame(w); frame.grid(row=r,column=1,padx=10,pady=5,sticky='ew'); ent.pack(in_=frame,side='left',fill='x',expand=True); ttk.Button(frame,text='浏览…',command=lambda v=v:self.choose_private_key(v)).pack(side='left',padx=4)
            else: ent.grid(row=r,column=1,padx=10,pady=5)
        def save():
            try: p={k:v.get() for k,v in vars.items()}; p['id']=(old or {}).get('id',str(uuid.uuid4())); p['port']=int(p['port']); p['tls']=p['protocol']=='WebDAV'; p['sync_rules']=(old or {}).get('sync_rules',[])
            except ValueError: messagebox.showerror('输入错误','端口必须是数字',parent=w); return
            address=p['host'].strip()
            if not address:
                messagebox.showerror('输入错误','请填写服务器地址',parent=w); return
            if '://' in address:
                parsed=urllib.parse.urlparse(address)
                expected={'FTP':('ftp','ftps'),'SFTP':('sftp',),'WebDAV':('http','https')}[p['protocol']]
                if parsed.scheme.lower() not in expected or not parsed.hostname or parsed.query or parsed.fragment or parsed.password is not None:
                    messagebox.showerror('输入错误','服务器 URL 与所选协议不匹配，且不能包含密码、查询参数或锚点',parent=w); return
                try: parsed_port=parsed.port
                except ValueError: messagebox.showerror('输入错误','URL 端口无效',parent=w); return
                p['host']=parsed.hostname; p['port']=parsed_port or p['port']; p['base_path']=norm(parsed.path or p['base_path']); p['tls']=parsed.scheme.lower() in ('https','ftps')
            elif '/' in address:
                messagebox.showerror('输入错误','服务器地址不能包含路径；请填写完整 URL',parent=w); return
            if old:self.profiles=[p if x['id']==old['id'] else x for x in self.profiles]
            else:self.profiles.append(p)
            self.save_profiles(); self.refresh_profiles(); w.destroy()
        ttk.Button(w,text='保存',command=save).grid(row=len(fields),column=1,sticky='e',padx=10,pady=10)
    def choose_private_key(self,variable):
        path=filedialog.askopenfilename(parent=self,title='选择 SSH 私钥文件')
        if path: variable.set(path)

def main():
    App().mainloop()


if __name__ == '__main__':
    main()
