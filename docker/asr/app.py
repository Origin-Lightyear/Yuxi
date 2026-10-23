"""自托管 FunASR 语音识别服务：接收 WAV 字节，返回纯文本。"""

import io
import logging
import threading
import wave

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from funasr import AutoModel
from funasr.utils.postprocess_utils import rich_transcription_postprocess
from starlette.concurrency import run_in_threadpool

logger = logging.getLogger("asr")

# 构建期已把 SenseVoiceSmall 固化到这个目录；直接加载本地目录，运行期不访问 ModelScope。
MODEL_DIR = "/opt/modelscope/models/iic--SenseVoiceSmall/snapshots/master"
MODEL = None
# FunASR 推理非线程安全，串行执行避免并发请求互相干扰。
_INFERENCE_LOCK = threading.Lock()


def _load_model():
    """加载一次模型；失败直接终止进程，避免以未就绪状态对外服务。"""
    try:
        # check_latest=False 关闭 ModelScope 的版本探测，否则启动时仍会发起网络请求。
        return AutoModel(
            model=MODEL_DIR, device="cpu", disable_update=True, check_latest=False
        )
    except Exception:
        logger.exception("语音识别模型加载失败")
        raise SystemExit(1)


MODEL = _load_model()

app = FastAPI(title="Yuxi ASR")


def _is_valid_wav(data: bytes) -> bool:
    """只接受可解析且含音频帧的 WAV，其他音轨在入口直接拒绝。"""
    try:
        with wave.open(io.BytesIO(data), "rb") as wav:
            return wav.getnframes() > 0
    except (wave.Error, EOFError):
        return False


def _transcribe(data: bytes) -> str:
    """同步执行推理，结果经富文本后处理去掉 <|zh|> 一类标签。"""
    with _INFERENCE_LOCK:
        result = MODEL.generate(input=data, language="zh", use_itn=True)
    return rich_transcription_postprocess(result[0]["text"]).strip()


@app.get("/health")
def health():
    if MODEL is None:
        return JSONResponse({"status": "unavailable"}, status_code=503)
    return {"status": "ok"}


@app.post("/transcribe")
async def transcribe(request: Request):
    data = await request.body()
    if not _is_valid_wav(data):
        raise HTTPException(status_code=400, detail="请求体不是有效的 WAV 音频")
    if MODEL is None:
        raise HTTPException(status_code=503, detail="语音识别模型未就绪")
    text = await run_in_threadpool(_transcribe, data)
    return {"text": text}
