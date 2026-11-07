"""中文转简体：使用 macOS 自带 ICU 转换，不安装依赖。"""
import ctypes
import sys

# 非 macOS 或系统转换不可用时的常见字兜底；目标运行环境为 macOS。
_FALLBACK=str.maketrans(dict(pair.split(':') for pair in '''
體:体 聲:声 識:识 語:语 車:车 錄:录 這:这 個:个 為:为 與:与 說:说
話:话 轉:转 寫:写 頻:频 開:开 關:关 後:后 裡:里 臺:台 灣:湾 萬:万
兩:两 點:点 擊:击 數:数 據:据 視:视 聽:听 見:见 時:时 間:间 長:长
網:网 絡:络 頁:页 設:设 備:备 傳:传 檔:档 發:发 現:现 實:实 結:结
果:果 學:学 習:习 費:费 錢:钱 愛:爱 買:买 賣:卖 東:东 麼:么 過:过
還:还 會:会 對:对 從:从 業:业 專:专 優:优 選:选 擇:择 機:机 電:电
動:动 無:无 須:须 應:应 當:当 認:认 證:证 確:确 請:请 問:问 題:题
簡:简 單:单 雙:双 線:线 練:练 經:经 驗:验 總:总 整:整 變:变 換:换
場:场 補:补 幫:帮 讓:让 氣:气 壓:压 輸:输 號:号 標:标 籤:签 級:级
訂:订 閱:阅 讚:赞 評:评 論:论 熱:热 榜:榜 種:种 類:类 資:资 訊:讯
記:记 憶:忆 憑:凭 鑰:钥 匙:匙 刪:删 除:除 繼:继 續:续 連:连 接:接
馬:马 達:达 駕:驾 駛:驶 險:险 養:养 鐵:铁 際:际 國:国 產:产 品:品
'''.split()))


def simplified(text):
    if sys.platform=='darwin':
        values=[]
        try:
            cf=ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
            ptr=ctypes.c_void_p;index=ctypes.c_long;utf8=0x08000100
            cf.CFStringCreateWithCString.argtypes=[ptr,ctypes.c_char_p,ctypes.c_uint32];cf.CFStringCreateWithCString.restype=ptr
            cf.CFStringCreateMutableCopy.argtypes=[ptr,index,ptr];cf.CFStringCreateMutableCopy.restype=ptr
            cf.CFStringTransform.argtypes=[ptr,ptr,ptr,ctypes.c_bool];cf.CFStringTransform.restype=ctypes.c_bool
            cf.CFStringGetLength.argtypes=[ptr];cf.CFStringGetLength.restype=index
            cf.CFStringGetCString.argtypes=[ptr,ptr,index,ctypes.c_uint32];cf.CFStringGetCString.restype=ctypes.c_bool
            cf.CFRelease.argtypes=[ptr]
            original=cf.CFStringCreateWithCString(None,text.encode(),utf8);values.append(original)
            transform=cf.CFStringCreateWithCString(None,b'Traditional-Simplified',utf8);values.append(transform)
            result=cf.CFStringCreateMutableCopy(None,0,original);values.append(result)
            if all(values) and cf.CFStringTransform(result,None,transform,False):
                buffer=ctypes.create_string_buffer(cf.CFStringGetLength(result)*4+1)
                if cf.CFStringGetCString(result,buffer,len(buffer),utf8):return buffer.value.decode()
        except (OSError,AttributeError):pass
        finally:
            for value in reversed(values):
                if value:cf.CFRelease(value)
    return text.translate(_FALLBACK)
