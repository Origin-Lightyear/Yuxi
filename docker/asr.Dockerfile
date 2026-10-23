# 自托管 FunASR 识别服务：构建期下载 SenseVoiceSmall，运行期完全离线。
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MODELSCOPE_CACHE=/opt/modelscope

WORKDIR /app

# torch 走 CPU wheel，避免拉取 CUDA 依赖。
RUN pip install --index-url https://download.pytorch.org/whl/cpu \
        --extra-index-url https://pypi.org/simple \
        torch torchaudio \
 && pip install funasr modelscope fastapi uvicorn soundfile

# 构建期把模型固化进镜像，运行期不需要再下载。
RUN python -c "from funasr import AutoModel; AutoModel(model='iic/SenseVoiceSmall', device='cpu', disable_update=True)"

COPY docker/asr/app.py .

ENV HF_HUB_OFFLINE=1

EXPOSE 8100

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8100"]
