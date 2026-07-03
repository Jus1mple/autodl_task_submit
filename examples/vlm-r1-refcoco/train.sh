#!/bin/bash
# VLM-R1 REC 训练：Qwen2.5-VL-3B + GRPO-LoRA，官方 grpo_jsonl.py 流程，真实 RefCOCOg 数据。
# 用法：autodl run --script examples/vlm-r1-refcoco/train.sh --background --name train \
#          --pull 'real_run/output/checkpoint-*/,real_run/metrics.json' --pull-to ./results
# 环境变量：MODEL(默认 3B) / STEPS(默认 200) / NUM_GEN(默认 4) / MAX_PIXELS(默认 200704)
set -e
. /root/miniconda3/etc/profile.d/conda.sh
conda activate base
export HF_ENDPOINT=https://hf-mirror.com
export DEBUG_MODE=true
MODEL=${MODEL:-Qwen/Qwen2.5-VL-3B-Instruct}
STEPS=${STEPS:-200}
NUM_GEN=${NUM_GEN:-4}
MAX_PIXELS=${MAX_PIXELS:-200704}
ROOT=/root/autodl-tmp/vlm-r1/src/open-r1-multimodal
DATA=/root/autodl-tmp/real_run
mkdir -p $DATA
rm -rf $DATA/output          # 清旧 checkpoint（换模型时防 shape 冲突）
export LOG_PATH=$DATA/debug.txt
rm -f ${LOG_PATH%.txt}_*.txt

echo "模型: $MODEL | 步数: $STEPS | num_gen: $NUM_GEN | max_pixels: $MAX_PIXELS"
echo "样本: $(wc -l < /root/autodl-tmp/refcocog_sub.jsonl) 条 | 图片: $(ls /root/autodl-tmp/coco_subset/train2014 | wc -l) 张"

cd $ROOT
python src/open_r1/grpo_jsonl.py \
    --use_vllm False \
    --output_dir $DATA/output \
    --model_name_or_path "$MODEL" \
    --data_file_paths /root/autodl-tmp/refcocog_sub.jsonl \
    --image_folders /root/autodl-tmp/coco_subset \
    --is_reward_customized_from_vlm_module True \
    --task_type rec \
    --per_device_train_batch_size 4 --gradient_accumulation_steps 2 \
    --gradient_checkpointing true --logging_steps 1 --max_steps $STEPS \
    --bf16 --torch_dtype bfloat16 --attn_implementation sdpa \
    --num_generations $NUM_GEN --max_completion_length 256 --max_prompt_length 1024 \
    --max_pixels $MAX_PIXELS \
    --reward_funcs accuracy format --beta 0.04 --report_to none \
    --learning_rate 1e-5 \
    --use_peft true --lora_r 64 --lora_alpha 128 --lora_dropout 0.05 --lora_task_type CAUSAL_LM \
    --freeze_vision_modules true \
    --save_strategy steps --save_steps 100 --save_only_model true \
    --dataset_name this_is_not_used \
    --run_name vlm_rec 2>&1 | tee $DATA/train.log

echo "=== 解析每步指标 -> metrics.json（含 series 趋势）==="
python - <<'PY'
import re, ast, json
text = open("/root/autodl-tmp/real_run/train.log").read()
series = []
for m in re.finditer(r"\{'loss':.*?'epoch':[^}]*\}", text):
    try: d = ast.literal_eval(m.group(0))
    except Exception: continue
    series.append({"step": len(series)+1, "loss": d.get("loss"), "reward": d.get("reward"),
        "iou": d.get("rewards/iou_reward"), "format": d.get("rewards/format_reward_rec"),
        "reward_std": d.get("reward_std"), "kl": d.get("kl")})
f = series[-1] if series else {}
mx = lambda k: max((s[k] or 0 for s in series), default=0)
out = {"steps": len(series), "final_reward": f.get("reward"), "final_iou": f.get("iou"),
       "final_format": f.get("format"), "max_reward": mx("reward"), "max_iou": mx("iou"),
       "max_format": mx("format"), "series": series}
json.dump(out, open("/root/autodl-tmp/metrics.json", "w"), indent=2)
print(f"steps={out['steps']} final_iou={out['final_iou']} max_iou={out['max_iou']} final_format={out['final_format']}")
PY
echo DONE
