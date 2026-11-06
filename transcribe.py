"""本地语音转文字：用抖音创作台已经装好的 faster-whisper small 模型。由 video-tools 的 Python 运行。"""
import json
import sys

from faster_whisper import WhisperModel


def main(model_dir,media,language='zh'):
    model=WhisperModel(model_dir,device='cpu',compute_type='int8')
    segments,info=model.transcribe(media,language=language,vad_filter=True,beam_size=5,
                                   initial_prompt='English speech.' if language=='en' else '以下是普通话的句子，使用简体中文。')
    rows=[{'start':round(s.start,1),'end':round(s.end,1),'text':s.text.strip()} for s in segments if s.text.strip()]
    return {'ok':True,'duration':round(info.duration,1),'segments':rows,'text':'\n'.join(r['text'] for r in rows)}


if __name__=='__main__':
    try:print(json.dumps(main(sys.argv[1],sys.argv[2],sys.argv[3] if len(sys.argv)>3 else 'zh'),ensure_ascii=False))
    except Exception as error:print(json.dumps({'ok':False,'error':type(error).__name__}))
