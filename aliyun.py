"""百炼适配器：仅标准库，密钥和请求体只留内存，错误不携带上游内容。"""
import base64
import json
from pathlib import Path
import subprocess
import tempfile
import urllib.request
import wave

from chinese import simplified

ENDPOINT='https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions'
ASR_MODEL='qwen3-asr-flash'
FFMPEG=Path.home()/'.local/bin/ffmpeg'
# 180 秒、16 kHz、单声道 PCM WAV 约 5.8 MB，Base64 后约 7.7 MB。
SEGMENT_SECONDS=180
MAX_AUDIO_BYTES=10_000_000


class AliyunError(RuntimeError):
    def __init__(self,message='阿里云百炼请求失败，请检查网络、额度和模型权限'):
        super().__init__(message)


def api_key():
    try:
        result=subprocess.run(['security','find-generic-password','-s','dashscope-api','-a','baokuan-radar','-w'],
                              capture_output=True,text=True,timeout=5,stdin=subprocess.DEVNULL)
        return result.stdout.strip() if result.returncode==0 else ''
    except (OSError,subprocess.TimeoutExpired):return ''


def completion(payload,key,timeout=300):
    """不读取 HTTP 错误体、不传播异常文本，也拒收任何回显密钥的成功响应。"""
    if not key:raise AliyunError('未配置阿里云百炼')
    try:
        request=urllib.request.Request(ENDPOINT,data=json.dumps(payload,ensure_ascii=False).encode(),
            headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'},method='POST')
        # 禁止重定向，避免 Authorization 被转发到其他地址。
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*args,**kwargs):return None
        with urllib.request.build_opener(NoRedirect).open(request,timeout=timeout) as response:
            body=response.read(4*1024*1024+1)
        if len(body)>4*1024*1024:raise ValueError()
        doc=json.loads(body)
        if key in json.dumps(doc,ensure_ascii=False):raise ValueError()
        choice=doc['choices'][0]
        if choice.get('finish_reason')!='stop':raise ValueError()
        text=choice['message']['content']
        if not isinstance(text,str) or not text.strip():raise ValueError()
        return text.strip()
    except Exception:
        raise AliyunError() from None


def recognize(path,language='zh',key=None):
    """本地媒体 → WAV 分段 → Base64 同步识别；整条成功才返回文字。"""
    key=api_key() if key is None else key
    if not key:raise AliyunError('未配置阿里云百炼')
    try:
        with tempfile.TemporaryDirectory(prefix='aliyun-',dir=Path(path).parent) as directory:
            folder=Path(directory)
            result=subprocess.run([str(FFMPEG),'-nostdin','-hide_banner','-loglevel','error','-i',str(path),
                '-map','0:a:0','-vn','-ac','1','-ar','16000','-c:a','pcm_s16le','-f','segment',
                '-segment_time',str(SEGMENT_SECONDS),'-reset_timestamps','1',str(folder/'part-%06d.wav')],
                capture_output=True,timeout=900,stdin=subprocess.DEVNULL)
            if result.returncode:raise ValueError()
            chunks=sorted(folder.glob('part-*.wav'))
            if not chunks:raise ValueError()
            texts=[]
            for chunk in chunks:
                with wave.open(str(chunk),'rb') as audio:
                    if audio.getnframes()/audio.getframerate()>300:raise ValueError()
                # 包含 Data URI 的 Base64 也严格小于官方 10 MB 上限。
                if 4*((chunk.stat().st_size+2)//3)+64>MAX_AUDIO_BYTES:raise ValueError()
                data='data:audio/wav;base64,'+base64.b64encode(chunk.read_bytes()).decode('ascii')
                text=completion({'model':ASR_MODEL,'stream':False,'messages':[{'role':'user','content':[
                    {'type':'input_audio','input_audio':{'data':data}}]}],
                    'asr_options':{'language':language,'enable_itn':True}},key,timeout=180)
                texts.append(simplified(text) if language=='zh' else text)
            return '\n'.join(texts)
    except AliyunError:raise
    except Exception:raise AliyunError('百炼语音识别失败，改用本地语音识别') from None
