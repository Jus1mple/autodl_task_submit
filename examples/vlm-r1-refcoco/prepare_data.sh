#!/bin/bash
# 准备 RefCOCOg REC 数据：下载官方标注 + 从 COCO train2014 解压子集。
# 用法：autodl run --script examples/vlm-r1-refcoco/prepare_data.sh --background --name prep
#   N_SAMPLES  取前多少条样本（默认 2000；越多越好，但要注意磁盘）
set -e
. /root/miniconda3/etc/profile.d/conda.sh
conda activate base
source /etc/network_turbo 2>/dev/null || true
export HF_ENDPOINT=https://hf-mirror.com
cd /root/autodl-tmp
N_SAMPLES=${N_SAMPLES:-2000}

echo "=== 下载 RefCOCO/+/g 标注 ==="
[ -f rec_jsons.zip ] || wget -q https://huggingface.co/datasets/omlab/VLM-R1/resolve/main/rec_jsons_processed.zip -O rec_jsons.zip
rm -rf rec_data && mkdir -p rec_data && unzip -o -q rec_jsons.zip -d rec_data

echo "=== 从 COCO train2014 解压子集（前 $N_SAMPLES 条引用的图片）==="
# COCO 图片：优先用 AutoDL 公共数据集 COCO14/train2014.zip；没有则从 HF 下 train2014.zip
ZIP=""
for c in /root/autodl-pub/COCO14/train2014.zip /root/autodl-pub/COCO2014/train2014.zip; do
  [ -f "$c" ] && ZIP="$c" && break
done
if [ -z "$ZIP" ]; then
  echo "公共数据集无 COCO，从 HF 下载 train2014.zip（13GB，较慢）..."
  wget -q https://huggingface.co/datasets/omlab/VLM-R1/resolve/main/train2014.zip -O train2014.zip
  ZIP=/root/autodl-tmp/train2014.zip
fi
echo "COCO zip: $ZIP"

python - "$N_SAMPLES" "$ZIP" <<'PY'
import json, os, sys, zipfile
N = int(sys.argv[1]); zp = sys.argv[2]
root = "/root/autodl-tmp"
jf = f"{root}/rec_data/rec_jsons_processed/refcocog_train.jsonl"
rows = [json.loads(l) for l in open(jf)][:N]
imgs = sorted({r["image"] for r in rows})
print(f"子集 {len(rows)} 条, 去重 {len(imgs)} 张图")
dest = f"{root}/coco_subset"
os.makedirs(dest, exist_ok=True)
with zipfile.ZipFile(zp) as z:
    names = set(z.namelist())
    got = 0
    for img in imgs:
        for cand in (img, f"train2014/{os.path.basename(img)}", os.path.basename(img)):
            if cand in names:
                z.extract(cand, dest); got += 1; break
    print(f"解压 {got}/{len(imgs)} 张")
ok = [r for r in rows if os.path.exists(f"{dest}/{r['image']}")]
open(f"{root}/refcocog_sub.jsonl", "w").write("\n".join(json.dumps(r) for r in ok) + "\n")
print(f"可用: {len(ok)} 条 -> refcocog_sub.jsonl | 图片: {os.popen(f'du -sh {dest}').read().split()[0]}")
PY
echo DONE
