# 自托管 FunASR 识别服务：构建期下载 SenseVoiceSmall，运行期完全离线。
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MODELSCOPE_CACHE=/opt/modelscope

WORKDIR /app

# torch 走 CPU wheel，避免拉取 CUDA 依赖；版本固定在实测可用的组合。
RUN pip install --index-url https://download.pytorch.org/whl/cpu \
        --extra-index-url https://pypi.org/simple \
        torch==2.14.1 torchaudio==2.11.0 \
 && pip install funasr==1.4.16 modelscope==1.40.2 fastapi==0.161.1 \
        uvicorn==0.54.0 soundfile==0.14.0

# 下载快照后复制到固定路径 /opt/model，避免运行期依赖 ModelScope 缓存目录布局；
# 同时加载一次模型做预热，模型或依赖有问题时在构建期失败。缓存不再保留，控制镜像体积。
RUN python - <<'PY'
import shutil
import os

from funasr import AutoModel
from modelscope.hub.snapshot_download import snapshot_download

src = snapshot_download("iic/SenseVoiceSmall")
shutil.copytree(src, "/opt/model", dirs_exist_ok=True)
AutoModel(model="/opt/model", device="cpu", disable_update=True, check_latest=False)
shutil.rmtree(os.environ["MODELSCOPE_CACHE"], ignore_errors=True)
PY

COPY docker/asr/app.py .

# 模型许可全文与署名随镜像分发（许可 §2.2），不依赖运行中的应用。
COPY docker/asr/MODEL_LICENSE.txt /opt/licenses/SenseVoiceSmall/MODEL_LICENSE.txt
COPY docker/asr/NOTICE.txt /opt/licenses/SenseVoiceSmall/NOTICE.txt

ENV HF_HUB_OFFLINE=1

EXPOSE 8100

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8100"]
