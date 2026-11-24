"""Bounded local inference worker. No network, tools, or arbitrary code loading."""
import json,sys
from pathlib import Path

def main():
    import ctranslate2
    import sentencepiece
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stdin.reconfigure(encoding='utf-8')
    root=Path(sys.argv[1])
    text=str(json.loads(sys.stdin.read(16000))['text'])[:1000]
    source=sentencepiece.SentencePieceProcessor(model_file=str(root/'source.spm'))
    target=sentencepiece.SentencePieceProcessor(model_file=str(root/'target.spm'))
    model=ctranslate2.Translator(str(root),device='cpu',compute_type='int8',inter_threads=1,intra_threads=1)
    result=model.translate_batch([source.encode(text,out_type=str)],beam_size=3,max_decoding_length=384)[0]
    print(json.dumps({'translation':target.decode(result.hypotheses[0])},ensure_ascii=False))
if __name__=='__main__':main()
