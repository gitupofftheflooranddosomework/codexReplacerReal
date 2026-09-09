#!/usr/bin/env python3
import base64
import importlib.util
import io
import json
import os
import pathlib
import subprocess
import tempfile

ROOT=pathlib.Path(__file__).resolve().parent

def load(path,name):
    spec=importlib.util.spec_from_file_location(name,path)
    mod=importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(mod)
    return mod

def run(*args,cwd=None):
    cp=subprocess.run(list(args),cwd=cwd,text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=False)
    if cp.returncode:
        raise AssertionError(cp.stderr or cp.stdout or args)
    return cp.stdout.strip()

def main():
    dispatch=load(ROOT/'codex-ci-dispatch.py','dispatch_test')
    worker=load(ROOT/'codex-ci-worker-rpc.py','worker_test')
    with tempfile.TemporaryDirectory() as td:
        root=pathlib.Path(td)
        src=root/'src'; src.mkdir()
        run('git','init','-q',cwd=src)
        run('git','config','user.name','Test',cwd=src); run('git','config','user.email','test@example.invalid',cwd=src)
        (src/'data.txt').write_text('one\n')
        run('git','add','data.txt',cwd=src); run('git','commit','-qm','one',cwd=src)
        parent=run('git','rev-parse','HEAD',cwd=src)
        (src/'data.txt').write_text('one\ntwo\n')
        run('git','commit','-qam','two',cwd=src)
        head=run('git','rev-parse','HEAD',cwd=src)
        run('git','remote','add','origin','https://token:secret@github.com/example/private.git',cwd=src)
        assert dispatch.worker_origin(src)=='https://github.com/example/private.git'
        bundle=root/'repo.bundle'
        with bundle.open('wb') as out:
            cp=subprocess.run(['git','bundle','create','-','HEAD'],cwd=src,stdout=out,stderr=subprocess.PIPE,check=False)
        assert cp.returncode==0,cp.stderr.decode()

        iid='a'*32
        work=root/'worker'
        work.mkdir()
        worker.WORK_ROOT=work
        worker.EXCLUSIVE=root/'exclusive.lock'
        worker.EXCLUSIVE.write_text(json.dumps({'leaseId':iid,'source':'github-actions','owner':'test'}))
        installed=worker.install_git_bundle(iid,head,'https://github.com/example/private.git',io.BytesIO(bundle.read_bytes()))
        assert run('git','rev-parse','HEAD',cwd=installed)==head
        assert run('git','rev-parse','HEAD^',cwd=installed)==parent
        assert run('git','remote','get-url','origin',cwd=installed)=='https://github.com/example/private.git'
        assert run('git','status','--porcelain',cwd=installed)==''
        assert (installed/'data.txt').read_text()=='one\ntwo\n'

    runner=(ROOT/'headless-job-runner.py').read_text()
    assert '--depth=1' not in runner
    assert '--filter=blob:none' not in runner
    dsource=(ROOT/'codex-ci-dispatch.py').read_text()
    assert 'git","bundle","create","-","HEAD' in dsource
    assert 'codex-ci import-git' in dsource
    assert 'git","archive"' not in dsource
    wsource=(ROOT/'codex-ci-worker-rpc.py').read_text()
    assert '"import-git":action_import_git' in wsource
    print('headless_repo_transfer_test=ok')

if __name__=='__main__': main()
