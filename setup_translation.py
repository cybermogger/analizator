"""Install the official Helsinki-NLP EN→RU model locally; no API key needed."""
from pathlib import Path
import shutil
import urllib.request
import zipfile
import ctranslate2

ROOT=Path(__file__).resolve().parent
def main():
    source=ROOT/'data'/'translation-source'
    target=ROOT/'data'/'translation-en-ru'
    source.mkdir(parents=True,exist_ok=True)
    archive=source/'opus-en-ru.zip'
    if not archive.exists():
        url='https://object.pouta.csc.fi/OPUS-MT-models/en-ru/opus-2020-02-11.zip'
        with urllib.request.urlopen(url,timeout=50) as response,(source/'download.part').open('wb') as output:
            while chunk:=response.read(1024*1024): output.write(chunk)
        (source/'download.part').replace(archive)
    with zipfile.ZipFile(archive) as package:
        for item in package.infolist():
            dest=(source/item.filename).resolve()
            if not dest.is_relative_to(source.resolve()): raise ValueError('Unsafe archive path')
        package.extractall(source)
    if not (target/'model.bin').exists():
        ctranslate2.converters.OpusMTConverter(str(source)).convert(str(target),quantization='int8',force=True)
    for filename in ('source.spm','target.spm'):
        shutil.copyfile(source/filename,target/filename)
    print('Local English to Russian translation is ready.')
if __name__=='__main__': main()
