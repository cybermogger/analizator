"""Local display translation; original evidence and model inputs are untouched."""
import asyncio
import hashlib
from pathlib import Path
import re

class NewsTranslator:
    def __init__(self, storage):
        self.storage=storage
        self.model_path=Path(__file__).resolve().parents[2]/'data'/'translation-en-ru'
        self.runtime='starting'
        self._model=None
        self._lock=asyncio.Lock()
        storage.execute('CREATE TABLE IF NOT EXISTS news_translation_ru_v1(text_hash TEXT PRIMARY KEY, original TEXT NOT NULL, translated TEXT NOT NULL)')

    @staticmethod
    def key(text): return hashlib.sha256(('opus-en-ru-20200211|'+text).encode()).hexdigest()

    def view(self,text):
        letters=re.findall(r'[A-Za-zА-Яа-яЁё]',text)
        russian=len(re.findall(r'[А-Яа-яЁё]',text))
        if letters and russian/len(letters)>.4: return {'summary_ru':text,'translation_status':'ORIGINAL_RU'}
        rows=self.storage.query('SELECT translated FROM news_translation_ru_v1 WHERE text_hash=?',(self.key(text),))
        if rows: return {'summary_ru':rows[0]['translated'],'translation_status':'MACHINE_TRANSLATED'}
        return {'summary_ru':None,'translation_status':'PENDING' if self.model_path.joinpath('model.bin').exists() else 'MODEL_MISSING'}

    def _translate(self,text):
        # Isolate native inference/OpenMP libraries from the trading process.
        # Successful results are persisted, so each news text runs only once.
        import json,subprocess,sys
        completed=subprocess.run([sys.executable,str(Path(__file__).with_name('translate_worker.py')),str(self.model_path)],
            input=json.dumps({'text':text[:1000]}),capture_output=True,text=True,encoding='utf-8',timeout=25)
        if completed.returncode: raise RuntimeError('Local translation worker failed')
        return json.loads(completed.stdout)['translation']

    async def translate(self,text):
        existing=self.view(text)
        if existing['summary_ru'] or existing['translation_status']=='MODEL_MISSING': return existing
        async with self._lock:
            existing=self.view(text)
            if existing['summary_ru']: return existing
            try:
                result=await asyncio.to_thread(self._translate,text)
                if not result.strip(): raise ValueError('Empty translation')
                self.storage.execute('INSERT OR IGNORE INTO news_translation_ru_v1 VALUES(?,?,?)',(self.key(text),text,result))
                self.runtime='ready'
            except Exception:
                self.runtime='degraded'
                return {'summary_ru':None,'translation_status':'UNAVAILABLE'}
        return self.view(text)

    async def run(self,events,stop):
        while not stop.is_set():
            for event in events.latest(30):
                if stop.is_set(): return
                await self.translate(str(event.get('summary') or ''))
            try: await asyncio.wait_for(stop.wait(),timeout=15)
            except TimeoutError: pass
