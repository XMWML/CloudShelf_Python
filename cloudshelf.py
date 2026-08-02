#!/usr/bin/env python3
import json, os, posixpath, subprocess, threading, uuid, urllib.parse, urllib.request, urllib.error
from datetime import datetime
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, simpledialog

APP_DIR = os.path.join(os.path.expanduser('~'), '.cloudshelf')
PROFILE_FILE = os.path.join(APP_DIR, 'connections.json')

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

class RemoteClient:
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
            cmd = ['sftp', '-q', '-P', str(self.p['port']), f'{self.p.get("username","")}@{self.p["host"]}']
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
    def request(self, method, path, data=None, headers=None):
        req=urllib.request.Request(self.url(path), data=data, method=method, headers=headers or {})
        import base64; req.add_header('Authorization','Basic '+base64.b64encode(f'{self.p.get("username","")}:{self.password}'.encode()).decode())
        with urllib.request.urlopen(req,timeout=30) as r: return r.read()
    def mkdir(self, path): self.request('MKCOL',path)
    def delete(self, path): self.request('DELETE',path)
    def rename(self, old, new): self.request('MOVE',old,headers={'Destination':self.url(new),'Overwrite':'T'})
    def download(self, item, target):
        os.makedirs(os.path.dirname(target),exist_ok=True); open(target,'wb').write(self.request('GET',item['path']))
    def upload(self, local, directory): self.request('PUT',join(directory,os.path.basename(local)),open(local,'rb').read())
    def copy(self, item, directory):
        target=join(directory,item['name'])
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
        if self.p['protocol']=='WebDAV':
            self.request('MOVE',item['path'],headers={'Destination':self.url(target),'Overwrite':'T'}); return
        if item['directory']:
            self.copy(item,directory); self.delete(item['path'])
        else: self.rename(item['path'],target)

