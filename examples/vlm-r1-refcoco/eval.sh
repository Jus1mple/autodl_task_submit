#!/bin/bash
# 评估训练效果：对比 base 模型 vs 训练后的 LoRA adapter 在测试样本上的 REC 定位准确度（IoU）。
# 用法：autodl run --script examples/vlm-r1-refcoco/eval.sh --name eval
# 环境变量：CKPT(默认 checkpoint-200) / N_TEST(默认 15)
set -e
. /root/miniconda3/etc/profile.d/conda.sh; conda activate base
export HF_ENDPOINT=https://hf-mirror.com
CKPT=${CKPT:-checkpoint-200}
N_TEST=${N_TEST:-15}
cd /root/autodl-tmp/vlm-r1/src/open-r1-multimodal
python - "$CKPT" "$N_TEST" <<'PY'
import torch, json, re, os, sys
from transformers import Qwen2_5_VLForConditionalGeneration, AutoProcessor
from peft import PeftModel
from PIL import Image
from qwen_vl_utils import process_vision_info

CKPT, N = sys.argv[1], int(sys.argv[2])
MODEL = "Qwen/Qwen2.5-VL-3B-Instruct"
ADAPTER = f"/root/autodl-tmp/real_run/output/{CKPT}"
IMGROOT = "/root/autodl-tmp/coco_subset"
TPL = ("{q} First output the thinking process in <think></think> tags and then output "
       "the final answer in <answer></answer> tags. Output the final answer in JSON format.")

def iou(a, b):
    x1, y1 = max(a[0], b[0]), max(a[1], b[1]); x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    if x2 <= x1 or y2 <= y1: return 0.0
    it = (x2 - x1) * (y2 - y1)
    u = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - it
    return it / u if u > 0 else 0.0

def getbox(t):
    m = re.search(r'<answer>(.*?)</answer>', t, re.DOTALL); s = m.group(1) if m else t
    m = re.search(r'\[(\d+),\s*(\d+),\s*(\d+),\s*(\d+)\]', s)
    return [int(m.group(i)) for i in range(1, 5)] if m else None

rows = [json.loads(l) for l in open("/root/autodl-tmp/refcocog_sub.jsonl")]
test = rows[-N:]
proc = AutoProcessor.from_pretrained(MODEL, max_pixels=200704)

def ev(model, tag):
    ious = []
    for r in test:
        p = os.path.join(IMGROOT, r['image']); im = Image.open(p).convert('RGB'); W, H = im.size
        gt = r['conversations'][1]['value']
        expr = r['conversations'][0]['value'].split('describes: ')[-1].rstrip('.')
        q = TPL.format(q=f"Please provide the bounding box coordinate of the region this sentence describes: {expr}.")
        msgs = [{"role": "user", "content": [{"type": "image", "image": p}, {"type": "text", "text": q}]}]
        txt = proc.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)
        imgs, _ = process_vision_info(msgs)
        inp = proc(text=[txt], images=imgs, return_tensors="pt").to(model.device)
        g = inp['image_grid_thw'][0]; ih, iw = int(g[1] * 14), int(g[2] * 14)
        with torch.no_grad():
            o = model.generate(**inp, max_new_tokens=256, do_sample=False)
        gen = proc.decode(o[0][inp['input_ids'].shape[1]:], skip_special_tokens=True)
        bb = getbox(gen)
        ious.append(iou([bb[0]/iw*W, bb[1]/ih*H, bb[2]/iw*W, bb[3]/ih*H], gt) if bb else 0.0)
    avg = sum(ious) / len(ious); hit = sum(1 for x in ious if x > 0.5) / len(ious)
    print(f"[{tag}] 平均 IoU={avg:.3f} | IoU>0.5 命中率={hit:.0%}")
    return avg

base = Qwen2_5_VLForConditionalGeneration.from_pretrained(MODEL, torch_dtype=torch.bfloat16,
                                                          device_map="cuda", attn_implementation="sdpa")
ab = ev(base, "base 未训练")
tr = PeftModel.from_pretrained(base, ADAPTER)
at = ev(tr, f"trained ({CKPT})")
print(f"\n=== 平均 IoU  base {ab:.3f} -> trained {at:.3f}  (提升 {at-ab:+.3f}) ===")
PY
echo DONE