class App(tk.Tk):
    def __init__(self):
        super().__init__(); self.title('CloudShelf'); self.geometry('1240x760'); self.minsize(980,620)
        self.profiles=[]; self.session=None; self.path='/'; self.items=[]; self.transfers=[]
        self.load_profiles(); self.build_ui(); self.refresh_profiles()
    def load_profiles(self):
        try: self.profiles=json.load(open(PROFILE_FILE))
        except (OSError,ValueError): self.profiles=[]
    def save_profiles(self):
        os.makedirs(APP_DIR,exist_ok=True); json.dump(self.profiles,open(PROFILE_FILE,'w'),ensure_ascii=False,indent=2)
    def build_ui(self):
        bar=ttk.Frame(self,padding=6); bar.pack(fill='x')
        for text,cmd in [('新建连接',self.add_profile),('编辑连接',self.edit_profile),('上级目录',self.go_up),('刷新',self.refresh),('新建文件夹',self.mkdir),('上传',self.upload),('下载',self.download),('复制到',self.copy),('移动到',self.move),('重命名',self.rename),('删除',self.delete)]: ttk.Button(bar,text=text,command=cmd).pack(side='left',padx=2)
        pan=ttk.PanedWindow(self,orient='horizontal'); pan.pack(fill='both',expand=True,padx=6,pady=4)
        left=ttk.Frame(pan,width=270); right=ttk.Frame(pan); pan.add(left,weight=1); pan.add(right,weight=4)
        ttk.Label(left,text='连接').pack(anchor='w'); self.conn=ttk.Treeview(left,columns=('protocol',),show='tree headings',selectmode='browse'); self.conn.heading('#0',text='连接'); self.conn.heading('protocol',text='协议'); self.conn.column('protocol',width=70); self.conn.pack(fill='both',expand=True); self.conn.bind('<<TreeviewSelect>>',self.select_profile)
        head=ttk.Frame(right); head.pack(fill='x'); self.status=tk.StringVar(value='未选择连接'); ttk.Label(head,textvariable=self.status).pack(side='left'); self.path_var=tk.StringVar(value='/'); ttk.Label(head,textvariable=self.path_var).pack(side='right')
        self.files=ttk.Treeview(right,columns=('size','type','modified'),show='tree headings',selectmode='extended'); self.files.heading('#0',text='名称'); self.files.heading('size',text='大小'); self.files.heading('type',text='类型'); self.files.heading('modified',text='修改时间'); self.files.column('#0',width=430); self.files.column('size',width=100); self.files.column('type',width=100); self.files.pack(fill='both',expand=True); self.files.bind('<Double-1>',self.open_item); self.files.bind('<Button-3>',self.context_menu)
        ttk.Label(self,text='传输任务').pack(anchor='w',padx=8); self.transfer=ttk.Treeview(self,columns=('state',),show='tree headings',height=6); self.transfer.heading('#0',text='项目'); self.transfer.heading('state',text='状态'); self.transfer.pack(fill='x',padx=6,pady=(0,6))
    def refresh_profiles(self):
        self.conn.delete(*self.conn.get_children()); [self.conn.insert('', 'end', iid=str(p['id']), text=p['name'], values=(p['protocol'],)) for p in self.profiles]
    def select_profile(self,_=None):
        sel=self.conn.selection();
        if not sel:return
        self.session=RemoteClient(next(p for p in self.profiles if str(p['id'])==sel[0])); self.path='/'; self.status.set(f'{self.session.p["name"]}  |  {self.session.p["protocol"]}  |  已连接'); self.refresh()
    def worker(self, fn, ok=None):
        def run():
            try: result=fn(); self.after(0,lambda: ok(result) if ok else None)
            except Exception as e: self.after(0,lambda: messagebox.showerror('操作失败',str(e)))
        threading.Thread(target=run,daemon=True).start()
    def refresh(self):
        if not self.session:return
        self.worker(lambda:self.session.list(self.path),self.show_items)
    def show_items(self,items):
        self.items=items; self.files.delete(*self.files.get_children());
        if self.path!='/': self.files.insert('',0,iid='__up__',text='..',values=('-','上级目录','-'))
        for i,x in enumerate(items): self.files.insert('', 'end', iid=str(i), text=('📁 ' if x['directory'] else '📄 ')+x['name'],values=(fmt_size(x['size']), '文件夹' if x['directory'] else '文件',x['modified']))
        self.path_var.set(self.path)
    def open_item(self,_=None):
        sel=self.files.selection();
        if not sel:return
        if sel[0]=='__up__': self.go_up(); return
        x=self.items[int(sel[0])]
        if x['directory']: self.path=x['path']; self.refresh()
    def go_up(self):
        if self.session and self.path!='/': self.path=posixpath.dirname(self.path) or '/'; self.refresh()
    def selected(self): return [self.items[int(i)] for i in self.files.selection() if i!='__up__']
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
    def upload(self):
        if not self.session:return
        paths=filedialog.askopenfilenames(parent=self,title='选择要上传的文件')
        for p in paths:self.worker(lambda p=p:self.upload_path(p),lambda _:self.add_transfer(os.path.basename(p),'上传完成'))
        folder=filedialog.askdirectory(parent=self,title='或选择要上传的文件夹')
        if folder:self.worker(lambda:self.upload_path(folder),lambda _:self.add_transfer(os.path.basename(folder),'文件夹上传完成'))
    def upload_path(self,path):
        if not os.path.isdir(path): return self.session.upload(path,self.path)
        target=join(self.path,os.path.basename(path))
        try:self.session.mkdir(target)
        except Exception:pass
        for root,dirs,files in os.walk(path):
            rel=os.path.relpath(root,path); remote=target if rel=='.' else join(target,rel)
            for name in dirs:
                try:self.session.mkdir(join(remote,name))
                except Exception:pass
            for name in files:self.session.upload(os.path.join(root,name),remote)
    def download(self):
        xs=self.selected();
        if not self.session or not xs:return
        dest=filedialog.askdirectory(parent=self,title='选择下载目录');
        if dest:
            for x in xs:self.worker(lambda x=x:self.download_path(x,dest),lambda _,name=x['name']:self.add_transfer(name,'下载完成'))
    def download_path(self,item,dest):
        target=os.path.join(dest,item['name'])
        if item['directory']:
            os.makedirs(target,exist_ok=True)
            for child in self.session.list(item['path']):self.download_path(child,target)
        else:self.session.download(item,target)
    def destination(self,title): return simpledialog.askstring(title,'目标远端目录：',initialvalue=self.path,parent=self)
    def copy(self):
        xs=self.selected(); dest=self.destination('复制到') if xs else None
        if dest:
            for x in xs:self.worker(lambda x=x:self.session.copy(x,norm(dest)),lambda _,name=x['name']:self.add_transfer(name,'复制完成'))
    def move(self):
        xs=self.selected(); dest=self.destination('移动到') if xs else None
        if dest:
            for x in xs:self.worker(lambda x=x:self.session.move(x,norm(dest)),lambda _,name=x['name']:self.add_transfer(name,'移动完成'))
    def add_transfer(self,title,state): self.transfer.insert('',0,text=title,values=(state,))
    def context_menu(self,e):
        iid=self.files.identify_row(e.y)
        if iid and iid!='__up__': self.files.selection_set(iid)
        m=tk.Menu(self,tearoff=0); m.add_command(label='打开',command=self.open_item); m.add_command(label='下载',command=self.download); m.add_command(label='复制到',command=self.copy); m.add_command(label='移动到',command=self.move); m.add_command(label='重命名',command=self.rename); m.add_command(label='删除',command=self.delete); m.post(e.x_root,e.y_root)
    def add_profile(self): self.profile_dialog()
    def edit_profile(self):
        sel=self.conn.selection();
        if sel:self.profile_dialog(next(p for p in self.profiles if str(p['id'])==sel[0]))
    def profile_dialog(self,old=None):
        w=tk.Toplevel(self); w.title('编辑连接' if old else '新建连接'); w.transient(self); w.grab_set(); vars={}
        fields=[('名称','name'),('协议','protocol'),('服务器地址','host'),('端口','port'),('用户名','username'),('密码','password'),('远端根目录','base_path')]
        for r,(label,key) in enumerate(fields):
            ttk.Label(w,text=label).grid(row=r,column=0,sticky='w',padx=10,pady=5)
            v=tk.StringVar(value=str((old or {}).get(key, {'protocol':'FTP','port':'21','base_path':'/'}.get(key,''))))
            vars[key]=v
            if key == 'protocol': ent=ttk.Combobox(w,textvariable=v,values=('FTP','SFTP','WebDAV'),state='readonly')
            else: ent=ttk.Entry(w,textvariable=v,show='*' if key=='password' else '')
            ent.grid(row=r,column=1,padx=10,pady=5)
        def save():
            try: p={k:v.get() for k,v in vars.items()}; p['id']=(old or {}).get('id',str(uuid.uuid4())); p['port']=int(p['port']); p['tls']=p['protocol']=='WebDAV';
            except ValueError: messagebox.showerror('输入错误','端口必须是数字',parent=w); return
            if old:self.profiles=[p if x['id']==old['id'] else x for x in self.profiles]
            else:self.profiles.append(p)
            self.save_profiles(); self.refresh_profiles(); w.destroy()
        ttk.Button(w,text='保存',command=save).grid(row=len(fields),column=1,sticky='e',padx=10,pady=10)

if __name__=='__main__': App().mainloop()
